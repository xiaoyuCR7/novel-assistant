from importlib import import_module

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import StatementError

from novel_harness.ai.base import ProviderExecutionError
from novel_harness.db.job_models import AIJobStageAttempt
from novel_harness.db.models import GenerationArtifact


def test_unknown_dispatch_is_not_reclaimed(queued_job):
    store, job_id = queued_job
    fence = store.claim(job_id, "epoch-a")
    number = store.begin_attempt(fence, "draft", {"prompt": "开场"})
    store.dispatch(fence, "draft", number)
    store.recover("epoch-b")
    assert store.read(job_id)["recovery_reason"] == "result_unknown"
    assert store.claim(job_id, "epoch-b") is None


def test_completed_stage_reused_after_restart(queued_job):
    store, job_id = queued_job
    runner_type = import_module("novel_harness.services.job_stages").StageRunner
    calls = []

    def invoke(observer):
        observer.before_send()
        calls.append(1)
        return {"text": "正文"}

    fence = store.claim(job_id, "one")
    first = runner_type(store, fence).run("draft", {"prompt": "正文"}, invoke, dict)
    store.recover("two")
    fence2 = store.claim(job_id, "two")
    assert runner_type(store, fence2).run("draft", {"prompt": "正文"}, invoke, dict) == first
    assert calls == [1]
    with pytest.raises(HTTPException):
        store.publish(fence, lambda session, job: None)
    store.publish(fence2, lambda session, job: setattr(job, "result", first))
    assert store.read(job_id)["status"] == "succeeded"


def test_invalid_output_does_not_save_artifact(queued_job):
    store, job_id = queued_job
    runner_type = import_module("novel_harness.services.job_stages").StageRunner
    fence = store.claim(job_id, "one")

    def invoke(observer):
        observer.before_send()
        return {"invalid": True}

    def validate(value):
        raise ValueError("invalid output")

    with pytest.raises(ValueError):
        runner_type(store, fence).run("draft", {}, invoke, validate)
    with store.database.job_session_scope() as session:
        assert session.scalar(select(GenerationArtifact)) is None
        assert session.scalar(select(AIJobStageAttempt)).status == "failed"


def test_only_named_known_validation_error_stays_nonterminal_for_bounded_repair(
    queued_job,
):
    store, job_id = queued_job
    runner_type = import_module("novel_harness.services.job_stages").StageRunner
    fence = store.claim(job_id, "one")

    def invoke(observer):
        observer.before_send()
        return {
            "data": {"private": "must not be persisted"},
            "provider": "test-provider",
            "model": "test-model",
            "usage": {"input_tokens": 17, "output_tokens": 9},
            "usage_source": "provider",
        }

    def validate(value):
        raise ProviderExecutionError(
            "missing evidence", outcome="known", code="MISSING_SUMMARY_EVIDENCE"
        )

    with pytest.raises(ProviderExecutionError):
        runner_type(store, fence).run(
            "chapter_summary",
            {},
            invoke,
            validate,
            nonterminal_known_codes={"MISSING_SUMMARY_EVIDENCE"},
        )

    job = store.read(job_id)
    assert job["status"] == "running"
    metrics = job["result"]["execution"]["stages"]["chapter_summary"]
    assert metrics["usage"] == {"input_tokens": 17, "output_tokens": 9}
    assert metrics["status"] == "failed"
    assert metrics["error_code"] == "MISSING_SUMMARY_EVIDENCE"
    assert "data" not in metrics
    with store.database.job_session_scope() as session:
        attempt = session.scalar(select(AIJobStageAttempt))
        assert attempt.status == "failed"
        assert attempt.error_code == "MISSING_SUMMARY_EVIDENCE"


def test_checkpoint_rolls_back_with_output(queued_job):
    store, job_id = queued_job
    fence = store.claim(job_id, "one")
    number = store.begin_attempt(fence, "draft", {})
    store.dispatch(fence, "draft", number)
    with pytest.raises(StatementError):
        store.finish_attempt(fence, "draft", number, {"bad": object()})
    with store.database.job_session_scope() as session:
        assert session.scalar(select(GenerationArtifact)) is None
        assert session.scalar(select(AIJobStageAttempt)).status == "dispatched"
