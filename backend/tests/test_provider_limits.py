import json

import httpx
import pytest

from novel_harness.ai.base import AITextRequest, ContextBudgetError, ProviderExecutionError
from novel_harness.ai.compatible_provider import CompatibleProvider
from novel_harness.ai.local_provider import LocalProvider


@pytest.mark.parametrize("kind", ["local", "api"])
def test_output_limit_and_actual_usage_are_retained(kind):
    payloads = []

    def send(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "message": {"content": "ok"},
                "prompt_eval_count": 7,
                "eval_count": 2,
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 2},
            },
        )

    transport = httpx.MockTransport(send)
    provider = (
        LocalProvider("http://127.0.0.1:11434", "model", transport=transport)
        if kind == "local"
        else CompatibleProvider("https://example.com/v1", "model", "fake", transport=transport)
    )
    result = provider.generate_text(
        AITextRequest(
            task="draft",
            developer_instruction="write",
            user_prompt="novel",
            output_token_budget=500,
        )
    )
    assert (
        payloads[0]["options"]["num_predict"] if kind == "local" else payloads[0]["max_tokens"]
    ) == 500
    assert result.usage == {"input_tokens": 7, "output_tokens": 2}
    assert result.usage_source == "provider"
    assert json.loads(payloads[0]["messages"][1]["content"]) == {
        "request": "novel",
        "reference_data": {},
    }


def test_capacity_and_excessive_output_are_rejected_before_publication():
    provider = CompatibleProvider(
        "https://example.com/v1",
        "model",
        "fake",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"choices": [{"message": {"content": "x" * 10000}}]})
        ),
    )
    with pytest.raises(ContextBudgetError):
        provider.generate_text(
            AITextRequest(
                task="chat",
                developer_instruction="write",
                user_prompt="x" * 3000,
                context_capacity=1024,
            )
        )
    with pytest.raises(ProviderExecutionError):
        provider.generate_text(
            AITextRequest(
                task="chat", developer_instruction="write", user_prompt="x", output_token_budget=256
            )
        )


def test_missing_usage_still_enforces_estimated_output_limit():
    provider = CompatibleProvider(
        "https://example.com/v1",
        "model",
        "fake",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json={"usage": None, "choices": [{"message": {"content": "中" * 1000}}]}
            )
        ),
    )
    with pytest.raises(ProviderExecutionError):
        provider.generate_text(
            AITextRequest(
                task="chat", developer_instruction="write", user_prompt="x", output_token_budget=256
            )
        )


def test_connection_test_uses_saved_limits(client, monkeypatch):
    from novel_harness.ai.demo import DemoProvider

    captured = []

    class Provider(DemoProvider):
        def generate_text(self, request):
            captured.append(request)
            return super().generate_text(request)

    monkeypatch.setattr("novel_harness.api.routes.settings.selected_provider", lambda _: Provider())
    assert (
        client.put(
            "/api/v1/settings/model",
            json={
                "mode": "demo",
                "output_parameter": "max_completion_tokens",
                "output_token_budget": 256,
                "context_capacity": 8192,
                "deadline_seconds": 7,
                "thinking_mode": "disabled",
            },
        ).status_code
        == 200
    )
    assert client.post("/api/v1/settings/model/test").status_code == 200
    assert captured[0].output_parameter == "max_completion_tokens"
    assert captured[0].output_token_budget == 256
    assert captured[0].deadline_seconds == 7
    assert captured[0].thinking_mode == "disabled"


def test_compatible_provider_only_sends_explicit_thinking_mode():
    payloads = []

    def send(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    provider = CompatibleProvider(
        "https://example.com/v1", "model", "fake", transport=httpx.MockTransport(send)
    )
    provider.generate_text(
        AITextRequest(task="chat", developer_instruction="write", user_prompt="x")
    )
    provider.generate_text(
        AITextRequest(
            task="chat",
            developer_instruction="write",
            user_prompt="x",
            thinking_mode="disabled",
        )
    )
    assert "thinking" not in payloads[0]
    assert payloads[1]["thinking"] == {"type": "disabled"}


def test_partial_usage_cannot_bypass_output_limit():
    provider = CompatibleProvider(
        "https://example.com/v1",
        "model",
        "fake",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "usage": {"completion_tokens": 5000},
                    "choices": [{"message": {"content": "ok"}}],
                },
            )
        ),
    )
    with pytest.raises(ProviderExecutionError):
        provider.generate_text(
            AITextRequest(
                task="chat", developer_instruction="write", user_prompt="x", output_token_budget=256
            )
        )


@pytest.mark.parametrize("kind", ["local", "api"])
@pytest.mark.parametrize("invalid", ["nested", "shape", "compressed"])
def test_complete_invalid_response_is_known_and_compression_rejected(kind, invalid):
    import gzip

    content = "[" * 5000 + "0" + "]" * 5000 if invalid == "nested" else "ok"
    data = {"message": {"content": content}, "choices": [{"message": {"content": content}}]}
    if invalid == "shape":
        data = {"message": None, "choices": [None]}

    def send(request):
        if invalid == "compressed":
            return httpx.Response(
                200,
                headers={"content-encoding": "gzip"},
                stream=httpx.ByteStream(gzip.compress(json.dumps(data).encode())),
            )
        return httpx.Response(200, json=data)

    transport = httpx.MockTransport(send)
    provider = (
        LocalProvider("http://127.0.0.1:11434", "model", transport=transport)
        if kind == "local"
        else CompatibleProvider("https://example.com/v1", "model", "fake", transport=transport)
    )
    request = AITextRequest(
        task="plan", developer_instruction="write", user_prompt="x", output_token_budget=16384
    )
    with pytest.raises(ProviderExecutionError) as caught:
        provider.generate_structured(
            request, {}
        ) if invalid == "nested" else provider.generate_text(request)
    assert caught.value.outcome == "known"


def test_structured_budget_counts_exact_schema_message_encoding():
    from novel_harness.ai.base import check_input_budget
    from novel_harness.schemas.summaries import GeneratedSummary
    from novel_harness.services.context import estimate_tokens

    captured = []

    def send(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    provider = CompatibleProvider(
        "https://example.com/v1", "model", "fake", transport=httpx.MockTransport(send)
    )
    request = AITextRequest(
        task="chapter_summary", developer_instruction="summarize", user_prompt="source"
    )
    schema = GeneratedSummary.model_json_schema()
    provider.generate_structured(request, schema)
    actual = 64 + estimate_tokens(json.dumps(captured[0]["messages"], ensure_ascii=False))
    assert check_input_budget(request, schema) == actual
