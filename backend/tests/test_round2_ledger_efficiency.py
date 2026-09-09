"""Ledger acknowledgements scale by changed records, not completed chapter history."""

import hashlib
from datetime import datetime, timedelta

import pytest
from sqlalchemy import event, select, text

from novel_harness.ai.prompts import PROMPT_VERSION
from novel_harness.db.job_models import AIJobControl
from novel_harness.db.models import (
    AIJob,
    ChapterDocument,
    ChapterSummary,
    ChapterVersion,
    StoryNode,
)
from novel_harness.services.chapter_summaries import current_summary, finish_ledger


def seed_completed(database, project_id, count):
    ids = []
    with database.job_session_scope() as session:
        for number in range(count):
            chapter = StoryNode(
                project_id=project_id,
                kind="chapter",
                title=f"Chapter {number}",
                status="completed",
                order_index=number,
            )
            session.add(chapter)
            session.flush()
            version = ChapterVersion(
                project_id=project_id,
                chapter_id=chapter.id,
                content="saved chapter",
                source="manual",
            )
            session.add(version)
            session.flush()
            session.add(
                ChapterDocument(
                    project_id=project_id,
                    chapter_id=chapter.id,
                    content=version.content,
                    current_version_id=version.id,
                )
            )
            summary = ChapterSummary(
                project_id=project_id,
                chapter_id=chapter.id,
                version_id=version.id,
                title=chapter.title,
                recap="saved summary",
                provider="demo",
                content_hash=hashlib.sha256(version.content.encode()).hexdigest(),
            )
            session.add(summary)
            job = AIJob(
                project_id=project_id,
                chapter_id=chapter.id,
                task_type="chapter_summary",
                prompt_version=PROMPT_VERSION,
                status="succeeded",
                result={"saved": True},
            )
            session.add(job)
            session.flush()
            session.add(
                AIJobControl(
                    job_id=job.id,
                    operation="chapter_summary",
                    idempotency_key=f"history-{number}",
                    request_hash="hash",
                    command={},
                    effects={"summary_id": summary.id, "ledger_pending": False},
                )
            )
            ids.append(chapter.id)
    return ids


def test_chapter_node_flush_projects_its_manuscript_once(client, project):
    database = client.app.state.vault_registry.require(project["id"]).database
    chapter_id = seed_completed(database, project["id"], 1)[0]
    manuscript_writes = []

    def record(connection, cursor, statement, parameters, context, executemany):
        if (
            statement.startswith("INSERT INTO search_documents")
            and parameters[0] == f"manuscript:{chapter_id}"
        ):
            manuscript_writes.append(parameters)

    with database.job_session_scope() as session:
        session.get(StoryNode, chapter_id).title = "Renamed chapter"
        event.listen(database.engine, "before_cursor_execute", record)
        try:
            session.flush()
        finally:
            event.remove(database.engine, "before_cursor_execute", record)
    assert len(manuscript_writes) == 1
    with database.job_session_scope() as session:
        title = session.scalar(
            text("SELECT title FROM search_documents WHERE key=:key"),
            {"key": f"manuscript:{chapter_id}"},
        )
        assert title == "Renamed chapter · 正文版本"


@pytest.mark.parametrize("count", [1, 10, 50])
def test_chapter_save_does_not_reindex_unrelated_completed_chapters(client, project, count):
    database = client.app.state.vault_registry.require(project["id"]).database
    ids = seed_completed(database, project["id"], count)
    statements = []

    def record(connection, cursor, statement, parameters, context, executemany):
        statements.append((statement, parameters))

    event.listen(database.engine, "before_cursor_execute", record)
    try:
        saved = client.put(
            f"/api/v1/projects/{project['id']}/chapters/{ids[0]}",
            json={"content": "changed chapter", "contract": {}, "revision": 1},
        )
    finally:
        event.remove(database.engine, "before_cursor_execute", record)
    assert saved.status_code == 200, saved.text
    projections = [
        (sql, params)
        for sql, params in statements
        if sql.startswith("INSERT INTO search_documents")
    ]
    unrelated = [
        (sql, params)
        for sql, params in projections
        if any(chapter_id in str(params) for chapter_id in ids[1:])
    ]
    assert unrelated == []
    # A fixed bound rules out per-chapter reads and historical control lookups,
    # without brittle wall-clock assertions. Chapter count only changes rows.
    assert len(statements) < 90, len(statements)
    with database.job_session_scope() as session:
        assert session.get(StoryNode, ids[0]).status == "drafting"
        assert all(
            session.get(StoryNode, chapter_id).status == "completed" for chapter_id in ids[1:]
        )
        assert all(job.result == {"saved": True} for job in session.scalars(select(AIJob)))
        assert session.scalar(text("SELECT count(*) FROM pending_projections")) == 0


@pytest.mark.parametrize(
    "latest_status, latest_deleted, latest_hash, chapter_deleted, expected",
    [
        ("valid", False, "same", False, "completed"),
        ("stale", False, "same", False, "drafting"),
        ("superseded", False, "same", False, "drafting"),
        ("valid", False, "different", False, "drafting"),
        ("stale", True, "different", False, "completed"),
        ("valid", False, "same", True, "drafting"),
    ],
)
def test_batched_latest_summary_preserves_ties_deletion_status_and_hash(
    client,
    project,
    latest_status,
    latest_deleted,
    latest_hash,
    chapter_deleted,
    expected,
):
    database = client.app.state.vault_registry.require(project["id"]).database
    chapter_id = seed_completed(database, project["id"], 1)[0]
    fixed = datetime(2026, 1, 1)
    with database.job_session_scope() as session:
        chapter = session.get(StoryNode, chapter_id)
        chapter.status = "drafting"
        if chapter_deleted:
            chapter.deleted_at = fixed
        original = current_summary(session, chapter_id)
        original.created_at = fixed - timedelta(days=1)
        for identifier, state in [("tie-a", "valid"), ("tie-z", latest_status)]:
            session.add(
                ChapterSummary(
                    id=identifier,
                    project_id=project["id"],
                    chapter_id=chapter_id,
                    version_id=original.version_id,
                    title="Latest",
                    recap="latest summary",
                    content_hash=(
                        original.content_hash
                        if identifier == "tie-a" or latest_hash == "same"
                        else "different"
                    ),
                    status=state,
                    created_at=fixed,
                    deleted_at=fixed if identifier == "tie-z" and latest_deleted else None,
                )
            )
    with database.job_session_scope() as session:
        finish_ledger(session)
    with database.job_session_scope() as session:
        chapter = session.scalar(
            select(StoryNode)
            .where(StoryNode.id == chapter_id)
            .execution_options(include_deleted=True)
        )
        assert chapter.status == expected
        if expected == "completed":
            # A real transition must still refresh the search projection.
            record = session.scalar(
                text("SELECT data FROM search_documents WHERE key=:key"),
                {"key": f"node:{chapter_id}"},
            )
            assert '"status": "completed"' in record
