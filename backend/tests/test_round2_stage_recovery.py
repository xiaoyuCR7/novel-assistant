"""Guard interruptions retain send evidence and require bounded author-authorized replay."""

import pytest
from fastapi import HTTPException
from job_helpers import finish_job
from sqlalchemy import select

from novel_harness.ai.demo import DemoProvider
from novel_harness.db.job_models import AIJobStageAttempt
from novel_harness.db.models import GenerationArtifact
from novel_harness.services.job_stages import StageRunner
from novel_harness.services.job_store import JobStore


@pytest.mark.parametrize(
    "point, status, outcome",
    [
        ("before_send", "failed", "not_sent"),
        ("preview", "result_unknown", "unknown"),
        ("validation", "failed", "known"),
    ],
)
def test_guard_interruption_records_truthful_attempt(queued_job, point, status, outcome):
    store, job_id = queued_job
    fence = store.claim(job_id, "worker")
    interrupted = False
    error = HTTPException(409, detail={"code": "PROVIDER_CHANGED"})

    def guard():
        if interrupted:
            raise error

    def invoke(observer):
        nonlocal interrupted
        if point == "before_send":
            interrupted = True
        observer.before_send()
        if point == "preview":
            interrupted = True
            observer.preview("private partial result")
        return {"text": "complete"}

    def validate(value):
        if point == "validation":
            raise error
        return value

    with pytest.raises(HTTPException) as caught:
        StageRunner(store, fence, guard).run("chat", {}, invoke, validate)
    assert caught.value is error
    with store.database.job_session_scope() as session:
        attempt = session.get(AIJobStageAttempt, (job_id, "chat", 1))
        assert (attempt.status, attempt.outcome, attempt.error_code) == (
            status,
            outcome,
            "PROVIDER_CHANGED",
        )
        assert attempt.artifact_id is None
    # The executor owns the pause reason; recording evidence must not hide it.
    assert store.read(job_id)["status"] == "running"
    store.pause(fence, "PROVIDER_CHANGED")
    assert store.read(job_id)["recovery_reason"] == "PROVIDER_CHANGED"


@pytest.mark.parametrize("change", ["cancel", "lost_fence", "completed"])
def test_interruption_does_not_overwrite_cancellation_new_owner_or_artifact(queued_job, change):
    store, job_id = queued_job
    fence = store.claim(job_id, "worker")
    error = HTTPException(409, detail={"code": "PROVIDER_CHANGED"})

    def invoke(observer):
        observer.before_send()
        if change == "cancel":
            store.cancel(job_id)
        elif change == "lost_fence":
            store.recover("new-owner")
        else:
            store.finish_attempt(fence, "chat", 1, {"text": "saved"})
        raise error

    with pytest.raises(HTTPException) as caught:
        StageRunner(store, fence).run("chat", {}, invoke, dict)
    assert caught.value is error
    with store.database.job_session_scope() as session:
        attempt = session.get(AIJobStageAttempt, (job_id, "chat", 1))
        if change == "completed":
            assert (attempt.status, attempt.outcome) == ("succeeded", "known")
            assert session.get(GenerationArtifact, attempt.artifact_id).data == {"text": "saved"}
        elif change == "cancel":
            assert (attempt.status, attempt.outcome) == ("dispatched", None)
        else:
            assert (attempt.status, attempt.outcome) == ("result_unknown", "unknown")
        assert attempt.error_code is None


def legacy_paused_dispatch(store, job_id):
    fence = store.claim(job_id, "worker")
    number = store.begin_attempt(fence, "chat", {"prompt": "same"})
    store.dispatch(fence, "chat", number)
    store.pause(fence, "PROVIDER_CHANGED")
    return store.read(job_id)


def test_legacy_dispatched_resume_requires_confirmation_from_stage_evidence(queued_job):
    store, job_id = queued_job
    paused = legacy_paused_dispatch(store, job_id)
    assert paused["replacement_requires_confirmation"] is True
    with pytest.raises(HTTPException) as caught:
        store.resume(job_id, "unconfirmed", paused["control_revision"], False)
    assert caught.value.detail["code"] == "UNKNOWN_RESULT_CONFIRMATION_REQUIRED"
    assert store.read(job_id) == paused


def test_legacy_confirmed_resume_normalizes_and_authorizes_only_exact_next_attempt(queued_job):
    store, job_id = queued_job
    paused = legacy_paused_dispatch(store, job_id)
    response = store.resume(job_id, "confirmed", paused["control_revision"], True)
    assert store.resume(job_id, "confirmed", paused["control_revision"], True) == response
    with store.database.job_session_scope() as session:
        attempt = session.get(AIJobStageAttempt, (job_id, "chat", 1))
        assert (attempt.status, attempt.outcome) == ("result_unknown", "unknown")
    store.recover("next-owner")
    fence = store.claim(job_id, "next-owner")
    with pytest.raises(HTTPException) as caught:
        store.begin_attempt(fence, "chat", {"prompt": "changed"})
    assert caught.value.detail["code"] == "STAGE_INPUT_CHANGED"
    number = store.begin_attempt(fence, "chat", {"prompt": "same"})
    assert number == 2
    store.dispatch(fence, "chat", number)
    with pytest.raises(HTTPException) as caught:
        store.begin_attempt(fence, "chat", {"prompt": "same"})
    assert caught.value.detail["code"] == "STAGE_REPLAY_NOT_AUTHORIZED"


def test_confirmed_legacy_resume_still_validates_original_source(queued_job):
    store, job_id = queued_job
    paused = legacy_paused_dispatch(store, job_id)

    def invalid_source(session, control):
        raise HTTPException(409, detail={"code": "SOURCE_CHANGED"})

    with pytest.raises(HTTPException) as caught:
        store.resume(job_id, "confirmed", paused["control_revision"], True, invalid_source)
    assert caught.value.detail["code"] == "SOURCE_CHANGED"
    assert store.read(job_id) == paused
    with store.database.job_session_scope() as session:
        assert session.get(AIJobStageAttempt, (job_id, "chat", 1)).status == "dispatched"


def test_actual_executor_guard_interruption_can_resume_after_restoring_settings(client, project):
    settings = client.app.state.model_settings
    budget = settings.default["output_token_budget"]
    calls = []

    class InterruptedProvider:
        # Deliberately not a DemoProvider: the provider owns before_send/preview.
        attempt_observer = None

        def generate_text(self, request):
            self.attempt_observer.before_send()
            calls.append("interrupted")
            settings.default["output_token_budget"] += 1
            self.attempt_observer.preview("private provider partial")
            raise AssertionError("The preview guard must interrupt")

    class CountedDemo(DemoProvider):
        def generate_text(self, request):
            calls.append("resumed")
            return super().generate_text(request)

    client.app.state.ai_provider = InterruptedProvider()
    receipt = client.post(
        f"/api/v1/projects/{project['id']}/ai/jobs",
        json={"project_id": project["id"], "task_type": "chat", "instructions": "discuss"},
        headers={"Idempotency-Key": "guard-interruption"},
    )
    paused = finish_job(client, receipt).json()
    assert (paused["status"], paused["recovery_reason"]) == (
        "recovery_required",
        "PROVIDER_CHANGED",
    )
    assert paused["error_code"] == "PROVIDER_CHANGED"
    assert paused["replacement_requires_confirmation"] is True
    database = client.app.state.vault_registry.require(project["id"]).database
    store = JobStore(database, project["id"])
    with database.job_session_scope() as session:
        attempt = session.scalar(
            select(AIJobStageAttempt).where(
                AIJobStageAttempt.job_id == paused["id"],
                AIJobStageAttempt.stage_key == "chat",
            )
        )
        assert (attempt.status, attempt.outcome, attempt.error_code) == (
            "result_unknown",
            "unknown",
            "PROVIDER_CHANGED",
        )
    payload = {"expected_control_revision": paused["control_revision"], "confirm_unknown": True}
    assert (
        client.post(
            paused["status_url"] + "/resume",
            json=payload,
            headers={"Idempotency-Key": "still-changed"},
        ).json()["detail"]["code"]
        == "PROVIDER_CHANGED"
    )
    settings.default["output_token_budget"] = budget
    client.app.state.ai_provider = CountedDemo()
    denied = client.post(
        paused["status_url"] + "/resume",
        json={**payload, "confirm_unknown": False},
        headers={"Idempotency-Key": "unconfirmed"},
    )
    assert denied.status_code == 409
    assert denied.json()["detail"]["code"] == "UNKNOWN_RESULT_CONFIRMATION_REQUIRED"
    assert calls == ["interrupted"]
    assert store.read(paused["id"])["status"] == "recovery_required"
    resumed = client.post(
        paused["status_url"] + "/resume", json=payload, headers={"Idempotency-Key": "confirmed"}
    )
    assert finish_job(client, resumed).json()["status"] == "succeeded"
    assert calls == ["interrupted", "resumed"]
    with database.job_session_scope() as session:
        attempts = session.scalars(
            select(AIJobStageAttempt)
            .where(
                AIJobStageAttempt.job_id == paused["id"],
                AIJobStageAttempt.stage_key == "chat",
            )
            .order_by(AIJobStageAttempt.attempt_no)
        ).all()
        assert [(a.attempt_no, a.status) for a in attempts] == [
            (1, "result_unknown"),
            (2, "succeeded"),
        ]
        assert attempts[0].input_hash == attempts[1].input_hash
