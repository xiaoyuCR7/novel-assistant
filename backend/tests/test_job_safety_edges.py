from concurrent.futures import ThreadPoolExecutor
from threading import Event

from job_helpers import complete_job, run_job, save_chapter
from sqlalchemy import select

from novel_harness.ai.demo import DemoProvider
from novel_harness.db.job_models import AIJobControl, AIJobStageAttempt, JobSchemaVersion
from novel_harness.db.models import AIJob


def test_future_stage_state_pauses_without_replay(queued_job):
    store, job_id = queued_job
    fence = store.claim(job_id, "old")
    store.begin_attempt(fence, "draft", {})
    with store.write() as session:
        session.scalar(select(AIJobStageAttempt)).status = "future_state"
    store.recover("new")
    assert store.read(job_id)["status"] == "recovery_required"
    assert store.claim(job_id, "new") is None


def test_frozen_keyword_fallback_does_not_retry_failed_embedding_on_restart(queued_job):
    store, job_id = queued_job
    fence = store.claim(job_id, "old")
    number = store.begin_attempt(fence, "context_embedding", {})
    store.dispatch(fence, "context_embedding", number)
    store.fail_attempt(
        fence, "context_embedding", number, "INVALID_VECTOR", "known", terminal=False
    )
    with store.write() as session:
        session.get(AIJobControl, job_id).context_ready = True
    store.recover("new")
    assert store.read(job_id)["status"] == "queued"


def test_unknown_resume_resends_only_explicitly_authorized_attempt(queued_job):
    store, job_id = queued_job
    fence = store.claim(job_id, "first")
    number = store.begin_attempt(fence, "draft", {})
    store.dispatch(fence, "draft", number)
    store.fail_attempt(fence, "draft", number, "TIMEOUT", "unknown")
    before = store.read(job_id)
    store.resume(job_id, "authorize", before["control_revision"], True)
    fence = store.claim(job_id, "second")
    number = store.begin_attempt(fence, "draft", {})
    store.dispatch(fence, "draft", number)
    store.recover("third")
    assert store.read(job_id)["recovery_reason"] == "result_unknown"


def test_worker_cancellation_cannot_publish_and_other_vault_runs_afterwards(client, project):
    entered, release = Event(), Event()

    class Blocked(DemoProvider):
        def generate_text(self, request):
            entered.set()
            assert release.wait(5)
            return super().generate_text(request)

    base = f"/api/v1/projects/{project['id']}/ai/jobs"
    receipt = client.post(
        base,
        json={"project_id": project["id"], "task_type": "chat"},
        headers={"Idempotency-Key": "block"},
    ).json()
    other = client.post("/api/v1/projects", json={"title": "Other"}).json()
    client.app.state.ai_provider = Blocked()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.app.state.job_executor.run_once)
        try:
            assert entered.wait(5)
            assert not client.app.state.job_executor.run_once()
            assert (
                client.post(base + "/" + receipt["id"] + "/cancel").json()["status"]
                == "cancel_requested"
            )
        finally:
            release.set()
        assert future.result(timeout=5)
    current = client.get(receipt["status_url"]).json()
    assert current["status"] == "cancelled"
    assert current["result"] == {}
    client.app.state.ai_provider = DemoProvider()
    assert (
        run_job(
            client,
            f"/api/v1/projects/{other['id']}/ai/jobs",
            json={"project_id": other["id"], "task_type": "chat"},
        ).json()["status"]
        == "succeeded"
    )


def test_published_summary_repair_works_after_cancel_and_broken_config(
    client,
    project,
    seeded_chapter,
    monkeypatch,
):
    from novel_harness.services import chapter_summaries

    url = f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}"
    save_chapter(client, url, json={"content": "Preserved chapter"})
    original = chapter_summaries.write_ledger
    monkeypatch.setattr(
        chapter_summaries, "write_ledger", lambda _: (_ for _ in ()).throw(OSError())
    )
    job = complete_job(client, url).json()
    client.post(job["status_url"] + "/cancel")
    monkeypatch.setattr(chapter_summaries, "write_ledger", original)
    monkeypatch.setattr(
        client.app.state.model_settings,
        "identity",
        lambda: (_ for _ in ()).throw(ValueError("invalid config")),
    )
    response = client.post(url + "/summary/repair-ledger")
    assert response.status_code == 200
    after = client.get(job["status_url"]).json()
    assert after["status"] == "cancelled"
    assert after["effects"]["summary_id"] == job["effects"]["summary_id"]
    assert after["effects"]["ledger_pending"] is False


def test_active_job_discovery_is_not_hidden_by_history_limit(client, project, queued_job):
    store, job_id = queued_job
    with store.write() as session:
        for _ in range(101):
            session.add(
                AIJob(
                    project_id=project["id"],
                    task_type="chat",
                    prompt_version="test",
                    status="succeeded",
                    instructions="history",
                )
            )
    jobs = client.get(f"/api/v1/projects/{project['id']}/ai/jobs").json()
    assert job_id in {job["id"] for job in jobs}


def test_unsupported_task_format_refuses_api_writes(client, project):
    database = client.app.state.vault_registry.require(project["id"]).database
    with database.job_session_scope() as session:
        session.get(JobSchemaVersion, "durable_jobs").version = 999
    response = client.post(
        f"/api/v1/projects/{project['id']}/ai/jobs",
        json={
            "project_id": project["id"],
            "task_type": "chat",
        },
        headers={"Idempotency-Key": "future"},
    )
    assert response.status_code == 503
    with database.job_session_scope() as session:
        assert session.scalar(select(AIJob)) is None


def test_legacy_and_incompatible_jobs_do_not_resume_automatically(queued_job):
    store, job_id = queued_job
    with store.write() as session:
        session.get(AIJob, job_id).prompt_version = "future-prompt"
        legacy = AIJob(
            project_id=store.project_id,
            task_type="chat",
            prompt_version="legacy",
            status="running",
            instructions="Old request",
        )
        session.add(legacy)
        session.flush()
        legacy_id = legacy.id
    store.recover("new")
    assert store.read(job_id)["status"] == "recovery_required"
    assert "resume" not in store.read(job_id)["allowed_actions"]
    assert store.read(legacy_id)["status"] == "recovery_required"
    assert store.read(legacy_id)["allowed_actions"] == ["cancel", "replace"]
    assert store.read(legacy_id)["replacement_requires_confirmation"] is True
