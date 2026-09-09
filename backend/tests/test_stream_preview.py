import json

import httpx
import pytest

from novel_harness.ai.base import AITextRequest, ProviderExecutionError
from novel_harness.ai.compatible_provider import CompatibleProvider


def test_api_stream_emits_scoped_preview_and_retains_final_usage():
    deltas = []

    class Observer:
        def before_send(self):
            pass

        def preview(self, value):
            deltas.append(value)

    frames = [
        {"choices": [{"delta": {"content": "第一段。"}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "第二段。"}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 8}},
    ]

    def send(request):
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content="".join("data: " + json.dumps(frame) + "\n\n" for frame in frames)
            + "data: [DONE]\n\n",
        )

    provider = CompatibleProvider(
        "https://example.com/v1",
        "model",
        "fake",
        transport=httpx.MockTransport(send),
        attempt_observer=Observer(),
    )
    result = provider.generate_text(
        AITextRequest(
            task="draft", developer_instruction="写作", user_prompt="生成", stream_preview=True
        )
    )
    assert result.text == "第一段。第二段。"
    assert deltas == ["第一段。", "第二段。"]
    assert result.usage_source == "provider"


def test_preview_isolation_and_late_append_cannot_revive_cancelled_output():
    from novel_harness.services.job_preview import PreviewRegistry

    registry = PreviewRegistry(max_chars=10)
    token = registry.begin("vault-a", "job", "draft")
    registry.append("vault-a", "job", token, "123456789012")
    assert registry.read("vault-b", "job")["text"] == ""
    assert len(registry.read("vault-a", "job")["text"]) == 10
    registry.drop("vault-a", "job")
    registry.append("vault-a", "job", token, "late")
    assert registry.read("vault-a", "job")["text"] == ""


@pytest.mark.parametrize("reason", ["length", "content_filter", "unknown"])
def test_local_incomplete_terminal_cannot_be_reopened(reason):
    from novel_harness.ai.local_provider import LocalProvider

    frames = [
        {"message": {"content": "truncated"}, "done": True, "done_reason": reason},
        {"message": {"content": " appended"}, "done": True, "done_reason": "stop"},
    ]
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200,
            headers={"content-type": "application/x-ndjson"},
            content="".join(json.dumps(frame) + "\n" for frame in frames),
        )
    )
    with pytest.raises(ProviderExecutionError):
        LocalProvider("http://127.0.0.1:11434", "model", transport=transport).generate_text(
            AITextRequest(
                task="draft", developer_instruction="write", user_prompt="x", stream_preview=True
            )
        )


@pytest.mark.parametrize("local", [True, False])
def test_post_terminal_delta_cannot_become_successful_text(local):
    from novel_harness.ai.local_provider import LocalProvider

    frames = (
        [
            {"message": {"content": "complete"}, "done": True},
            {"message": {"content": " AFTER_STOP"}, "done": False},
        ]
        if local
        else [
            {"choices": [{"delta": {"content": "complete"}, "finish_reason": "stop"}]},
            {"choices": [{"delta": {"content": " AFTER_STOP"}, "finish_reason": None}]},
        ]
    )
    body = "".join(
        (json.dumps(frame) + "\n" if local else "data: " + json.dumps(frame) + "\n\n")
        for frame in frames
    )
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200,
            headers={"content-type": "application/x-ndjson" if local else "text/event-stream"},
            content=body,
        )
    )
    provider = (
        LocalProvider("http://127.0.0.1:11434", "model", transport=transport)
        if local
        else CompatibleProvider("https://example.com/v1", "model", "fake", transport=transport)
    )
    with pytest.raises(ProviderExecutionError):
        provider.generate_text(
            AITextRequest(
                task="draft", developer_instruction="write", user_prompt="x", stream_preview=True
            )
        )
