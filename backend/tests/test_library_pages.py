import base64
import re
from datetime import timedelta

from sqlalchemy import event, insert

from novel_harness.db.base import utc_now
from novel_harness.db.models import Entity, Idea


def test_projected_pages_full_counts_caps_and_anchor_deletion(client, project, monkeypatch):
    from novel_harness.services import library

    vault = client.app.state.vault_registry.require(project["id"])
    base = f"/api/v1/projects/{project['id']}"
    with vault.database.session_scope() as session:
        session.execute(
            insert(Entity),
            [
                dict(
                    id=f"e{i:04}",
                    project_id=project["id"],
                    kind="character",
                    name=f"人物{i:04}",
                    summary="长设定" * 8000,
                    is_pinned=i >= 995,
                )
                for i in range(1000)
            ],
        )
        session.execute(
            insert(Idea),
            [
                dict(
                    id="idea",
                    project_id=project["id"],
                    title="灵感",
                    content="全文" * 10000,
                    is_pinned=True,
                )
            ],
        )
    monkeypatch.setattr(
        library,
        "list_items",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("Projected pages must not hydrate full library")
        ),
    )
    page = client.get(base + "/library/page").json()
    assert len(page["items"]) == 50
    assert page["total"] == 1001
    assert page["counts"]["entity"] == 1000 and page["counts"]["idea"] == 1
    assert all("record" not in row and "content" not in row for row in page["items"])
    assert all(len(row["preview"]) <= 160 and len(row["title"]) <= 240 for row in page["items"])
    assert len(client.get(base + "/library/page", params={"limit": 1000}).json()["items"]) == 100
    pinned = client.get(base + "/library/page", params={"pinned": True, "limit": 5}).json()
    assert len(pinned["items"]) == 5 and pinned["total"] == 6
    category = client.get(base + "/library/page", params={"category": "idea"}).json()
    assert category["total"] == 1 and category["counts"]["entity"] == 1000
    seen = {(row["type"], row["id"]) for row in page["items"]}
    anchor = page["items"][-1]
    client.delete(base + f"/library/{anchor['type']}/{anchor['id']}")
    cursor = page["next_cursor"]
    other = client.post("/api/v1/projects", json={"title": "不同项目"}).json()
    assert (
        client.get(
            f"/api/v1/projects/{other['id']}/library/page", params={"cursor": cursor}
        ).status_code
        == 422
    )
    assert (
        client.get(
            base + "/library/page", params={"cursor": cursor, "category": "idea"}
        ).status_code
        == 422
    )
    assert (
        client.get(base + "/library/page", params={"cursor": cursor, "pinned": True}).status_code
        == 422
    )
    assert client.get(base + "/trash/page", params={"cursor": cursor}).status_code == 422
    while cursor:
        page = client.get(base + "/library/page", params={"cursor": cursor, "limit": 100}).json()
        keys = {(row["type"], row["id"]) for row in page["items"]}
        assert not seen.intersection(keys)
        seen.update(keys)
        cursor = page["next_cursor"]
    assert len(seen) == 1001
    trash = client.get(base + "/trash/page").json()
    assert trash["total"] == 1 and trash["items"][0]["purge_after"]
    assert client.get(base + "/library/page", params={"category": "unknown"}).status_code == 422
    assert client.get(base + "/library/page", params={"cursor": "x" * 4097}).status_code == 422
    assert client.get(base + "/library/page", params={"cursor": "invalid"}).status_code == 422


def test_full_detail_and_cross_vault_rejection(client, project):
    base = f"/api/v1/projects/{project['id']}"
    content = "完整设定" * 6000
    item = client.post(base + "/library/entity", json={"title": "人物", "content": content}).json()
    detail = base + f"/library/entity/{item['id']}"
    assert client.get(detail).json()["content"] == content
    other = client.post("/api/v1/projects", json={"title": "另一部书"}).json()
    other_base = f"/api/v1/projects/{other['id']}"
    assert client.get(other_base + f"/library/entity/{item['id']}").status_code == 404
    assert client.get(other_base + "/library/page").json()["total"] == 0
    client.delete(detail)
    assert client.get(detail).status_code == 404
    assert client.get(base + "/library/entity/missing").status_code == 404


def test_summary_eligibility_and_historical_detail(client, project, seeded_chapter):
    from novel_harness.db.models import ChapterSummary, ChapterVersion, StoryNode

    base = f"/api/v1/projects/{project['id']}"
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        version = ChapterVersion(
            id="version", project_id=project["id"], chapter_id=seeded_chapter, content="正文"
        )
        session.add(version)
        session.flush()
        for status in ("valid", "stale", "superseded"):
            session.add(
                ChapterSummary(
                    id=status,
                    project_id=project["id"],
                    chapter_id=seeded_chapter,
                    version_id=version.id,
                    title=status,
                    recap="总结",
                    status=status,
                    content_hash="hash",
                )
            )
    page = client.get(base + "/library/page", params={"category": "summary"}).json()
    assert [row["id"] for row in page["items"]] == ["valid"]
    assert page["items"][0]["status"] == "valid" and page["items"][0]["origin"] == "ai_generated"
    for status in ("stale", "superseded"):
        assert client.get(base + "/library/summary/" + status).json()["status"] == status
    with vault.database.session_scope() as session:
        chapter = session.get(StoryNode, seeded_chapter)
        chapter.deleted_at = utc_now()
        chapter.purge_after = utc_now() + timedelta(days=30)
    assert client.get(base + "/library/page", params={"category": "summary"}).json()["total"] == 0
    assert client.get(base + "/library/summary/valid").status_code == 404


def test_light_search_filters_before_cutoff_and_never_selects_full_blob(client, project):
    from novel_harness.services import retrieval

    base = f"/api/v1/projects/{project['id']}"
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        session.add_all(
            [
                Entity(
                    project_id=project["id"],
                    kind="character",
                    name=f"公共关键词{i:03}",
                    summary="公共关键词",
                )
                for i in range(105)
            ]
        )
        session.add(
            Idea(
                id="wanted", project_id=project["id"], title="靠后灵感", content="公共关键词" * 6000
            )
        )
    statements = []

    def observe(_conn, _cursor, sql, *_args):
        statements.append(sql)

    event.listen(vault.database.engine, "before_cursor_execute", observe)
    try:
        response = client.get(
            base + "/library/search",
            params={"q": "公共关键词", "category": "idea", "lightweight": True},
        )
    finally:
        event.remove(vault.database.engine, "before_cursor_execute", observe)
    assert response.status_code == 200
    result = response.json()
    assert [row["id"] for row in result["items"]] == ["wanted"]
    hit = result["items"][0]
    assert "record" not in hit and "content" not in hit
    assert hit["citation"]["id"] == "wanted" and hit["chunk"]["source_hash"]
    assert hit["reason"] and hit["channels"] and hit["score"]
    assert result["total_kind"] == "bounded_candidates"
    for sql in statements:
        scalar_free = re.sub(r"json_extract\([^)]*\)", "<scalar>", sql, flags=re.I)
        assert not re.search(r"\bd\s*\.\s*data\b", scalar_free, flags=re.I)
    with vault.database.session_scope() as session:
        full = retrieval.search(session, "靠后灵感", limit=100)
    assert full["items"][0]["record"]["content"] == "公共关键词" * 6000
    legacy = client.get(base + "/library/search", params={"q": "靠后灵感"}).json()["items"][0]
    assert legacy["record"]["content"] == "公共关键词" * 6000
    assert client.get(base + "/library/search", params={"category": "unknown"}).status_code == 422


def test_navigation_and_specialized_workspace_are_explicit_and_lazy(
    client, project, seeded_chapter
):
    base = f"/api/v1/projects/{project['id']}"
    client.post(base + "/library/idea", json={"title": "灵感", "content": "长正文" * 1000})
    navigation = client.get(base + "/workspace/navigation").json()
    assert set(navigation) == {"project", "nodes"}
    assert navigation["nodes"] and "summary" not in navigation["nodes"][0]
    ideas = client.get(base + "/workspace/views/ideas").json()
    assert set(ideas) == {"ideas"} and len(ideas["ideas"][0]["content"]) == 3000
    assert set(client.get(base + "/workspace/views/story").json()) == {"nodes", "plots"}
    assert set(client.get(base + "/workspace/views/entities").json()) == {"entities", "relations"}
    assert set(client.get(base + "/workspace/views/knowledge").json()) == {
        "canon_facts",
        "timeline",
    }
    assert set(client.get(base + "/workspace/views/style").json()) == {"styles", "memory_conflicts"}
    assert set(client.get(base + "/workspace/views/library").json()) == {"memory_conflicts"}
    assert client.get(base + "/workspace/views/unknown").status_code == 404
    assert "ideas" in client.get(base + "/workspace").json()


def test_manuscript_detail_reads_saved_version_not_working_copy(client, project, seeded_chapter):
    from novel_harness.db.models import ChapterDocument, ChapterVersion

    base = f"/api/v1/projects/{project['id']}"
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        session.add(
            ChapterVersion(
                id="saved-version",
                project_id=project["id"],
                chapter_id=seeded_chapter,
                content="已保存版本" * 5000,
            )
        )
        session.flush()
        document = session.get(ChapterDocument, seeded_chapter)
        if document is None:
            document = ChapterDocument(chapter_id=seeded_chapter, project_id=project["id"])
            session.add(document)
        document.content = "后来工作副本"
        document.current_version_id = "saved-version"
    result = client.get(base + "/library/manuscript/" + seeded_chapter)
    assert result.status_code == 200
    assert result.json()["content"] == "已保存版本" * 5000
    assert result.json()["record"] == {"chapter_id": seeded_chapter, "version_id": "saved-version"}


def test_cursor_invalid_shapes_and_depth_are_validation_errors(client, project):
    base = f"/api/v1/projects/{project['id']}/library/page"
    for value in ("null", "[]", "1", '{"after":null}', "[" * 1100 + "]" * 1100):
        cursor = base64.urlsafe_b64encode(value.encode()).decode()
        assert client.get(base, params={"cursor": cursor}).status_code == 422


def test_view_conflicts_use_all_canon_and_styles_not_library_page(client, project):
    from novel_harness.db.models import CanonFact, StyleProfile

    base = f"/api/v1/projects/{project['id']}"
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        session.add_all(
            [
                StyleProfile(
                    project_id=project["id"],
                    name=f"方案{i}",
                    is_active=True,
                    config={"rhythm": str(i)},
                )
                for i in range(2)
            ]
        )
        session.add_all(
            [CanonFact(project_id=project["id"], predicate="年龄", value=i) for i in range(2)]
        )
        session.execute(
            insert(Entity),
            [dict(project_id=project["id"], name=f"AAA{i}", kind="character") for i in range(60)],
        )
    page = client.get(base + "/library/page", params={"category": "entity"}).json()
    assert len(page["items"]) == 50 and all(row["type"] == "entity" for row in page["items"])
    for view in ("library", "style"):
        conflicts = client.get(base + "/workspace/views/" + view).json()["memory_conflicts"]
        assert {issue["code"] for issue in conflicts} == {"STYLE_CONFLICT", "CANON_OVERLAP"}
