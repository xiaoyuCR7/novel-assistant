from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException


def test_cancel_is_idempotent_and_blocks_publish(queued_job):
    store, job_id = queued_job
    fence = store.claim(job_id, "owner")
    first = store.cancel(job_id)
    assert first["status"] == "cancel_requested"
    assert store.cancel(job_id)["control_revision"] == first["control_revision"]
    with pytest.raises(HTTPException):
        store.publish(fence, lambda session, job: setattr(job, "result", {"bad": True}))
    store.recover("new-owner")
    assert store.read(job_id)["status"] == "cancelled"


def test_unknown_resume_requires_confirmation_and_is_idempotent(queued_job):
    store, job_id = queued_job
    fence = store.claim(job_id, "one")
    number = store.begin_attempt(fence, "draft", {})
    store.dispatch(fence, "draft", number)
    store.recover("two")
    revision = store.read(job_id)["control_revision"]
    with pytest.raises(HTTPException) as error:
        store.resume(job_id, "resume", revision, False)
    assert error.value.detail["code"] == "UNKNOWN_RESULT_CONFIRMATION_REQUIRED"
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(
            pool.map(lambda _: store.resume(job_id, "resume", revision, True), range(2))
        )
    assert receipts[0] == receipts[1]
    store.recover("three")  # Authorized queued replay survives a restart before claiming.
    assert store.read(job_id)["status"] == "queued"
    next_fence = store.claim(job_id, "three")
    assert store.begin_attempt(next_fence, "draft", {}) == 2
    with pytest.raises(HTTPException):
        store.resume(job_id, "resume", revision, False)


def test_queued_cancel_cannot_be_claimed(queued_job):
    store, job_id = queued_job
    first = store.cancel(job_id)
    assert first["status"] == "cancelled"
    assert first["control_revision"] == store.cancel(job_id)["control_revision"]
    assert store.claim(job_id, "owner") is None
