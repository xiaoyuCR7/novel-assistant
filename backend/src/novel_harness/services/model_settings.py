"""Application-local model configuration, separate from every novel Vault."""

import json
import os
import shutil
import tempfile
from threading import RLock
from typing import Literal
from uuid import uuid4

from pydantic import Field, SecretStr

from novel_harness.ai.base import ExecutionLimits, ProviderError
from novel_harness.ai.compatible_provider import CompatibleProvider, validate_api_url
from novel_harness.ai.demo import DemoProvider
from novel_harness.ai.local_provider import LocalProvider
from novel_harness.services.secret_store import protect, reveal


class ModelConfigInput(ExecutionLimits):
    mode: Literal["demo", "local", "api"]
    base_url: str = Field(default="", max_length=1000)
    model: str = Field(default="", max_length=200)
    api_key: SecretStr = Field(default=SecretStr(""), max_length=4096)
    external_consent: bool = False
    clear_api_key: bool = False
    repair_config: bool = False


class CorruptModelConfig(ValueError):
    pass


class ModelSettings:
    def __init__(self, settings):
        self.path = settings.data_dir / ".settings" / "model.json"
        self.lock = RLock()
        self.default = {
            **ExecutionLimits().model_dump(),
            "mode": settings.ai_provider,
            "base_url": settings.local_model_url,
            "model": settings.local_text_model,
            "external_consent": False,
            "protected_key": "",
        }

    def _read(self):
        if not self.path.exists():
            return dict(self.default)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if (
                not isinstance(data, dict)
                or data.get("mode") not in ("demo", "local", "api")
                or any(
                    not isinstance(data.get(key), str)
                    for key in ("base_url", "model", "protected_key")
                )
                or type(data.get("external_consent")) is not bool
            ):
                raise ValueError("Invalid configuration")
            return {**data, **ExecutionLimits.model_validate(data).model_dump()}
        except (ValueError, UnicodeError):
            raise CorruptModelConfig(
                "模型配置已损坏；请明确重置后重新填写，不会自动切换提供者。"
            ) from None

    def identity(self):
        with self.lock:
            config = self._read()
            return {
                key: config[key]
                for key in (
                    "mode",
                    "base_url",
                    "model",
                    "external_consent",
                    *ExecutionLimits.model_fields,
                )
            }

    def provider_for_identity(self, expected, fallback):
        from fastapi import HTTPException

        with self.lock:
            if self.identity() != expected:
                raise HTTPException(409, detail={"code": "PROVIDER_CHANGED"})
            return self.provider(fallback)

    def public(self):
        with self.lock:
            config = self._read()
            return {
                key: config[key]
                for key in (
                    "mode",
                    "base_url",
                    "model",
                    "external_consent",
                    *ExecutionLimits.model_fields,
                )
            } | {"has_api_key": bool(config.get("protected_key"))}

    def save(self, payload):
        with self.lock:
            if payload.output_token_budget >= payload.context_capacity:
                raise ValueError("输出预算必须小于模型上下文容量。")
            repair = False
            try:
                old = self._read()
            except CorruptModelConfig:
                if not (payload.repair_config and payload.mode == "demo" and payload.clear_api_key):
                    raise
                old = dict(self.default)
                repair = True
            key = payload.api_key.get_secret_value().strip()
            base = payload.base_url.strip()
            model = payload.model.strip()
            protected = "" if payload.clear_api_key else old.get("protected_key", "")
            if payload.mode != "demo" and not model:
                raise ValueError("请填写模型名称。")
            if payload.mode == "api":
                base = validate_api_url(base)
                if not payload.external_consent:
                    raise ValueError("请确认允许将当前任务内容发送给所选 API 服务商。")
                if base != old.get("base_url") and not key:
                    raise ValueError("更换 API 地址后需重新输入密钥，避免发送到错误服务商。")
                if not key and not protected:
                    raise ValueError("请填写 API Key。")
            if payload.mode == "local":
                LocalProvider(base, model)  # Reuse strict loopback-only validation.
            if key:
                protected = protect(key)
            if base != old.get("base_url") and not key:
                protected = ""
            data = {
                **{key: getattr(payload, key) for key in ExecutionLimits.model_fields},
                "mode": payload.mode,
                "base_url": base,
                "model": model,
                "external_consent": payload.mode == "api" and payload.external_consent,
                "protected_key": protected,
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if repair:
                shutil.copy2(self.path, self.path.with_name(f"model.corrupt-{uuid4().hex}.json"))
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=self.path.parent, delete=False
                ) as file:
                    temporary = file.name
                    json.dump(data, file, ensure_ascii=False)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temporary, self.path)
            finally:
                if temporary and os.path.exists(temporary):
                    os.unlink(temporary)
            return self.public()

    def provider(self, fallback):
        with self.lock:
            if not self.path.exists():
                return fallback
            config = self._read()
            if config["mode"] == "demo":
                return DemoProvider()
            if config["mode"] == "local":
                return LocalProvider(config["base_url"], config["model"])
            if not config["external_consent"]:
                raise ValueError("外部 API 尚未授权。")
            return CompatibleProvider(
                config["base_url"], config["model"], reveal(config["protected_key"])
            )


def selected_provider(request):
    from fastapi import HTTPException

    try:
        return request.app.state.model_settings.provider(request.app.state.ai_provider)
    except (ValueError, OSError, ProviderError):
        raise HTTPException(
            422, detail={"message": "模型配置不可用，请打开设置重新保存。"}
        ) from None
