from sqlalchemy import text

from novel_harness.schemas.library import MaterialPatch
from novel_harness.services import chapter_summaries, library


def test_rollback_cannot_replace_ledger(client, project, seeded_chapter):
    database = client.app.state.vault_registry.require(project["id"]).database
    path = database.path.parent / "rag/chapter-continuity-ledger.md"
    with database.session_scope() as session:
        chapter_summaries.write_ledger(session)
    before = path.read_text(encoding="utf-8")
    try:
        with database.session_scope() as session:
            chapter_summaries.write_ledger(session)
            assert path.read_text(encoding="utf-8") == before
            # A unique uncommitted record must never become a file projection.
            from novel_harness.db.models import ChapterSummary, ChapterVersion

            version = ChapterVersion(
                project_id=project["id"],
                chapter_id=seeded_chapter,
                content="draft",
                source="manual",
            )
            session.add(version)
            session.flush()
            session.add(
                ChapterSummary(
                    project_id=project["id"],
                    chapter_id=seeded_chapter,
                    version_id=version.id,
                    title="UNCOMMITTED_SENTINEL",
                    content_hash="hash",
                    recap="sentinel",
                    details={},
                )
            )
            session.flush()
            chapter_summaries.write_ledger(session)
            raise RuntimeError("rollback")
    except RuntimeError:
        pass
    assert path.read_text(encoding="utf-8") == before


def test_rename_updates_manuscript_projection(client, project, seeded_chapter):
    from novel_harness.services.versions import create_version

    database = client.app.state.vault_registry.require(project["id"]).database
    with database.session_scope() as session:
        create_version(session, seeded_chapter, content="正文", source="manual")
        item = library.require_item(session, "node", seeded_chapter)
        library.edit_item(
            session, "node", item.id, MaterialPatch(revision=item.revision, title="新章名")
        )
    with database.session_scope() as session:
        title = session.scalar(
            text("SELECT title FROM search_documents WHERE source_type='manuscript'")
        )
        assert title == "新章名 · 正文版本"


def test_summary_visibility_restore_and_projection_failure(
    client, project, seeded_chapter, monkeypatch
):
    from job_helpers import complete_summary, save_chapter

    from novel_harness.services.projections import drain_ledger

    base = f"/api/v1/projects/{project['id']}"
    chapter_url = base + f"/chapters/{seeded_chapter}"
    save_chapter(client, chapter_url, json={"content": "原始正文"})
    summary = complete_summary(client, chapter_url).json()
    client.delete(base + f"/library/node/{seeded_chapter}")
    assert not any(item["type"] == "summary" for item in client.get(base + "/library").json())
    client.post(base + f"/trash/node/{seeded_chapter}/restore")
    client.delete(base + f"/library/summary/{summary['id']}")
    client.post(base + f"/trash/summary/{summary['id']}/restore")
    assert client.get(chapter_url).json()["status"] == "completed"
    database = client.app.state.vault_registry.require(project["id"]).database
    import json

    with database.job_session_scope() as session:
        row = session.scalar(
            text("SELECT data FROM search_documents WHERE key=:key"),
            {"key": f"node:{seeded_chapter}"},
        )
        assert json.loads(row)["record"]["status"] == "completed"
    original = chapter_summaries._write_ledger_file
    monkeypatch.setattr(
        chapter_summaries, "_write_ledger_file", lambda _: (_ for _ in ()).throw(OSError())
    )
    edited = client.patch(
        chapter_url + "/summary",
        json={
            "summary_id": summary["id"],
            "revision": client.get(chapter_url + "/summary").json()["revision"],
            "recap": "磁盘失败时仍保存作者修订",
        },
    )
    assert edited.status_code == 200
    with database.job_session_scope() as session:
        assert session.scalar(text("SELECT count(*) FROM pending_projections")) == 1
    monkeypatch.setattr(chapter_summaries, "_write_ledger_file", original)
    assert drain_ledger(database)
    assert "磁盘失败时仍保存作者修订" in (
        database.path.parent / "rag/chapter-continuity-ledger.md"
    ).read_text(encoding="utf-8")
