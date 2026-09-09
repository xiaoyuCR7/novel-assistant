from importlib import import_module

from job_helpers import save_chapter

from novel_harness.ai.demo import DemoProvider
from novel_harness.db.models import ChapterDocument, ChapterSummary, StoryNode
from novel_harness.services.job_store import JobStore
from novel_harness.services.versions import get_or_create_document


def setup_summary(client, project, chapter_id):
    summaries = import_module("novel_harness.services.chapter_summaries")
    database = client.app.state.vault_registry.require(project["id"]).database
    with database.job_session_scope() as session:
        document = get_or_create_document(session, chapter_id)
        document.content = "邮差收到一封没有地址的信。"
        revision = document.revision
    store = JobStore(database, project["id"])
    command = dict(
        project_id=project["id"],
        chapter_id=chapter_id,
        task_type="chapter_summary",
        instructions="",
        token_budget=12000,
        expected_revision=revision,
    )
    job = store.enqueue(
        command,
        "complete",
        lambda session: {
            "source_snapshot": summaries.prepare_completion(session, chapter_id, revision),
            "provider_identity": {"mode": "demo"},
            "embedding_identity": None,
        },
    )
    return summaries, store, job["id"]


def test_ledger_failure_retains_summary_and_repair_never_regenerates(
    client,
    project,
    seeded_chapter,
    monkeypatch,
):
    summaries, store, job_id = setup_summary(client, project, seeded_chapter)
    calls = []

    class Counted(DemoProvider):
        def generate_structured(self, request, schema):
            calls.append(request.task)
            return super().generate_structured(request, schema)

    original = summaries.write_ledger
    monkeypatch.setattr(summaries, "write_ledger", lambda session: (_ for _ in ()).throw(OSError()))
    summaries.generate_summary(store, store.claim(job_id, "one"), lambda observer: Counted())
    before = store.read(job_id)
    assert before["status"] == "recovery_required"
    assert before["effects"]["ledger_pending"] is True
    with store.database.job_session_scope() as session:
        summary = session.get(ChapterSummary, before["effects"]["summary_id"])
        assert summary.status == "valid"
    monkeypatch.setattr(summaries, "write_ledger", original)
    with store.write() as session:
        summaries.repair_ledger(session, seeded_chapter)
    after = store.read(job_id)
    assert after["effects"]["ledger_pending"] is False
    assert after["effects"]["summary_id"] == before["effects"]["summary_id"]
    assert calls == ["chapter_summary"]


def test_summary_job_exposes_sanitized_stage_usage(
    client,
    project,
    seeded_chapter,
):
    summaries, store, job_id = setup_summary(client, project, seeded_chapter)

    class Metered(DemoProvider):
        def generate_structured(self, request, schema):
            result = super().generate_structured(request, schema)
            result.usage = {"input_tokens": 17, "output_tokens": 9}
            result.usage_source = "provider"
            return result

    summaries.generate_summary(
        store,
        store.claim(job_id, "one"),
        lambda observer: Metered(),
    )

    result = store.read(job_id)["result"]
    execution = result["execution"]
    stage = execution["stages"]["chapter_summary"]
    assert execution["prompt_version"]
    assert execution["summary_reused"] is False
    assert stage["usage"] == {"input_tokens": 17, "output_tokens": 9}
    assert "text" not in stage
    assert "data" not in stage


def test_ledger_repair_after_edit_does_not_complete_new_draft(client, project, seeded_chapter):
    summaries, store, job_id = setup_summary(client, project, seeded_chapter)
    summaries.generate_summary(store, store.claim(job_id, "one"), lambda observer: DemoProvider())
    with store.write() as session:
        session.get(ChapterDocument, seeded_chapter).content = "新正文"
        session.get(StoryNode, seeded_chapter).status = "drafting"
        summaries.repair_ledger(session, seeded_chapter)
    with store.database.job_session_scope() as session:
        assert session.get(StoryNode, seeded_chapter).status == "drafting"


def test_same_hash_completion_preserves_author_edited_summary_without_inference(
    client,
    project,
    seeded_chapter,
):
    from job_helpers import complete_summary

    url = f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}"
    save_chapter(client, url, json={"content": "原始正文"})
    summary = complete_summary(client, url).json()
    client.patch(
        url + "/summary",
        json={
            "summary_id": summary["id"],
            "revision": summary["revision"],
            "recap": "作者修订总结",
        },
    )

    class Forbidden(DemoProvider):
        def generate_structured(self, request, schema):
            raise AssertionError("Same-hash completion must not regenerate")

    client.app.state.ai_provider = Forbidden()
    after = complete_summary(client, url).json()
    assert after["id"] == summary["id"]
    assert after["recap"] == "作者修订总结"
    assert after["origin"] == "author_edited"
