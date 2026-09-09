import pytest

from novel_harness.ai.prompts import PROMPT_VERSION
from novel_harness.db.job_models import AIJobControl, AIJobStageAttempt
from novel_harness.db.models import AIJob


def _enqueue(store, key):
    command = {
        "project_id": store.project_id,
        "chapter_id": None,
        "task_type": "chat",
        "instructions": "owned reconciliation",
        "token_budget": 12_000,
        "expected_revision": None,
    }
    return store.enqueue(
        command,
        key,
        lambda session: {
            "source_snapshot": {},
            "provider_identity": {"mode": "demo"},
            "embedding_identity": None,
        },
    )["id"]


def _attempt(store, fence, status, *, stage="chat", outcome=None):
    number = store.begin_attempt(fence, stage, {"stage": stage})
    if status == "prepared":
        return number
    if status == "skipped":
        store.finish_attempt(fence, stage, number, {"value": stage}, skipped=True)
        return number
    store.dispatch(fence, stage, number)
    if status == "dispatched":
        return number
    if status == "succeeded":
        store.finish_attempt(fence, stage, number, {"value": stage})
        return number
    store.fail_attempt(fence, stage, number, "BROKEN", outcome, terminal=False)
    return number


def test_reconcile_owned_only_converges_the_matching_epoch(queued_job):
    store, job_id = queued_job
    fence = store.claim(job_id, "current")
    original_revision = fence.control_revision

    store.recover("current")
    assert store.read(job_id)["status"] == "running"
    store.reconcile_owned("other")
    assert store.read(job_id)["status"] == "running"

    store.reconcile_owned("current")
    current = store.read(job_id)
    assert current["status"] == "queued"
    assert current["control_revision"] == original_revision + 1
    with store.database.job_session_scope() as session:
        assert session.get(AIJobControl, job_id).worker_epoch is None


@pytest.mark.parametrize("cancel_source", ["job", "control"])
def test_reconcile_owned_finishes_cancellation_before_other_classification(
    queued_job, cancel_source
):
    store, job_id = queued_job
    store.claim(job_id, "owner")
    with store.write() as session:
        job = session.get(AIJob, job_id)
        control = session.get(AIJobControl, job_id)
        job.prompt_version = "unsupported"
        if cancel_source == "job":
            job.status = "cancel_requested"
        else:
            control.cancel_requested = True

    store.reconcile_owned("owner")
    assert store.read(job_id)["status"] == "cancelled"


@pytest.mark.parametrize("status", ["dispatched", "result_unknown"])
def test_reconcile_owned_pauses_send_evidence_as_unknown(queued_job, status):
    store, job_id = queued_job
    fence = store.claim(job_id, "owner")
    number = _attempt(
        store,
        fence,
        status,
        outcome="unknown" if status == "result_unknown" else None,
    )

    store.reconcile_owned("owner")

    current = store.read(job_id)
    assert current["status"] == "recovery_required"
    assert current["recovery_reason"] == "result_unknown"
    with store.database.job_session_scope() as session:
        attempt = session.get(AIJobStageAttempt, (job_id, "chat", number))
        assert (attempt.status, attempt.outcome) == ("result_unknown", "unknown")


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (None, None),
        ("prepared", None),
        ("succeeded", None),
        ("skipped", None),
        ("failed", "not_sent"),
    ],
)
def test_reconcile_owned_requeues_only_safe_evidence_without_rewriting_attempt(
    queued_job, status, outcome
):
    store, job_id = queued_job
    fence = store.claim(job_id, "owner")
    number = _attempt(store, fence, status, outcome=outcome) if status else None
    before = None
    if number:
        with store.database.job_session_scope() as session:
            attempt = session.get(AIJobStageAttempt, (job_id, "chat", number))
            before = (
                attempt.stage_key,
                attempt.attempt_no,
                attempt.input_hash,
                attempt.status,
                attempt.outcome,
            )

    store.reconcile_owned("owner")

    assert store.read(job_id)["status"] == "queued"
    if number:
        with store.database.job_session_scope() as session:
            attempt = session.get(AIJobStageAttempt, (job_id, "chat", number))
            assert (
                attempt.stage_key,
                attempt.attempt_no,
                attempt.input_hash,
                attempt.status,
                attempt.outcome,
            ) == before


def test_reconcile_owned_uses_only_latest_attempt_per_stage(queued_job):
    store, job_id = queued_job
    fence = store.claim(job_id, "owner")
    first = _attempt(store, fence, "failed", outcome="not_sent")
    with store.write() as session:
        control = session.get(AIJobControl, job_id)
        control.authorized_stage = "chat"
        control.authorized_attempt_no = first + 1
    second = store.begin_attempt(fence, "chat", {"stage": "chat"})
    store.dispatch(fence, "chat", second)
    store.finish_attempt(fence, "chat", second, {"value": "done"})

    store.reconcile_owned("owner")

    assert store.read(job_id)["status"] == "queued"


def test_reconcile_owned_marks_known_failure_failed(queued_job):
    store, job_id = queued_job
    fence = store.claim(job_id, "owner")
    _attempt(store, fence, "failed", outcome="known")

    store.reconcile_owned("owner")

    current = store.read(job_id)
    assert current["status"] == "failed"
    assert current["recovery_reason"] == "stage_failed"


@pytest.mark.parametrize("safe_exception", ["authorized", "completed_embedding"])
def test_reconcile_owned_requeues_known_failure_when_retry_is_already_safe(
    queued_job, safe_exception
):
    store, job_id = queued_job
    fence = store.claim(job_id, "owner")
    stage = "chat" if safe_exception == "authorized" else "context_embedding"
    number = _attempt(store, fence, "failed", stage=stage, outcome="known")
    with store.write() as session:
        control = session.get(AIJobControl, job_id)
        if safe_exception == "authorized":
            control.authorized_stage = stage
            control.authorized_attempt_no = number + 1
        else:
            control.context_ready = True

    store.reconcile_owned("owner")

    assert store.read(job_id)["status"] == "queued"


@pytest.mark.parametrize("unsupported", ["prompt", "control"])
def test_reconcile_owned_pauses_unsupported_format(queued_job, unsupported):
    store, job_id = queued_job
    store.claim(job_id, "owner")
    with store.write() as session:
        if unsupported == "prompt":
            session.get(AIJob, job_id).prompt_version = "future"
        else:
            session.get(AIJobControl, job_id).format_version = 99

    store.reconcile_owned("owner")

    current = store.read(job_id)
    assert current["status"] == "recovery_required"
    assert current["recovery_reason"] == "unsupported_format"


def test_reconcile_owned_marks_control_less_running_job_unrecoverable(queued_job):
    store, _ = queued_job
    with store.write() as session:
        legacy = AIJob(
            project_id=store.project_id,
            task_type="chat",
            prompt_version=PROMPT_VERSION,
            status="running",
            instructions="legacy",
        )
        session.add(legacy)
        session.flush()
        legacy_id = legacy.id

    store.reconcile_owned("owner")

    current = store.read(legacy_id)
    assert current["status"] == "recovery_required"
    assert current["error_code"] == "LEGACY_JOB_UNRECOVERABLE"


def test_reconcile_owned_does_not_touch_empty_epoch(queued_job):
    store, job_id = queued_job
    store.claim(job_id, "owner")
    with store.write() as session:
        control = session.get(AIJobControl, job_id)
        control.worker_epoch = None
        revision = control.control_revision

    store.reconcile_owned("owner")

    assert store.read(job_id)["status"] == "running"
    with store.database.job_session_scope() as session:
        assert session.get(AIJobControl, job_id).control_revision == revision


@pytest.mark.parametrize("entrypoint", ["recover", "reconcile_owned"])
@pytest.mark.parametrize(
    ("evidence", "expected_status", "expected_reason"),
    [
        ("cancel", "cancelled", None),
        ("unsupported", "recovery_required", "unsupported_format"),
        ("unknown", "recovery_required", "result_unknown"),
        ("failed", "failed", "stage_failed"),
        ("prepared", "queued", None),
    ],
)
def test_recovery_entrypoints_share_the_same_classification_matrix(
    queued_job, entrypoint, evidence, expected_status, expected_reason
):
    store, job_id = queued_job
    owner = "owner"
    fence = store.claim(job_id, owner)
    if evidence == "cancel":
        with store.write() as session:
            session.get(AIJobControl, job_id).cancel_requested = True
    elif evidence == "unsupported":
        with store.write() as session:
            session.get(AIJobControl, job_id).format_version = 99
    elif evidence == "unknown":
        _attempt(store, fence, "dispatched")
    elif evidence == "failed":
        _attempt(store, fence, "failed", outcome="known")
    else:
        _attempt(store, fence, "prepared")

    if entrypoint == "recover":
        store.recover("startup")
    else:
        store.reconcile_owned(owner)

    current = store.read(job_id)
    assert current["status"] == expected_status
    assert current["recovery_reason"] == expected_reason


def test_reconcile_owned_honors_exact_authorization_for_result_unknown(queued_job):
    store, job_id = queued_job
    fence = store.claim(job_id, "owner")
    number = _attempt(store, fence, "result_unknown", outcome="unknown")
    with store.write() as session:
        control = session.get(AIJobControl, job_id)
        control.authorized_stage = "chat"
        control.authorized_attempt_no = number + 1

    store.reconcile_owned("owner")

    assert store.read(job_id)["status"] == "queued"
    with store.database.job_session_scope() as session:
        control = session.get(AIJobControl, job_id)
        assert (control.authorized_stage, control.authorized_attempt_no) == (
            "chat",
            number + 1,
        )


@pytest.mark.parametrize("wrong_authorization", ["stage", "attempt"])
def test_reconcile_owned_rejects_non_exact_unknown_authorization(
    queued_job, wrong_authorization
):
    store, job_id = queued_job
    fence = store.claim(job_id, "owner")
    number = _attempt(store, fence, "result_unknown", outcome="unknown")
    with store.write() as session:
        control = session.get(AIJobControl, job_id)
        control.authorized_stage = "other" if wrong_authorization == "stage" else "chat"
        control.authorized_attempt_no = number + (2 if wrong_authorization == "attempt" else 1)
        authorization = (control.authorized_stage, control.authorized_attempt_no)

    store.reconcile_owned("owner")

    current = store.read(job_id)
    assert current["status"] == "recovery_required"
    assert current["recovery_reason"] == "result_unknown"
    with store.database.job_session_scope() as session:
        control = session.get(AIJobControl, job_id)
        assert (control.authorized_stage, control.authorized_attempt_no) == authorization
