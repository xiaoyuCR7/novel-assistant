"""Opt-in Chat Completions adapter. No redirects, proxy inheritance or key logging."""

import ipaddress
import json
from time import monotonic
from urllib.parse import urlsplit

import httpx

from novel_harness.ai.base import (
    MAX_RESPONSE_BYTES,
    ProviderExecutionError,
    StructuredResult,
    TextResult,
    check_input_budget,
    output_metadata,
    read_json_response,
)
from novel_harness.ai.prompt_payload import messages


def validate_api_url(value: str) -> str:
    url = urlsplit(value.strip())
    if not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("API 地址不能含账号、密码、查询参数或片段。")
    try:
        _ = url.port  # Access validates malformed/out-of-range ports.
    except ValueError as exc:
        raise ValueError("API 端口无效。") from exc
    loopback = url.hostname in {"localhost", "127.0.0.1", "::1"}
    if url.scheme != "https" and not (url.scheme == "http" and loopback):
        raise ValueError("外部 API 必须使用 HTTPS；HTTP 仅允许本机回环地址。")
    try:
        address = ipaddress.ip_address(url.hostname)
    except ValueError:
        address = None
    if address and not address.is_global and not loopback:
        raise ValueError("API 地址不能指向内网或链路本地地址。")
    base = value.strip().rstrip("/")
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
    return base


class CompatibleProvider:
    name = "api"

    def __init__(self, base_url, model, api_key, *, transport=None, attempt_observer=None):
        self.base_url = validate_api_url(base_url)
        self.model = model
        self._api_key = api_key
        self._transport = transport
        self.attempt_observer = attempt_observer

    def _send(self, request, schema=None):
        check_input_budget(request, schema)
        payload = {
            "model": self.model,
            "messages": messages(request, schema),
            "stream": bool(request.stream_preview and schema is None),
            request.output_parameter: request.output_token_budget,
        }
        if request.thinking_mode != "provider_default":
            payload["thinking"] = {"type": request.thinking_mode}
        if payload["stream"]:
            payload["stream_options"] = {"include_usage": True}
        if schema:
            payload["response_format"] = {"type": "json_object"}
        try:
            from novel_harness.ai.network_safety import SafeTransport

            deadline = monotonic() + request.deadline_seconds
            with httpx.Client(
                timeout=request.deadline_seconds,
                follow_redirects=False,
                trust_env=False,
                headers={"Accept-Encoding": "identity"},
                transport=self._transport or SafeTransport(request.deadline_seconds),
            ) as client:
                for attempt in range(2):
                    if self.attempt_observer is not None:
                        self.attempt_observer.before_send()
                    try:
                        with client.stream(
                            "POST",
                            self.base_url + "/chat/completions",
                            json=payload,
                            headers={"Authorization": "Bearer " + self._api_key},
                        ) as response:
                            response.raise_for_status()
                            if payload["stream"] and "text/event-stream" in response.headers.get(
                                "content-type", ""
                            ):
                                from novel_harness.ai.streaming import read_text_stream

                                data = read_text_stream(
                                    response, request, self.attempt_observer, deadline
                                )
                            else:
                                data = read_json_response(response, MAX_RESPONSE_BYTES, deadline)
                        break
                    except (httpx.ConnectError, httpx.ConnectTimeout):
                        if self.attempt_observer is not None:
                            self.attempt_observer.not_sent()
                        if attempt:
                            raise ProviderExecutionError(
                                "API 连接失败。", outcome="known", code="CONNECT_FAILED"
                            ) from None
                choice = data["choices"][0]
                if not isinstance(choice, dict):
                    raise ValueError("Invalid choice")
                if choice.get("finish_reason") in {"length", "content_filter"}:
                    from novel_harness.ai.base import incomplete_output

                    raise incomplete_output(choice["finish_reason"])
                content = choice["message"]["content"]
                if not isinstance(content, str) or not content.strip():
                    raise ValueError("Empty response")
                return content, output_metadata(content, request, data, schema=schema)
        except httpx.HTTPStatusError as exc:
            from novel_harness.ai.base import provider_http_error

            raise provider_http_error(exc.response.status_code) from None
        except (ValueError, KeyError, IndexError, TypeError, RecursionError):
            # Never echo a remote body/exception: it may contain an Authorization header.
            raise ProviderExecutionError(
                "API 请求失败，请检查地址、密钥、模型及服务商额度；未改动正文。",
                outcome="known",
                code="INVALID_PROVIDER_RESPONSE",
            ) from None
        except httpx.HTTPError:
            raise ProviderExecutionError(
                "API 请求结果未知；未自动重新发送。", code="PROVIDER_RESULT_UNKNOWN"
            ) from None

    def generate_text(self, request):
        content, metadata = self._send(request)
        return TextResult(text=content, provider=self.name, model=self.model, **metadata)

    def generate_structured(self, request, schema):
        content, metadata = self._send(request, schema)
        try:
            data = json.loads(content)
            if not isinstance(data, dict):
                raise ValueError("Not an object")
            return StructuredResult(data=data, provider=self.name, model=self.model, **metadata)
        except (ValueError, TypeError, RecursionError):
            error = ProviderExecutionError(
                "模型未返回有效 JSON，请换用支持 JSON 输出的模型后重试。",
                outcome="known", code="INVALID_STRUCTURED_OUTPUT",
            )
            error.metadata = {"provider": self.name, "model": self.model, **metadata}
            raise error from None

    def generate_image(self, request):
        raise ProviderExecutionError("当前 API 配置仅用于文本，未启用生图。")
