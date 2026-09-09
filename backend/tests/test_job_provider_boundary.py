import httpx
import pytest
from sqlalchemy import select, text

from novel_harness.ai.base import AITextRequest, ProviderExecutionError
from novel_harness.ai.compatible_provider import CompatibleProvider
from novel_harness.ai.local_provider import LocalProvider
from novel_harness.db.job_models import AIJobStageAttempt
from novel_harness.services.job_stages import StageRunner


@pytest.mark.parametrize("kind", ["local", "api"])
@pytest.mark.parametrize(
    "failure, expected, sends",
    [
        (httpx.ConnectError, "succeeded", 2),
        (httpx.ReadTimeout, "recovery_required", 1),
        (httpx.WriteTimeout, "recovery_required", 1),
    ],
)
def test_send_evidence_and_safe_retry(queued_job, kind, failure, expected, sends):
    store, job_id = queued_job
    calls = []

    def handle(request):
        with store.database.job_session_scope() as session:
            session.execute(text("BEGIN IMMEDIATE"))  # No write lock held during HTTP.
            attempt = session.scalar(
                select(AIJobStageAttempt).order_by(AIJobStageAttempt.attempt_no.desc())
            )
            assert attempt.status == "dispatched"
        calls.append(1)
        if len(calls) == 1:
            raise failure("secret must not be stored", request=request)
        return httpx.Response(
            200,
            json={"message": {"content": "正文"}, "choices": [{"message": {"content": "正文"}}]},
        )

    def invoke(observer):
        kwargs = dict(transport=httpx.MockTransport(handle), attempt_observer=observer)
        provider = (
            LocalProvider("http://127.0.0.1:11434", "local-model", **kwargs)
            if kind == "local"
            else CompatibleProvider("https://example.com/v1", "model", "fake-key", **kwargs)
        )
        return provider.generate_text(
            AITextRequest(
                task="chat",
                developer_instruction="",
                user_prompt="正文",
            )
        ).model_dump()

    fence = store.claim(job_id, "owner")
    runner = StageRunner(store, fence)
    if expected == "succeeded":
        assert runner.run("draft", {}, invoke, dict)["text"] == "正文"
        store.publish(fence, lambda session, job: None)
    else:
        with pytest.raises(ProviderExecutionError) as error:
            runner.run("draft", {}, invoke, dict)
        assert error.value.outcome == "unknown"
    assert len(calls) == sends
    assert store.read(job_id)["status"] == expected


def test_complete_invalid_response_is_known():
    provider = CompatibleProvider(
        "https://example.com",
        "model",
        "fake-key",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
    )
    with pytest.raises(ProviderExecutionError) as error:
        provider.generate_text(AITextRequest(task="chat", developer_instruction="", user_prompt=""))
    assert error.value.outcome == "known"
