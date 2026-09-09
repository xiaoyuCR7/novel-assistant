from types import SimpleNamespace

import pytest

from novel_harness.ai.base import AITextRequest, ProviderConfigurationError, ProviderExecutionError
from novel_harness.ai.openai_provider import OpenAIProvider


class FakeResponses:
    def __init__(self, error: Exception | None = None):
        self.calls: list[dict] = []
        self.error = error

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(output_text='{"scenes": [{"goal": "投递信件"}]}')


class FakeImages:
    def generate(self, **kwargs):
        return SimpleNamespace(data=[SimpleNamespace(b64_json="aW1hZ2U=")])


class FakeClient:
    def __init__(self, error: Exception | None = None):
        self.responses = FakeResponses(error)
        self.images = FakeImages()


def test_openai_structured_request_keeps_developer_instruction_and_schema():
    client = FakeClient()
    provider = OpenAIProvider(
        api_key="sk-test-secret",
        text_model="gpt-test",
        image_model="image-test",
        client=client,
    )
    request = AITextRequest(
        task="plan",
        developer_instruction="你是故事架构师，只返回场景计划。",
        user_prompt="规划第一章。",
        context={"contract": {"purpose": "收到遗书"}},
    )
    schema = {
        "type": "object",
        "properties": {"scenes": {"type": "array", "items": {"type": "object"}}},
        "required": ["scenes"],
        "additionalProperties": False,
    }

    result = provider.generate_structured(request, schema)

    call = client.responses.calls[0]
    assert call["model"] == "gpt-test"
    assert call["input"][0] == {
        "role": "developer",
        "content": "你是故事架构师，只返回场景计划。",
    }
    assert call["text"]["format"]["schema"] == schema
    assert result.data["scenes"][0]["goal"] == "投递信件"


def test_openai_provider_requires_key_and_redacts_provider_errors():
    request = AITextRequest(
        task="draft",
        developer_instruction="主笔",
        user_prompt="写正文",
        context={},
    )
    missing = OpenAIProvider(
        api_key=None,
        text_model="gpt-test",
        image_model="image-test",
        client=FakeClient(),
    )
    with pytest.raises(ProviderConfigurationError, match="OPENAI_API_KEY"):
        missing.generate_text(request)

    secret = "sk-test-secret"
    failing = OpenAIProvider(
        api_key=secret,
        text_model="gpt-test",
        image_model="image-test",
        client=FakeClient(RuntimeError(f"upstream included {secret}")),
    )
    with pytest.raises(ProviderExecutionError) as error:
        failing.generate_text(request)
    assert secret not in str(error.value)
