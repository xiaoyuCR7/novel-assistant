"""Independent conversations use temporary vaults and the offline demo only."""

import json
from uuid import uuid4

from sqlalchemy import event, select

from novel_harness.db.job_models import AIJobControl
from novel_harness.db.models import AIJob
from novel_harness.schemas.ai import AIJobCreate


def paths(project):
    base = f"/api/v1/projects/{project['id']}"
    return base + "/conversations", base + "/ai/jobs"


def create(client, project, title="独立方案", chapter_id=None, **extra):
    base, _ = paths(project)
    response = client.post(
        base,
        json={
            "id": str(uuid4()),
            "chapter_id": chapter_id,
            "title": title,
            **extra,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def submit(client, project, thread=None, text="讨论", chapter_id=None):
    _, jobs = paths(project)
    command = {
        "project_id": project["id"],
        "chapter_id": chapter_id,
        "task_type": "chat",
        "instructions": text,
        "expected_revision": 1 if chapter_id else None,
    }
    if thread:
        command["conversation_id"] = thread["id"]
    response = client.post(jobs, json=command, headers={"Idempotency-Key": uuid4().hex})
    assert response.status_code == 202, response.text
    assert client.app.state.job_executor.run_once()
    result = client.get(jobs + "/" + response.json()["id"]).json()
    assert result["status"] == "succeeded", result
    return result


def history_ids(job):
    fragments = job["context_snapshot"]["fragments"]
    fragment = next((f for f in fragments if f["source_type"] == "conversation"), None)
    return [turn["id"] for turn in json.loads(fragment["content"])] if fragment else []


def test_schema_omits_absent_conversation_from_legacy_hash_but_keeps_explicit_id():
    command = dict(
        project_id="p",
        chapter_id=None,
        task_type="chat",
        instructions="",
        token_budget=12000,
        expected_revision=None,
        replaces_job_id=None,
        confirm_unknown=False,
    )
    assert AIJobCreate(**command).model_dump() == command
    assert (
        AIJobCreate(**command, conversation_id="chosen").model_dump()["conversation_id"] == "chosen"
    )


def test_default_history_and_explicit_conversations_are_isolated(client, project):
    base, jobs = paths(project)
    old = submit(client, project, text="默认会话约定")
    thread = create(client, project)
    first = submit(client, project, thread, "独立话题")
    assert history_ids(first) == []
    second = submit(client, project, thread, "继续独立话题")
    assert history_ids(second) == [first["id"]]
    default_next = submit(client, project, text="继续原话题")
    assert history_ids(default_next) == [old["id"]]
    listing = client.get(base).json()
    assert old["conversation_id"] == listing["default_conversation_id"]
    assert {row["id"] for row in client.get(jobs + "/page").json()["items"]} == {
        old["id"],
        default_next["id"],
    }
    assert {
        row["id"]
        for row in client.get(
            jobs + "/page",
            params={
                "conversation_id": thread["id"],
            },
        ).json()["items"]
    } == {first["id"], second["id"]}


def test_branch_freezes_prefix_and_nested_branch_does_not_read_future(client, project):
    base, jobs = paths(project)
    thread = create(client, project)
    first = submit(client, project, thread, "分叉以前")
    later = submit(client, project, thread, "不能泄漏到分支")
    body = {"id": str(uuid4()), "from_job_id": first["id"], "title": "方案 B"}
    branch = client.post(base + "/" + thread["id"] + "/branches", json=body)
    assert branch.status_code == 201, branch.text
    assert (
        client.post(base + "/" + thread["id"] + "/branches", json=body).json()["id"] == body["id"]
    )
    child = submit(client, project, branch.json(), "尝试另一种发展")
    assert history_ids(child) == [first["id"]]
    page = client.get(jobs + "/page", params={"conversation_id": body["id"]}).json()
    assert {row["id"] for row in page["items"]} == {first["id"], child["id"]}
    assert next(row for row in page["items"] if row["id"] == first["id"])["inherited"] is True
    assert later["id"] not in history_ids(child)
    inherited_detail = client.get(jobs + "/" + first["id"], params={"conversation_id": body["id"]})
    assert inherited_detail.status_code == 200
    assert inherited_detail.json()["inherited"] is True
    assert inherited_detail.json()["allowed_actions"] == []
    assert (
        client.get(
            jobs + "/" + later["id"],
            params={
                "conversation_id": body["id"],
            },
        ).status_code
        == 404
    )
    nested = client.post(
        base + "/" + body["id"] + "/branches",
        json={
            "id": str(uuid4()),
            "from_job_id": first["id"],
            "title": "从继承消息再分支",
        },
    ).json()
    assert history_ids(submit(client, project, nested)) == [first["id"]]


def test_soft_delete_blocks_new_calls_filters_branch_memory_and_restores(client, project):
    base, jobs = paths(project)
    thread = create(client, project)
    first = submit(client, project, thread, "可恢复删除的原话题")
    branch = client.post(
        base + "/" + thread["id"] + "/branches",
        json={
            "id": str(uuid4()),
            "from_job_id": first["id"],
            "title": "保留分支",
        },
    ).json()
    deleted = client.patch(
        base + "/" + thread["id"],
        json={
            "expected_revision": thread["revision"],
            "status": "deleted",
        },
    )
    assert deleted.status_code == 200, deleted.text
    rejected = client.post(
        jobs,
        headers={"Idempotency-Key": uuid4().hex},
        json={
            "project_id": project["id"],
            "task_type": "chat",
            "conversation_id": thread["id"],
        },
    )
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "CONVERSATION_DELETED"
    assert history_ids(submit(client, project, branch)) == []
    assert client.get(jobs + "/" + first["id"]).status_code == 200
    restored = client.patch(
        base + "/" + thread["id"],
        json={
            "expected_revision": deleted.json()["revision"],
            "status": "active",
        },
    )
    assert restored.status_code == 200
    assert history_ids(submit(client, project, restored.json())) == [first["id"]]


def test_active_task_prevents_deletion_and_legacy_command_remains_exact(client, project):
    base, jobs = paths(project)
    thread = create(client, project)
    response = client.post(
        jobs,
        headers={"Idempotency-Key": "pending-independent"},
        json={
            "project_id": project["id"],
            "task_type": "chat",
            "conversation_id": thread["id"],
        },
    )
    assert response.status_code == 202
    rejected = client.patch(
        base + "/" + thread["id"],
        json={
            "expected_revision": thread["revision"],
            "status": "deleted",
        },
    )
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "CONVERSATION_TASK_ACTIVE"
    client.post(jobs + "/" + response.json()["id"] + "/cancel")
    assert (
        client.patch(
            base + "/" + thread["id"],
            json={
                "expected_revision": thread["revision"],
                "status": "deleted",
            },
        ).status_code
        == 200
    )
    legacy = submit(client, project)
    database = client.app.state.vault_registry.require(project["id"]).database
    with database.job_session_scope() as session:
        assert "conversation_id" not in session.get(AIJobControl, legacy["id"]).command
        assert session.scalar(select(AIJob.id).where(AIJob.id == response.json()["id"]))


def test_search_finds_full_message_and_literal_wildcards_and_paginates(client, project):
    base, _ = paths(project)
    default = submit(client, project, text="原有默认消息")
    first = create(client, project, "调查线索")
    second = create(client, project, "角色分歧")
    submit(client, project, first, "长消息" * 600 + "独特全文尾部%_")
    submit(client, project, second, "普通讨论")
    found = client.get(base, params={"q": "全文尾部%_"})
    assert found.status_code == 200, found.text
    assert [row["id"] for row in found.json()["items"]] == [first["id"]]
    assert (
        client.get(base, params={"q": "原有默认消息"}).json()["items"][0]["id"]
        == default["conversation_id"]
    )
    page = client.get(base, params={"limit": 1}).json()
    seen = [page["items"][0]["id"]]
    while page["next_cursor"]:
        page = client.get(base, params={"limit": 1, "before": page["next_cursor"]}).json()
        seen.extend(row["id"] for row in page["items"])
    assert len(seen) == len(set(seen)) == 3


def test_scope_validation_archive_and_deleted_default_do_not_leak_to_work_queue(
    client, project, seeded_chapter
):
    from novel_harness.services.conversation_threads import visible_job_filter

    base, jobs = paths(project)
    chapter_thread = create(client, project, chapter_id=seeded_chapter)
    wrong = client.post(
        jobs,
        json={
            "project_id": project["id"],
            "task_type": "chat",
            "conversation_id": chapter_thread["id"],
        },
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert wrong.status_code == 404
    assert (
        client.get(jobs + "/page", params={"conversation_id": chapter_thread["id"]}).status_code
        == 404
    )
    default = submit(client, project)
    independent = create(client, project)
    separate = submit(client, project, independent)
    assert (
        client.patch(
            base + "/" + default["conversation_id"],
            json={
                "expected_revision": 1,
                "status": "deleted",
            },
        ).status_code
        == 200
    )
    assert (
        client.patch(
            base + "/" + independent["id"],
            json={
                "expected_revision": 1,
                "status": "archived",
            },
        ).status_code
        == 200
    )
    assert client.get(base).json()["items"] == []
    assert (
        client.get(base, params={"status": "archived"}).json()["items"][0]["id"]
        == independent["id"]
    )
    database = client.app.state.vault_registry.require(project["id"]).database
    with database.job_session_scope() as session:
        assert session.scalars(select(AIJob.id).where(visible_job_filter())).all() == []
        assert session.scalars(
            select(AIJob.id).where(
                visible_job_filter(include_archived=True),
            )
        ).all() == [separate["id"]]


def test_conversation_list_is_bounded_without_loading_job_content(client, project):
    from novel_harness.db.models import ConversationThread

    base, _ = paths(project)
    database = client.app.state.vault_registry.require(project["id"]).database
    with database.job_session_scope() as session:
        for index in range(100):
            session.add(
                ConversationThread(id=str(uuid4()), project_id=project["id"], title=f"会话 {index}")
            )
    statements = []

    def record(conn, cursor, statement, params, context, many):
        statements.append(statement)

    event.listen(database.engine, "before_cursor_execute", record)
    try:
        response = client.get(base, params={"limit": 20})
    finally:
        event.remove(database.engine, "before_cursor_execute", record)
    assert response.status_code == 200
    assert len(response.json()["items"]) == 20
    assert response.json()["next_cursor"]
    assert len(statements) <= 6
    assert all("context_snapshot" not in sql for sql in statements)


def test_deleted_parent_is_not_searchable_through_branch_but_trash_search_finds_own_message(
    client, project
):
    base, _ = paths(project)
    parent = create(client, project)
    job = submit(client, project, parent, "只存在原会话的特殊线索")
    branch = client.post(
        base + "/" + parent["id"] + "/branches",
        json={
            "id": str(uuid4()),
            "from_job_id": job["id"],
            "title": "分支",
        },
    ).json()
    assert len(client.get(base, params={"q": "特殊线索"}).json()["items"]) == 2
    assert (
        client.patch(
            base + "/" + parent["id"],
            json={
                "expected_revision": 1,
                "status": "deleted",
            },
        ).status_code
        == 200
    )
    assert client.get(base, params={"q": "特殊线索"}).json()["items"] == []
    recovered = client.get(base, params={"q": "特殊线索", "status": "deleted"}).json()["items"]
    assert [item["id"] for item in recovered] == [parent["id"]]
    assert branch["id"] != parent["id"]


def test_active_branch_blocks_parent_delete_and_failed_snapshot_cannot_resume_deleted_source(
    client, project
):
    base, jobs = paths(project)
    parent = create(client, project)
    first = submit(client, project, parent)
    branch = client.post(
        base + "/" + parent["id"] + "/branches",
        json={
            "id": str(uuid4()),
            "from_job_id": first["id"],
            "title": "分支",
        },
    ).json()
    pending = client.post(
        jobs,
        headers={"Idempotency-Key": "frozen-branch"},
        json={
            "project_id": project["id"],
            "task_type": "chat",
            "conversation_id": branch["id"],
        },
    ).json()
    refused = client.patch(
        base + "/" + parent["id"],
        json={
            "expected_revision": 1,
            "status": "deleted",
        },
    )
    assert refused.status_code == 409
    assert refused.json()["detail"]["job_id"] == pending["id"]
    database = client.app.state.vault_registry.require(project["id"]).database
    with database.job_session_scope() as session:
        row = session.get(AIJob, pending["id"])
        row.status = "failed"
        row.context_snapshot = {"conversation": {"source_conversation_ids": [parent["id"]]}}
    assert (
        client.patch(
            base + "/" + parent["id"],
            json={
                "expected_revision": 1,
                "status": "deleted",
            },
        ).status_code
        == 200
    )
    assert "resume" not in client.get(jobs + "/" + pending["id"]).json()["allowed_actions"]
    page = client.get(jobs + "/page", params={"conversation_id": branch["id"]}).json()
    assert (
        "resume"
        not in next(row for row in page["items"] if row["id"] == pending["id"])["allowed_actions"]
    )
    resumed = client.post(
        jobs + "/" + pending["id"] + "/resume",
        headers={"Idempotency-Key": "resume-deleted-source"},
        json={
            "expected_control_revision": pending["control_revision"],
            "confirm_unknown": False,
        },
    )
    assert resumed.status_code == 409
    assert resumed.json()["detail"]["code"] == "CONVERSATION_SOURCE_DELETED"


def test_branch_compaction_cannot_reuse_parent_memory_cache(client, project):
    from test_conversation_context import enqueue, execute, history

    from novel_harness.services.job_context import capture_source
    from novel_harness.services.job_store import JobStore

    base, _ = paths(project)
    history(client, project, count=12, size=120)
    store, parent_job_id = enqueue(client, project)
    parent = execute(store, parent_job_id, [])
    assert parent["context_snapshot"]["conversation"]["mode"] == "compressed"
    branch = client.post(
        base + "/" + parent["conversation_id"] + "/branches",
        json={
            "id": str(uuid4()),
            "from_job_id": parent_job_id,
            "title": "另一种发展",
        },
    ).json()
    store = JobStore(store.database, project["id"])
    command = dict(
        project_id=project["id"],
        chapter_id=None,
        task_type="chat",
        instructions="沿分支继续讨论",
        token_budget=6000,
        expected_revision=None,
        conversation_id=branch["id"],
    )
    job = store.enqueue(
        command,
        "branch-compaction",
        lambda session: {
            "source_snapshot": capture_source(session, project["id"], None),
            "provider_identity": {
                "mode": "demo",
                "context_capacity": 8192,
                "output_token_budget": 512,
            },
            "embedding_identity": None,
        },
    )
    calls = []
    result = execute(store, job["id"], calls)
    state = result["context_snapshot"]["conversation"]
    assert state["mode"] == "compressed"
    assert state["scope"]["conversation_id"] == branch["id"]
    assert state["scope"]["branch_from_job_id"] == parent_job_id
    assert state["reused_from_job_id"] is None
    assert any(request.task == "conversation_summary" for request in calls)


def test_restart_adds_tables_for_pre_thread_vault_without_changing_old_receipt(
    tmp_path, monkeypatch
):
    import sqlite3
    from contextlib import closing

    from fastapi.testclient import TestClient

    from novel_harness.main import create_app

    monkeypatch.setenv("NOVEL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NOVEL_AI_PROVIDER", "demo")
    with TestClient(create_app(start_executor=False)) as client:
        project = client.post("/api/v1/projects", json={"title": "升级验证"}).json()
        old = submit(client, project)
        base, jobs = paths(project)
    database_path = tmp_path / "projects" / project["id"] / "project.db"
    with closing(sqlite3.connect(database_path)) as database, database:
        command = database.execute("SELECT command,request_hash FROM ai_job_controls").fetchone()
        database.execute("DROP TABLE conversation_jobs")
        database.execute("DROP TABLE conversation_threads")
    with TestClient(create_app(start_executor=False)) as restarted:
        default = restarted.get(base).json()
        assert default["default_conversation_id"] == old["conversation_id"]
        detail = restarted.get(jobs + "/" + old["id"]).json()
        assert detail["context_snapshot"] == old["context_snapshot"]
        assert detail["result"] == old["result"]
    with closing(sqlite3.connect(database_path)) as database:
        assert (
            database.execute("SELECT command,request_hash FROM ai_job_controls").fetchone()
            == command
        )
        assert database.execute("PRAGMA foreign_key_check").fetchall() == []
