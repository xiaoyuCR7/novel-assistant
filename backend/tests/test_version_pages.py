from datetime import UTC, datetime, timedelta

from sqlalchemy import event

from novel_harness.db.models import ChapterVersion


def _add_versions(
    client,
    project_id,
    chapter_id,
    *,
    count,
    content,
    created_at,
    id_prefix="version",
    source="manual",
    summary="完成章节快照",
):
    database = client.app.state.vault_registry.require(project_id).database
    ids = []
    with database.session_scope() as session:
        for index in range(count):
            version_id = f"{id_prefix}-{index:04d}"
            session.add(
                ChapterVersion(
                    id=version_id,
                    project_id=project_id,
                    chapter_id=chapter_id,
                    content=content,
                    summary=summary,
                    source=source,
                    word_count=index,
                    created_at=created_at + timedelta(seconds=index),
                    updated_at=created_at + timedelta(seconds=index),
                )
            )
            ids.append(version_id)
    return ids


def _page_url(project_id, chapter_id):
    return f"/api/v1/projects/{project_id}/chapters/{chapter_id}/versions/page"


def test_version_page_is_bounded_ordered_and_excludes_content(client, project, seeded_chapter):
    base = datetime(2026, 1, 1, tzinfo=UTC)
    _add_versions(
        client,
        project["id"],
        seeded_chapter,
        count=3,
        content="不应出现在分页响应的正文",
        created_at=base,
        source="ai",
        summary="AI 改写章节",
    )

    response = client.get(_page_url(project["id"], seeded_chapter), params={"limit": 1})

    assert response.status_code == 200
    page = response.json()
    assert [item["id"] for item in page["items"]] == ["version-0002"]
    assert page["next_cursor"] == "version-0002"
    assert "content" not in page["items"][0]
    assert "reason" not in page["items"][0]
    assert page["items"][0]["source"] == "ai"
    assert page["items"][0]["summary"] == "AI 改写章节"
    too_large = client.get(_page_url(project["id"], seeded_chapter), params={"limit": 101})
    assert too_large.status_code == 422


def test_version_page_keyset_pagination_is_stable_for_equal_timestamps(
    client, project, seeded_chapter,
):
    base = datetime(2026, 1, 1, tzinfo=UTC)
    ids = _add_versions(
        client,
        project["id"],
        seeded_chapter,
        count=500,
        content="正文",
        created_at=base,
    )
    database = client.app.state.vault_registry.require(project["id"]).database
    with database.session_scope() as session:
        for version_id in ids:
            session.get(ChapterVersion, version_id).created_at = base

    collected = []
    before = None
    while True:
        response = client.get(
            _page_url(project["id"], seeded_chapter),
            params={"limit": 50, "before": before} if before else {"limit": 50},
        )
        assert response.status_code == 200
        page = response.json()
        collected.extend(item["id"] for item in page["items"])
        before = page["next_cursor"]
        if before is None:
            break

    assert collected == sorted(ids, reverse=True)
    assert len(collected) == len(set(collected))


def test_version_page_cursor_uses_composite_range_seek(client, project, seeded_chapter):
    _add_versions(
        client,
        project["id"],
        seeded_chapter,
        count=500,
        content="正文",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    database = client.app.state.vault_registry.require(project["id"]).database
    statements = []

    def record(conn, cursor, statement, params, context, many):
        statements.append((statement, params))

    event.listen(database.engine, "before_cursor_execute", record)
    try:
        response = client.get(
            _page_url(project["id"], seeded_chapter),
            params={"limit": 50, "before": "version-0100"},
        )
    finally:
        event.remove(database.engine, "before_cursor_execute", record)

    assert response.status_code == 200
    assert response.json()["next_cursor"] == "version-0050"
    list_queries = [
        (statement, params)
        for statement, params in statements
        if "from chapter_versions" in statement.lower() and "order by" in statement.lower()
    ]
    assert len(list_queries) == 1
    statement, params = list_queries[0]
    with database.engine.connect() as connection:
        plan = connection.exec_driver_sql("EXPLAIN QUERY PLAN " + statement, params).all()
    detail = " ".join(row[3] for row in plan)
    assert "ix_chapter_versions_chapter_created_id_desc" in detail
    assert "USE TEMP B-TREE FOR ORDER BY" not in detail
    assert "(created_at,id)<(?,?)" in detail.replace(" ", "")


def test_version_page_rejects_missing_and_cross_chapter_cursors(client, project, seeded_chapter):
    base = datetime(2026, 1, 1, tzinfo=UTC)
    _add_versions(client, project["id"], seeded_chapter, count=1, content="正文", created_at=base)
    other = client.post(
        f"/api/v1/projects/{project['id']}/nodes",
        json={"kind": "chapter", "title": "第二章", "order_index": 2},
    ).json()
    _add_versions(
        client,
        project["id"],
        other["id"],
        count=1,
        content="别章正文",
        created_at=base,
        id_prefix="foreign",
    )

    for cursor in ("not-a-version", "foreign-0000"):
        response = client.get(
            _page_url(project["id"], seeded_chapter), params={"before": cursor},
        )
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "INVALID_VERSION_CURSOR"
        assert response.json()["detail"]["message"]


def test_legacy_version_list_still_includes_content(client, project, seeded_chapter):
    base = datetime(2026, 1, 1, tzinfo=UTC)
    _add_versions(
        client, project["id"], seeded_chapter, count=1, content="旧接口正文", created_at=base,
    )

    response = client.get(
        f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}/versions",
    )

    assert response.status_code == 200
    assert response.json()[0]["content"] == "旧接口正文"


def test_version_page_large_history_has_small_payload(client, project, seeded_chapter):
    _add_versions(
        client,
        project["id"],
        seeded_chapter,
        count=500,
        content="x" * 8_000,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    database = client.app.state.vault_registry.require(project["id"]).database
    statements = []

    def record(conn, cursor, statement, params, context, many):
        statements.append((statement, params))

    event.listen(database.engine, "before_cursor_execute", record)
    try:
        response = client.get(_page_url(project["id"], seeded_chapter), params={"limit": 50})
    finally:
        event.remove(database.engine, "before_cursor_execute", record)

    assert response.status_code == 200
    assert len(response.json()["items"]) == 50
    assert len(response.content) < 100_000
    assert all("content" not in item for item in response.json()["items"])
    version_queries = [
        (statement, params)
        for statement, params in statements
        if "from chapter_versions" in statement.lower()
    ]
    assert len(version_queries) == 1
    statement, params = version_queries[0]
    assert "select *" not in statement.lower()
    assert "content" not in statement.lower()
    assert "limit" in statement.lower()
    assert 51 in params
