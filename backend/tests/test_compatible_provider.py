import json

import httpx
import pytest

from novel_harness.ai.base import AITextRequest, ProviderError
from novel_harness.ai.compatible_provider import CompatibleProvider


def test_compatible_request_and_response_contract():
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "继续讨论"}, "finish_reason": "stop"}]}
        )

    provider = CompatibleProvider(
        "https://example.com/v1", "model", "fake-secret", transport=httpx.MockTransport(handle)
    )
    result = provider.generate_text(
        AITextRequest(
            task="chat",
            developer_instruction="帮助写作",
            user_prompt="开篇",
            context={"novel": "本项目"},
        )
    )
    assert result.text == "继续讨论"
    assert str(seen[0].url) == "https://example.com/v1/chat/completions"
    assert seen[0].headers["authorization"] == "Bearer fake-secret"
    assert "本项目" in json.loads(seen[0].content)["messages"][1]["content"]


def test_redirect_is_not_followed_and_secret_not_in_error():
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(302, headers={"Location": "https://evil.example"})

    provider = CompatibleProvider(
        "https://example.com/v1", "model", "fake-secret", transport=httpx.MockTransport(handle)
    )
    with pytest.raises(ProviderError) as failure:
        provider.generate_text(
            AITextRequest(task="chat", developer_instruction="", user_prompt="test")
        )
    assert len(seen) == 1
    assert "fake-secret" not in str(failure.value)
