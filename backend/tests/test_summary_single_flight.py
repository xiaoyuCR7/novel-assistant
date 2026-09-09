"""Chapter summary admission is single-flight per project/chapter."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier

import pytest
from job_helpers import save_chapter
from sqlalchemy import func, select

from novel_harness.ai.base import ProviderExecutionError
from novel_harness.ai.demo import DemoProvider
from novel_harness.ai.prompts import PROMPT_VERSION
from novel_harness.db.job_models import AIJobControl
from novel_harness.db.models import AIJob
from novel_harness.services.job_store import JobStore


def setup_completion(client, project, chapter_id):
    url = f"/api/v1/projects/{project['id']}/chapters/{chapter_id}"
    saved = save_chapter(client, url, json={"content": "邮差将无主信封锁进钟楼。"})
    assert saved.status_code == 200, saved.text
    payload = {"expected_revision": saved.json()["revision"]}
    store = JobStore(
        client.app.state.vault_registry.require(project["id"]).database,
        project["id"],
    )
    return url, payload, store


def submit(client, url, payload, key, **extra):
    return client.post(
        url + "/complete",
        json={**payload, **extra},
        headers={"Idempotency-Key": key},
    )


def set_summary_state(store, job_id, status, *, effects=None):
    with store.write() as session:
        session.get(AIJob, job_id).status = status
        if effects is not None:
            session.get(AIJobControl, job_id).effects = effects


def summary_count(store):
    with store.database.job_session_scope() as session:
        return session.scalar(
            select(func.count())
            .select_from(AIJob)
            .where(AIJob.task_type == "chapter_summary")
        )


def test_same_key_replays_before_distinct_key_is_blocked(client, project, seeded_chapter):
    url, payload, store = setup_completion(client, project, seeded_chapter)
    first = submit(client, url, payload, "same-key")
    assert first.status_code == 202, first.text

    replay = submit(client, url, payload, "same-key")
    assert replay.status_code == 202
    assert replay.json()["id"] == first.json()["id"]

    blocked = submit(client, url, payload, "distinct-key")
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == {
        "code": "SUMMARY_TASK_ACTIVE",
        "job_id": first.json()["id"],
    }
    assert summary_count(store) == 1


@pytest.mark.parametrize(
    "status",
    ["queued", "running", "cancel_requested", "recovery_required", "failed"],
)
def test_unresolved_summary_states_block_distinct_keys(
    client, project, seeded_chapter, status
):
    url, payload, store = setup_completion(client, project, seeded_chapter)
    first = submit(client, url, payload, "first")
    assert first.status_code == 202, first.text
    set_summary_state(store, first.json()["id"], status)

    blocked = submit(client, url, payload, "second")
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == {
        "code": "SUMMARY_TASK_ACTIVE",
        "job_id": first.json()["id"],
    }
    assert summary_count(store) == 1


def test_published_projection_only_job_does_not_block(client, project, seeded_chapter):
    url, payload, store = setup_completion(client, project, seeded_chapter)
    first = submit(client, url, payload, "published")
    assert first.status_code == 202, first.text
    set_summary_state(
        store,
        first.json()["id"],
        "recovery_required",
        effects={"summary_id": "published-summary", "ledger_pending": True},
    )

    current_payload = {"expected_revision": client.get(url).json()["revision"]}
    second = submit(client, url, current_payload, "new-summary")
    assert second.status_code == 202, second.text
    assert second.json()["id"] != first.json()["id"]
    assert summary_count(store) == 2


@pytest.mark.parametrize(
    "summary_id",
    [None, "", 0, False, [], {}],
    ids=["null", "empty", "zero", "false", "list", "object"],
)
def test_invalid_published_identity_remains_a_blocker(
    client, project, seeded_chapter, summary_id
):
    url, payload, store = setup_completion(client, project, seeded_chapter)
    first = submit(client, url, payload, "invalid-published-identity")
    assert first.status_code == 202, first.text
    set_summary_state(
        store,
        first.json()["id"],
        "recovery_required",
        effects={"summary_id": summary_id, "ledger_pending": True},
    )

    blocked = submit(client, url, payload, "new-summary")
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == {
        "code": "SUMMARY_TASK_ACTIVE",
        "job_id": first.json()["id"],
    }
    assert summary_count(store) == 1


def test_historical_summary_without_control_remains_a_blocker(
    client, project, seeded_chapter
):
    url, payload, store = setup_completion(client, project, seeded_chapter)
    first = submit(client, url, payload, "legacy-summary")
    assert first.status_code == 202, first.text
    with store.write() as session:
        session.get(AIJob, first.json()["id"]).status = "recovery_required"
        session.delete(session.get(AIJobControl, first.json()["id"]))

    blocked = submit(client, url, payload, "new-summary")
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == {
        "code": "SUMMARY_TASK_ACTIVE",
        "job_id": first.json()["id"],
    }
    assert summary_count(store) == 1


def test_cancelled_source_can_be_replaced_but_other_summary_reports_its_job_id(
    client, project, seeded_chapter
):
    url, payload, store = setup_completion(client, project, seeded_chapter)
    source = submit(client, url, payload, "source")
    assert source.status_code == 202, source.text
    set_summary_state(store, source.json()["id"], "cancelled")
    current_payload = {"expected_revision": client.get(url).json()["revision"]}

    replacement = submit(
        client,
        url,
        current_payload,
        "replacement",
        replaces_job_id=source.json()["id"],
    )
    assert replacement.status_code == 202, replacement.text
    assert replacement.json()["replaces_job_id"] == source.json()["id"]

    blocked = submit(
        client,
        url,
        current_payload,
        "another-replacement",
        replaces_job_id=source.json()["id"],
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == {
        "code": "SUMMARY_TASK_ACTIVE",
        "job_id": replacement.json()["id"],
    }


def test_unknown_provider_result_blocks_unlinked_resubmit_without_second_send(
    client, project, seeded_chapter
):
    url, payload, store = setup_completion(client, project, seeded_chapter)
    calls = []

    class UnknownResult(DemoProvider):
        def generate_structured(self, request, schema):
            calls.append(request.task)
            raise ProviderExecutionError("provider result unknown")

    client.app.state.ai_provider = UnknownResult()
    first = submit(client, url, payload, "first-send")
    assert first.status_code == 202, first.text
    assert client.app.state.job_executor.run_once()
    assert store.read(first.json()["id"])["status"] == "recovery_required"
    assert calls == ["chapter_summary"]

    blocked = submit(client, url, payload, "unlinked-resubmit")
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == {
        "code": "SUMMARY_TASK_ACTIVE",
        "job_id": first.json()["id"],
    }
    assert not client.app.state.job_executor.run_once()
    assert calls == ["chapter_summary"]
    assert summary_count(store) == 1


def test_historical_duplicate_blockers_choose_created_at_then_id(
    client, project, seeded_chapter
):
    url, payload, store = setup_completion(client, project, seeded_chapter)
    created_at = datetime(2026, 1, 1, tzinfo=UTC)
    with store.write() as session:
        for job_id in ("summary-blocker-b", "summary-blocker-a"):
            job = AIJob(
                id=job_id,
                project_id=project["id"],
                chapter_id=seeded_chapter,
                task_type="chapter_summary",
                status="queued",
                prompt_version=PROMPT_VERSION,
                instructions="",
                token_budget=12000,
                context_snapshot={},
                result={},
                created_at=created_at,
            )
            session.add(job)
            session.flush()
            session.add(
                AIJobControl(
                    job_id=job_id,
                    operation="chapter_summary",
                    idempotency_key=job_id,
                    request_hash=job_id,
                    command={},
                    source_snapshot={},
                    provider_identity={},
                    embedding_identity=None,
                    effects={},
                )
            )
            session.flush()

    blocked = submit(client, url, payload, "third")
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == {
        "code": "SUMMARY_TASK_ACTIVE",
        "job_id": "summary-blocker-a",
    }
    assert summary_count(store) == 2


def test_concurrent_distinct_keys_create_only_one_summary_job(
    client, project, seeded_chapter
):
    url, payload, store = setup_completion(client, project, seeded_chapter)
    ready = Barrier(2)

    def post(key):
        ready.wait(timeout=5)
        return submit(client, url, payload, key)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(post, ("concurrent-a", "concurrent-b")))

    assert sorted(response.status_code for response in responses) == [202, 409]
    accepted = next(response for response in responses if response.status_code == 202)
    blocked = next(response for response in responses if response.status_code == 409)
    assert blocked.json()["detail"] == {
        "code": "SUMMARY_TASK_ACTIVE",
        "job_id": accepted.json()["id"],
    }
    assert summary_count(store) == 1
