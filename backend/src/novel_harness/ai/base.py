"""Provider-neutral AI request and result contracts."""

from __future__ import annotations

from time import monotonic
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

MAX_RESPONSE_BYTES = 16 * 1024 * 1024


def require_identity_encoding(response):
    # httpx decodes before yielding iter_bytes; reject compression before allocation.
    if response.headers.get("content-encoding", "identity").strip().lower() not in {"", "identity"}:
        raise ValueError("Compressed provider responses are not supported")


def read_json_response(response, limit: int, deadline=None) -> dict:
    """Bound decoded provider bytes before parsing a complete JSON response."""
    import json

    require_identity_encoding(response)
    body = bytearray()
    for chunk in response.iter_bytes(chunk_size=64 * 1024):
        if deadline is not None and monotonic() > deadline:
            raise ProviderExecutionError(
                "请求总期限已到；结果未知，不自动重发。", code="PROVIDER_RESULT_UNKNOWN"
            )
        if len(body) + len(chunk) > limit:
            raise ValueError("Provider response exceeds size limit")
        body.extend(chunk)
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ValueError("Provider response must be an object")
    return data


class ProviderError(RuntimeError):
    pass


class ProviderConfigurationError(ProviderError):
    pass


class ProviderExecutionError(ProviderError):
    def __init__(self, message, *, outcome="unknown", code="PROVIDER_ERROR"):
        super().__init__(message)
        self.outcome = outcome
        self.code = code


class ContextBudgetError(ProviderError):
    pass


PROVIDER_FAILURE_MESSAGES = {
    "SPENDING_PRICE_REQUIRED": "请在费用中心设置模型单价，并核对未定价记录后继续。",
    "SPENDING_LIMIT_REACHED": "本次请求预计超过本地费用限额，请在费用中心查看记录或调整预算。",
    "PROVIDER_AUTH_FAILED": "API 认证失败，请检查密钥和访问权限后手动恢复。",
    "PROVIDER_BALANCE_INSUFFICIENT": "API 账户余额不足，请充值或调整模型设置后继续。",
    "PROVIDER_RATE_LIMITED": "API 服务商限流，请稍后手动恢复；未自动重发。",
    "PROVIDER_UNAVAILABLE": "API 服务暂时不可用，请稍后手动恢复；未自动重发。",
    "OUTPUT_TRUNCATED": "模型输出被截断，可能是输出预算被正文或思考耗尽。请调整设置后新建任务。",
    "CONTENT_FILTERED": "API 服务商过滤了本次输出，请调整任务后新建；未保存不完整候选。",
}


def incomplete_output(reason):
    code = "OUTPUT_TRUNCATED" if reason == "length" else "CONTENT_FILTERED"
    return ProviderExecutionError(PROVIDER_FAILURE_MESSAGES[code], outcome="known", code=code)


def provider_http_error(status):
    code = {401: "PROVIDER_AUTH_FAILED", 403: "PROVIDER_AUTH_FAILED",
            402: "PROVIDER_BALANCE_INSUFFICIENT", 429: "PROVIDER_RATE_LIMITED"}.get(status)
    if code is None and status >= 500:
        code = "PROVIDER_UNAVAILABLE"
    return ProviderExecutionError(
        PROVIDER_FAILURE_MESSAGES.get(code, "API 请求失败，请检查地址、模型和请求参数。"),
        outcome="known", code=code or "INVALID_PROVIDER_RESPONSE",
    )


class ExecutionLimits(BaseModel):
    output_token_budget: int = Field(default=4096, ge=256, le=65536)
    context_capacity: int = Field(default=32768, ge=1024, le=1048576)
    deadline_seconds: float = Field(default=180, ge=1, le=600)
    output_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    thinking_mode: Literal["provider_default", "disabled", "enabled"] = "provider_default"


class AITextRequest(ExecutionLimits):
    task: str
    developer_instruction: str
    user_prompt: str
    context: dict[str, Any] = Field(default_factory=dict)
    token_budget: int | None = None
    stream_preview: bool = False


def estimate_input_tokens(request: AITextRequest, schema=None, *, schema_in_system=True) -> int:
    import json

    from novel_harness.ai.prompt_payload import messages
    from novel_harness.services.context import estimate_tokens

    return 64 + estimate_tokens(
        json.dumps(messages(request, schema if schema_in_system else None), ensure_ascii=False)
        + (json.dumps(schema, ensure_ascii=False) if schema and not schema_in_system else "")
    )


def check_input_budget(request: AITextRequest, schema=None, *, schema_in_system=True) -> int:
    tokens = estimate_input_tokens(request, schema, schema_in_system=schema_in_system)
    if request.token_budget is not None and tokens > request.token_budget:
        raise ContextBudgetError(
            "当前任务与必要参考超过输入预算，请缩短任务、分章处理或提高预算；未裁剪硬约束。"
        )
    if tokens + request.output_token_budget > request.context_capacity:
        raise ContextBudgetError("输入和预留输出超过模型上下文容量，请调整预算或模型容量。")
    return tokens


def output_metadata(text, request, data, *, local=False, schema=None):
    from novel_harness.services.context import estimate_tokens

    provider_usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    usage = (
        {"input_tokens": data.get("prompt_eval_count"), "output_tokens": data.get("eval_count")}
        if local
        else {
            "input_tokens": provider_usage.get("prompt_tokens"),
            "output_tokens": provider_usage.get("completion_tokens"),
        }
    )
    available = {key: type(value) is int and value >= 0 for key, value in usage.items()}
    actual = all(available.values())
    output_tokens = usage["output_tokens"] if available["output_tokens"] else estimate_tokens(text)
    if len(text) > request.output_token_budget * 8 or output_tokens > request.output_token_budget:
        raise ProviderExecutionError(
            "模型输出超过配置上限。", outcome="known", code="OUTPUT_LIMIT_EXCEEDED"
        )
    return {
        "usage": {
            "input_tokens": usage["input_tokens"]
            if available["input_tokens"]
            else check_input_budget(request, schema, schema_in_system=not local),
            "output_tokens": output_tokens,
        },
        "usage_source": "provider"
        if actual
        else "mixed"
        if any(available.values())
        else "estimated",
    }


class AIImageRequest(BaseModel):
    task: str
    prompt: str
    size: str = "1024x1024"
    reference_description: str = ""


class TextResult(BaseModel):
    text: str
    provider: str
    model: str
    usage: dict[str, int] = Field(default_factory=dict)
    usage_source: Literal["provider", "estimated", "mixed"] = "estimated"


class StructuredResult(BaseModel):
    data: dict[str, Any]
    provider: str
    model: str
    usage: dict[str, int] = Field(default_factory=dict)
    usage_source: Literal["provider", "estimated", "mixed"] = "estimated"


class ImageResult(BaseModel):
    data: bytes
    mime_type: str = "image/png"
    provider: str
    model: str


class AIProvider(Protocol):
    name: str

    def generate_text(self, request: AITextRequest) -> TextResult: ...

    def generate_structured(
        self, request: AITextRequest, schema: dict[str, Any]
    ) -> StructuredResult: ...

    def generate_image(self, request: AIImageRequest) -> ImageResult: ...
