"""Named snapshots of validated model settings; no provider invocation."""

import os
import tempfile
from copy import deepcopy
from pathlib import Path
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from novel_harness.ai.base import ExecutionLimits
from novel_harness.ai.compatible_provider import validate_api_url
from novel_harness.ai.local_provider import LocalProvider
from novel_harness.services.job_state import command_hash
from novel_harness.services.model_settings import (
    ModelConfigInput,
    config_revision,
    public_config,
)
from novel_harness.services.secret_store import reveal


class ProfileName(BaseModel):
    name: str = Field(min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def trimmed(cls, value):
        if not value.strip():
            raise ValueError("方案名称不能为空")
        return value.strip()


class CreateProfile(ProfileName):
    id: UUID
    expected_config_revision: str = Field(pattern=r"^[a-f0-9]{64}$")


class RenameProfile(ProfileName):
    expected_revision: int = Field(ge=1)


class ApplyProfile(BaseModel):
    expected_revision: int = Field(ge=1)
    expected_config_revision: str = Field(pattern=r"^[a-f0-9]{64}$")


class ProfileRecord(ProfileName):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    revision: int = Field(ge=1)
    config: dict
    creation_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class ProfileFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1, le=1)
    items: list[ProfileRecord] = Field(max_length=50)


def _checked_snapshot(config):
    if not isinstance(config, dict) or not isinstance(config.get("protected_key"), str):
        raise ValueError("Invalid model snapshot")
    payload = ModelConfigInput.model_validate(config)
    if payload.output_token_budget >= payload.context_capacity:
        raise ValueError("Invalid model window")
    if payload.mode != "demo" and not payload.model.strip():
        raise ValueError("Model is required")
    if payload.mode == "api":
        if (
            not payload.external_consent
            or not config["protected_key"]
            or validate_api_url(payload.base_url) != payload.base_url
        ):
            raise ValueError("Invalid API profile")
    elif payload.mode == "local":
        LocalProvider(payload.base_url, payload.model)
    allowed = {
        "mode",
        "base_url",
        "model",
        "external_consent",
        "protected_key",
        *ExecutionLimits.model_fields,
    }
    if set(config) != allowed:
        raise ValueError("Invalid snapshot fields")
    return deepcopy(config)


class ModelProfiles:
    def __init__(self, settings):
        self.settings = settings
        self.path = settings.path.with_name("model-profiles.json")

    def _read(self):
        if not self.path.exists():
            return []
        try:
            if self.path.stat().st_size > 1_048_576:
                raise ValueError("Oversized profile file")
            data = ProfileFile.model_validate_json(self.path.read_text(encoding="utf-8"))
            ids, names = set(), set()
            for row in data.items:
                if row.id in ids or row.name.casefold() in names:
                    raise ValueError("Duplicate profile")
                ids.add(row.id)
                names.add(row.name.casefold())
                _checked_snapshot(row.config)
            return data.items
        except (ValueError, UnicodeError, OSError):
            raise HTTPException(
                503,
                detail={
                    "code": "MODEL_PROFILES_UNAVAILABLE",
                    "message": "模型方案文件无法读取，未改动当前模型或方案，请检查本机文件。",
                },
            ) from None

    def _write(self, rows):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent, delete=False
            ) as output:
                temporary = output.name
                output.write(ProfileFile(version=1, items=rows).model_dump_json())
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

    def _public(self, row, current):
        return {
            "id": str(row.id),
            "name": row.name,
            "revision": row.revision,
            "config": public_config(row.config),
            "is_current": config_revision(row.config) == config_revision(current),
        }

    @staticmethod
    def _find(rows, identifier):
        row = next((row for row in rows if str(row.id) == str(identifier)), None)
        if row is None:
            raise HTTPException(
                404,
                detail={
                    "code": "MODEL_PROFILE_NOT_FOUND",
                    "message": "该模型方案已不存在，请刷新列表。",
                },
            )
        return row

    @staticmethod
    def _unique(rows, name, except_id=None):
        if any(row.name.casefold() == name.casefold() and row.id != except_id for row in rows):
            raise HTTPException(
                409,
                detail={
                    "code": "MODEL_PROFILE_NAME_EXISTS",
                    "message": "已有同名方案，请使用另一个名称。",
                },
            )

    def list(self):
        with self.settings.lock:
            current = self.settings._read()
            return {
                "items": [self._public(row, current) for row in self._read()],
                "current_config_revision": config_revision(current),
            }

    def create(self, payload):
        with self.settings.lock:
            rows = self._read()
            current = self.settings._read()
            digest = command_hash(payload.model_dump(mode="json"))
            existing = next((row for row in rows if row.id == payload.id), None)
            if existing:
                if existing.creation_hash != digest:
                    raise HTTPException(409, detail={"code": "IDEMPOTENCY_CONFLICT"})
                return self._public(existing, current)
            if config_revision(current) != payload.expected_config_revision:
                raise HTTPException(
                    409,
                    detail={
                        "code": "MODEL_CONFIG_CHANGED",
                        "message": "当前模型设置已更改，请刷新后再保存方案。",
                    },
                )
            if len(rows) >= 50:
                raise HTTPException(
                    409,
                    detail={
                        "code": "MODEL_PROFILE_LIMIT",
                        "message": "最多保存 50 个模型方案，请删除不再使用的方案。",
                    },
                )
            self._unique(rows, payload.name)
            row = ProfileRecord(
                id=payload.id,
                name=payload.name,
                revision=1,
                config=_checked_snapshot(current),
                creation_hash=digest,
            )
            self._write([*rows, row])
            return self._public(row, current)

    def rename(self, identifier, payload):
        with self.settings.lock:
            rows = self._read()
            row = self._find(rows, identifier)
            current = self.settings._read()
            if row.revision != payload.expected_revision:
                if row.revision == payload.expected_revision + 1 and row.name == payload.name:
                    return self._public(row, current)
                raise HTTPException(
                    409,
                    detail={
                        "code": "MODEL_PROFILE_CHANGED",
                        "message": "模型方案已在其他窗口更改，请刷新后再操作。",
                    },
                )
            self._unique(rows, payload.name, row.id)
            if row.name != payload.name:
                row.name = payload.name
                row.revision += 1
                self._write(rows)
            return self._public(row, current)

    def delete(self, identifier, revision):
        with self.settings.lock:
            rows = self._read()
            row = next((row for row in rows if str(row.id) == str(identifier)), None)
            if row is None:
                return {"deleted": True, "id": str(identifier)}
            if row.revision != revision:
                raise HTTPException(
                    409,
                    detail={
                        "code": "MODEL_PROFILE_CHANGED",
                        "message": "模型方案已在其他窗口更改，请刷新后再删除。",
                    },
                )
            self._write([value for value in rows if value.id != row.id])
            return {"deleted": True, "id": str(identifier)}

    def apply(self, identifier, payload):
        with self.settings.lock:
            row = self._find(self._read(), identifier)
            if row.revision != payload.expected_revision:
                raise HTTPException(
                    409,
                    detail={
                        "code": "MODEL_PROFILE_CHANGED",
                        "message": "模型方案已更改，请刷新后再应用。",
                    },
                )
            snapshot = _checked_snapshot(row.config)
            try:
                key = reveal(snapshot["protected_key"]) if snapshot["protected_key"] else ""
            except (ValueError, UnicodeError, OSError):
                raise HTTPException(
                    422,
                    detail={
                        "code": "MODEL_PROFILE_KEY_UNAVAILABLE",
                        "message": "此方案的密钥无法在当前系统账号解密，请重新保存模型和方案。",
                    },
                ) from None
            if snapshot["mode"] == "api" and not key.strip():
                raise HTTPException(
                    422,
                    detail={
                        "code": "MODEL_PROFILE_KEY_UNAVAILABLE",
                        "message": "此方案没有可用密钥，不会使用当前模型的密钥。",
                    },
                )
            if config_revision(self.settings._read()) == config_revision(snapshot):
                return self.settings.public()
            validated = ModelConfigInput.model_validate(
                {
                    **snapshot,
                    "api_key": key,
                    "clear_api_key": True,
                    "expected_config_revision": payload.expected_config_revision,
                }
            )
            return self.settings.save(validated, _protected_key=snapshot["protected_key"])
