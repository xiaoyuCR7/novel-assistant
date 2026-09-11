import pytest
from job_helpers import save_chapter
from test_quality_api import create_run, force_rewrite, inputs, setup_summary_handoff

from novel_harness.db.models import Entity


def coverage(client, project, **params):
    response = client.get(f"/api/v1/projects/{project['id']}/quality/runs/coverage", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def reviewed(client, project, chapter, instructions=""):
    job = create_run(
        client,
        project,
        inputs(client, project, [chapter]),
        mode="polish",
        instructions=instructions,
    ).json()
    assert client.app.state.job_executor.run_once()
    path = f"/api/v1/projects/{project['id']}/quality/runs/{job['id']}"
    assert client.get(path).json()["status"] == "succeeded"
    return path, job["id"]


def test_coverage_is_read_only_and_scores_do_not_imply_author_confirmation(
    client,
    project,
    seeded_chapter,
):
    path = f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}"
    save_chapter(client, path, json={"content": "邮差在街角停下，望向亮着灯的窗口。"})
    page = coverage(client, project)
    assert page["counts"] == {"unchecked": 1, "stale": 0, "pending": 0, "confirmed": 0}
    run, job_id = reviewed(client, project, seeded_chapter)
    page = coverage(client, project)
    assert page["items"][0]["status"] == "pending"
    assert page["items"][0]["job_id"] == job_id
    assert client.post(run + "/accept", json={"chapter_id": seeded_chapter}).status_code == 200
    assert coverage(client, project)["items"][0]["status"] == "confirmed"
    save_chapter(client, path, json={"content": "作者后来修改的另一段正文。"})
    assert coverage(client, project)["items"][0]["status"] == "stale"
    assert not client.app.state.job_executor.run_once()


def test_confirmed_coverage_expires_when_explicit_reference_changes(
    client, project, seeded_chapter
):
    base = f"/api/v1/projects/{project['id']}"
    entity = client.post(
        base + "/entities",
        json={
            "kind": "character",
            "name": "人物甲",
            "summary": "可靠的邮差",
            "profile": {},
            "state": {},
        },
    ).json()
    save_chapter(client, base + f"/chapters/{seeded_chapter}", json={"content": "邮差抵达街口。"})
    run, _ = reviewed(client, project, seeded_chapter, f"参考 [[ref:entity:{entity['id']}]]")
    assert client.post(run + "/accept", json={"chapter_id": seeded_chapter}).status_code == 200
    assert coverage(client, project)["items"][0]["status"] == "confirmed"
    with client.app.state.vault_registry.require(project["id"]).database.session_scope() as session:
        record = session.get(Entity, entity["id"])
        record.summary = "已经改变的人物设定"
        record.revision += 1
    assert coverage(client, project)["items"][0]["status"] == "stale"


def test_coverage_allows_expected_acceptance_and_summary_transitions(client, project):
    path, ids, _ = setup_summary_handoff(client, project)
    for chapter in ids[3:]:
        response = client.post(path + "/accept", json={"chapter_id": chapter})
        assert response.status_code == 200, response.text
    page = coverage(client, project, status="confirmed", limit=1)
    assert page["total"] == 2 and page["chapter_count"] == 5
    assert page["items"][0]["chapter_id"] == ids[3] and page["next_offset"] == 1
    next_page = coverage(client, project, status="confirmed", limit=1, offset=1)
    assert next_page["items"][0]["chapter_id"] == ids[4]
    other = client.post("/api/v1/projects", json={"title": "另一本小说"}).json()
    assert coverage(client, other)["chapter_count"] == 0


def test_partial_acceptance_remains_pending_and_new_contract_expires_report(
    client,
    project,
    seeded_chapter,
):
    path = f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}"
    save_chapter(client, path, json={"content": "旧正文第一行。\n第二行的叙述。"})
    force_rewrite(client)
    run, _ = reviewed(client, project, seeded_chapter)
    hunks = client.get(run + f"/chapters/{seeded_chapter}/diff").json()["hunks"]
    response = client.post(
        run + "/accept",
        json={
            "chapter_id": seeded_chapter,
            "selected_hunk_ids": [hunks[0]["id"]],
        },
    )
    assert response.status_code == 200, response.text
    item = coverage(client, project)["items"][0]
    assert item["status"] == "pending" and "组合稿" in item["reason"]
    current = client.get(path).json()
    save_chapter(
        client,
        path,
        json={
            "content": current["content"],
            "contract": {"purpose": "改变了本章的目的"},
        },
    )
    assert coverage(client, project)["items"][0]["status"] == "stale"


def test_coverage_of_empty_unreviewed_chapters_creates_no_documents(
    client, project, seeded_chapter
):
    from sqlalchemy import func, select

    from novel_harness.db.models import ChapterDocument

    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.job_session_scope() as session:
        before = session.scalar(select(func.count()).select_from(ChapterDocument))
    page = coverage(client, project)
    assert not page["items"][0]["can_review"]
    with vault.database.job_session_scope() as session:
        assert session.scalar(select(func.count()).select_from(ChapterDocument)) == before


def test_hard_setting_changes_expire_coverage_and_browsing_never_writes(
    client, project, seeded_chapter,
):
    from sqlalchemy import event

    from novel_harness.db.models import Project

    path = f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}"
    save_chapter(client, path, json={"content": "邮差追着落在风里的信，跑向街角。"})
    run, _ = reviewed(client, project, seeded_chapter)
    assert client.post(run + "/accept", json={"chapter_id": seeded_chapter}).status_code == 200
    database = client.app.state.vault_registry.require(project["id"]).database
    with database.session_scope() as session:
        session.get(Project, project["id"]).premise = "与旧任务不同的故事前提。"
    statements = []

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.strip().upper())

    event.listen(database.engine, "before_cursor_execute", record)
    try:
        page = coverage(client, project)
    finally:
        event.remove(database.engine, "before_cursor_execute", record)
    assert page["items"][0]["status"] == "stale"
    assert not any(sql.startswith(("INSERT", "UPDATE", "DELETE", "REPLACE")) for sql in statements)
    assert not any("AI_JOBS.CONTEXT_SNAPSHOT" in sql or "AI_JOBS.RESULT AS" in sql
                   or "AI_JOB_CONTROLS.COMMAND" in sql for sql in statements)


@pytest.mark.parametrize(
    "content",
    ["\t\n\u3000", " " * 50000 + "x"],
    ids=["unicode-whitespace-only", "padded-over-limit"],
)
def test_review_availability_matches_manuscript_validation(
    client, project, seeded_chapter, content
):
    base = f"/api/v1/projects/{project['id']}"
    save_chapter(client, base + f"/chapters/{seeded_chapter}", json={"content": content})
    page = coverage(client, project)
    assert page["items"][0]["can_review"] is False
    assert page["counts"] == {"unchecked": 1, "stale": 0, "pending": 0, "confirmed": 0}
    rejected = create_run(client, project, inputs(client, project, [seeded_chapter]), mode="polish")
    assert rejected.status_code == 422
    expected = "QUALITY_EMPTY_MANUSCRIPT" if not content.strip() else "QUALITY_MANUSCRIPT_TOO_LONG"
    assert rejected.json()["detail"]["code"] == expected
    assert not client.app.state.job_executor.run_once()
