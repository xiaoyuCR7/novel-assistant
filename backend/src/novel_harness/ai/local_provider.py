"""Stateless Ollama requests; no cloud endpoint, proxy, redirect or model download."""

import ipaddress
import json
import math
from time import monotonic
from urllib.parse import urlsplit

import httpx

from novel_harness.ai.base import (
    MAX_RESPONSE_BYTES,
    ProviderConfigurationError,
    ProviderExecutionError,
    StructuredResult,
    TextResult,
    check_input_budget,
    output_metadata,
    read_json_response,
)
from novel_harness.ai.prompt_payload import messages


class LocalProvider:
    name = "local"

    def __init__(self, endpoint, model, *, transport=None, attempt_observer=None):
        parsed = urlsplit(endpoint)
        hostname = parsed.hostname or ""
        try:
            local = hostname == "localhost" or ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            local = False
        if (
            not local
            or parsed.scheme != "http"
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ProviderConfigurationError(
                "纯本地模式仅允许 http://127.0.0.1 或 ::1 本机模型服务。"
            )
        if not model.strip() or "cloud" in model.lower():
            raise ProviderConfigurationError("必须配置已安装的本地模型，不允许 cloud 模型。")
        host = "127.0.0.1" if hostname == "localhost" else hostname
        if ":" in host:
            host = f"[{host}]"
        self.endpoint = f"http://{host}:{parsed.port or 11434}"
        self.model = model
        self.transport = transport
        self.attempt_observer = attempt_observer

    def _post(self, path, payload, timeout=180, stream_request=None):
        try:
            from novel_harness.ai.network_safety import SafeTransport

            deadline = monotonic() + timeout
            with httpx.Client(
                transport=self.transport or SafeTransport(timeout),
                trust_env=False,
                follow_redirects=False,
                timeout=timeout,
                headers={"Accept-Encoding": "identity"},
            ) as client:
                for attempt in range(2):
                    if self.attempt_observer is not None:
                        self.attempt_observer.before_send()
                    try:
                        with client.stream("POST", self.endpoint + path, json=payload) as response:
                            response.raise_for_status()
                            if stream_request and "ndjson" in response.headers.get(
                                "content-type", ""
                            ):
                                from novel_harness.ai.streaming import read_text_stream

                                return read_text_stream(
                                    response,
                                    stream_request,
                                    self.attempt_observer,
                                    deadline,
                                    local=True,
                                )
                            return read_json_response(response, MAX_RESPONSE_BYTES, deadline)
                    except (httpx.ConnectError, httpx.ConnectTimeout):
                        if self.attempt_observer is not None:
                            self.attempt_observer.not_sent()
                        if attempt:
                            raise ProviderExecutionError(
                                "本机模型连接失败。", outcome="known", code="CONNECT_FAILED"
                            ) from None
        except (httpx.HTTPStatusError, ValueError, RecursionError):
            raise ProviderExecutionError(
                "本机模型服务不可用或返回无效结果；未调用任何云端服务。",
                outcome="known",
                code="INVALID_PROVIDER_RESPONSE",
            ) from None
        except httpx.HTTPError:
            raise ProviderExecutionError(
                "本机模型请求结果未知；未自动重新发送。", code="PROVIDER_RESULT_UNKNOWN"
            ) from None

    def _chat(self, request, schema=None):
        check_input_budget(request, schema, schema_in_system=False)
        # Each call supplies only its current project's packet; no shared chat history.
        payload = {
            "model": self.model,
            "stream": bool(request.stream_preview and schema is None),
            "options": {
                "num_predict": request.output_token_budget,
                "num_ctx": request.context_capacity,
            },
            "messages": messages(request),
        }
        if schema:
            payload["format"] = schema
        data = self._post(
            "/api/chat",
            payload,
            timeout=request.deadline_seconds,
            stream_request=request if payload["stream"] else None,
        )
        try:
            if data.get("done") is False or data.get("done_reason") in {"length", "content_filter"}:
                raise ValueError("Incomplete output")
            content = data["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError("Empty content")
            return content, output_metadata(content, request, data, local=True, schema=schema)
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderExecutionError("本机模型未返回有效正文。", outcome="known") from exc

    def generate_text(self, request):
        content, metadata = self._chat(request)
        return TextResult(text=content, provider=self.name, model=self.model, **metadata)

    def generate_structured(self, request, schema):
        try:
            content, metadata = self._chat(request, schema)
            data = json.loads(content)
            if not isinstance(data, dict):
                raise ValueError("Expected JSON object")
        except (ValueError, RecursionError) as exc:
            raise ProviderExecutionError(
                "本机模型未遵循结构化输出格式，请重试或换用支持该格式的模型。", outcome="known"
            ) from exc
        return StructuredResult(data=data, provider=self.name, model=self.model, **metadata)

    def generate_image(self, request):
        raise ProviderConfigurationError("纯本地文本模型不提供生图；此操作未发送到云端。")

    def embed(self, text):
        data = self._post(
            "/api/embed",
            {
                "model": self.model,
                "input": text,
                "truncate": False,
            },
            timeout=15,
        )
        try:
            vector = [float(value) for value in data["embeddings"][0]]
            if not vector or not all(math.isfinite(value) for value in vector):
                raise ValueError("Invalid vector")
            return vector
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderExecutionError("本机向量模型返回格式无效。", outcome="known") from exc
