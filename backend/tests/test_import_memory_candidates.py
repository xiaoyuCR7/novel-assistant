from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest
from sqlalchemy import event, func, select, text

from novel_harness.db.models import (
    CanonFact,
    ChapterSummary,
    ChapterVersion,
    Entity,
    EntityRelation,
    EntityState,
    GeneratedMemoryCandidate,
    Idea,
    ImportBatch,
    MemoryCandidate,
    PlotThread,
    Project,
    SourceDocument,
    StoryNode,
    StyleRule,
    TimelineEvent,
)
from novel_harness.services.job_state import command_hash
from novel_harness.services.source_identity import source_document_identity_hash


def _seed_candidate(
    client,
    project_id: str,
    *,
    kind="canon",
    payload=None,
    evidence=None,
    chapter_id=None,
    source_version_id=None,
):
    content = "雾潮每七日退一次。阿雾记下了规则。"
    vault = client.app.state.vault_registry.require(project_id, job_only=True)
    with vault.database.session_scope() as session:
        batch = ImportBatch(
            project_id=project_id,
            source_kind="folder",
            manifest={},
            status="completed",
        )
        session.add(batch)
        session.flush()
        source = SourceDocument(
            project_id=project_id,
            import_batch_id=batch.id,
            chapter_id=chapter_id,
            relative_path="设定/世界.md",
            stored_path=f"imports/{batch.id}/sources/设定/世界.md",
            category="world",
            title="世界观",
            content=content,
            encoding="utf-8",
            size_bytes=len(content.encode()),
            byte_hash=hashlib.sha256(content.encode()).hexdigest(),
            content_hash=hashlib.sha256(content.encode()).hexdigest(),
            content_revision=1,
        )
        session.add(source)
        session.flush()
        source_hash = source_document_identity_hash(source)
        actual_payload = payload or {"predicate": "雾潮规则", "value": "每七日退潮"}
        actual_evidence = evidence or [{"quote": "雾潮每七日退一次", "start": 0, "end": 8}]
        dedupe_key = command_hash(
            {
                "source_document_id": source.id,
                "source_hash": source_hash,
                "kind": kind,
                "payload": actual_payload,
                "evidence": actual_evidence,
            }
        )
        candidate = MemoryCandidate(
            project_id=project_id,
            import_batch_id=batch.id,
            source_document_id=source.id,
            chapter_id=chapter_id,
            source_version_id=source_version_id,
            kind=kind,
            payload=actual_payload,
            evidence=actual_evidence,
            source_hash=source_hash,
            dedupe_key=dedupe_key,
        )
        session.add(candidate)
        session.flush()
        return candidate.id, source.id


def _duplicate_promoted_record(session, model, original):
    excluded = {"id", "created_at", "updated_at"}
    values = {
        column.name: getattr(original, column.name)
        for column in model.__table__.columns
        if column.name not in excluded
    }
    duplicate = model(**values)
    session.add(duplicate)
    session.flush()
    return duplicate


def test_import_candidate_lists_and_confirms_exact_evidence(client, project):
    candidate_id, _ = _seed_candidate(client, project["id"])
    url = f"/api/v1/projects/{project['id']}/memory-candidates"

    page = client.get(url, params={"status": "pending", "limit": 1})
    assert page.status_code == 200
    assert [item["id"] for item in page.json()["items"]] == [candidate_id]

    confirmed = client.post(
        f"{url}/{candidate_id}/confirm",
        json={"revision": 1, "decision": "confirm"},
    )
    assert confirmed.status_code == 200
    body = confirmed.json()
    assert body["status"] == "confirmed"
    assert body["revision"] == 2
    assert body["promoted_type"] == "canon"

    vault = client.app.state.vault_registry.require(project["id"], job_only=True)
    with vault.database.session_scope() as session:
        assert session.scalar(select(func.count()).select_from(CanonFact)) == 1


def test_import_candidate_rejects_changed_evidence_without_promotion(client, project):
    candidate_id, _ = _seed_candidate(
        client,
        project["id"],
        evidence=[{"quote": "雾潮每七日退一次", "start": 1, "end": 10}],
    )
    response = client.post(
        f"/api/v1/projects/{project['id']}/memory-candidates/{candidate_id}/confirm",
        json={"revision": 1, "decision": "confirm"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "CANDIDATE_EVIDENCE_CHANGED"
    vault = client.app.state.vault_registry.require(project["id"], job_only=True)
    with vault.database.session_scope() as session:
        assert session.scalar(select(func.count()).select_from(CanonFact)) == 0


def test_unified_candidate_pages_keep_import_and_generated_rows(client, project, seeded_chapter):
    import_id, _ = _seed_candidate(client, project["id"])
    vault = client.app.state.vault_registry.require(project["id"], job_only=True)
    with vault.database.session_scope() as session:
        from novel_harness.services.versions import create_version

        version = create_version(
            session, seeded_chapter, content="铜钥匙", source="manual"
        )
        generated = GeneratedMemoryCandidate(
            project_id=project["id"],
            chapter_id=seeded_chapter,
            source_version_id=version.id,
            kind="canon",
            payload={
                "subject_entity_id": None,
                "predicate": "钥匙",
                "value": "铜",
                "valid_from_node_id": None,
                "valid_to_node_id": None,
            },
            evidence={"quote": "铜钥匙", "start": 0, "end": 3},
            identity_hash=command_hash({"generated": import_id}),
        )
        session.add(generated)
        session.flush()
        generated_id = generated.id

    url = f"/api/v1/projects/{project['id']}/memory-candidates"
    first = client.get(url, params={"limit": 1})
    assert first.status_code == 200
    assert first.json()["next_cursor"]
    second = client.get(
        url, params={"limit": 1, "cursor": first.json()["next_cursor"]}
    )
    assert second.status_code == 200
    rows = first.json()["items"] + second.json()["items"]
    assert {row["id"] for row in rows} == {import_id, generated_id}
    assert {row["origin"] for row in rows} == {"import", "generated"}
    assert first.json()["total"] == 2


def test_unified_candidate_page_can_filter_to_import_origin(client, project, seeded_chapter):
    import_id, _ = _seed_candidate(client, project["id"])
    vault = client.app.state.vault_registry.require(project["id"], job_only=True)
    with vault.database.session_scope() as session:
        from novel_harness.services.versions import create_version

        version = create_version(session, seeded_chapter, content="生成候选", source="manual")
        generated = GeneratedMemoryCandidate(
            project_id=project["id"],
            chapter_id=seeded_chapter,
            source_version_id=version.id,
            kind="canon",
            payload={
                "subject_entity_id": None,
                "predicate": "来源",
                "value": "生成",
                "valid_from_node_id": None,
                "valid_to_node_id": None,
            },
            evidence={"quote": "生成候选", "start": 0, "end": 4},
            identity_hash=command_hash({"origin-filter": import_id}),
        )
        session.add(generated)

    response = client.get(
        f"/api/v1/projects/{project['id']}/memory-candidates",
        params={"origin": "import", "status": "pending"},
    )

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [import_id]
    assert response.json()["total"] == 1
    assert response.json()["counts"] == {"pending": 1}


def test_unified_candidate_page_limits_each_origin_query_in_sql(
    client, project, seeded_chapter
):
    project_id = project["id"]
    import_id, source_id = _seed_candidate(client, project_id)
    del import_id
    vault = client.app.state.vault_registry.require(project_id, job_only=True)
    with vault.database.session_scope() as session:
        from novel_harness.services.versions import create_version

        source = session.get(SourceDocument, source_id)
        version = create_version(
            session, seeded_chapter, content="有界分页", source="manual"
        )
        session.execute(
            MemoryCandidate.__table__.insert(),
            [
                {
                    "id": f"import-page-{index:05d}",
                    "project_id": project_id,
                    "import_batch_id": source.import_batch_id,
                    "source_document_id": source.id,
                    "chapter_id": None,
                    "source_version_id": None,
                    "kind": "canon",
                    "payload": {"predicate": f"import-{index}", "value": index},
                    "evidence": [{"quote": "雾潮", "start": 0, "end": 2}],
                    "source_hash": source_document_identity_hash(source),
                    "dedupe_key": command_hash({"import-page": index}),
                    "status": "pending",
                    "revision": 1,
                    "conflict": {},
                }
                for index in range(5_000)
            ],
        )
        session.execute(
            GeneratedMemoryCandidate.__table__.insert(),
            [
                {
                    "id": f"generated-page-{index:05d}",
                    "project_id": project_id,
                    "chapter_id": seeded_chapter,
                    "source_version_id": version.id,
                    "kind": "canon",
                    "payload": {
                        "subject_entity_id": None,
                        "predicate": f"generated-{index}",
                        "value": index,
                        "valid_from_node_id": None,
                        "valid_to_node_id": None,
                    },
                    "evidence": {"quote": "有界", "start": 0, "end": 2},
                    "identity_hash": command_hash({"generated-page": index}),
                    "status": "pending",
                    "revision": 1,
                }
                for index in range(5_000)
            ],
        )

    candidate_selects = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        sql = " ".join(statement.lower().split())
        if (
            " from generated_memory_candidates" in sql
            or " from memory_candidates" in sql
        ) and "payload" in sql:
            candidate_selects.append(sql)

    event.listen(vault.database.engine, "before_cursor_execute", capture)
    try:
        response = client.get(
            f"/api/v1/projects/{project_id}/memory-candidates",
            params={"limit": 3},
        )
    finally:
        event.remove(vault.database.engine, "before_cursor_execute", capture)
    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) == 3
    assert response.json()["total"] == 10_001
    assert len(candidate_selects) == 2
    assert all(" limit " in statement for statement in candidate_selects)


def test_unified_candidate_list_accepts_saved_v1_generated_cursor(
    client, project, seeded_chapter
):
    vault = client.app.state.vault_registry.require(project["id"], job_only=True)
    with vault.database.session_scope() as session:
        from novel_harness.services.versions import create_version

        version = create_version(session, seeded_chapter, content="旧游标", source="manual")
        generated = GeneratedMemoryCandidate(
            project_id=project["id"],
            chapter_id=seeded_chapter,
            source_version_id=version.id,
            kind="canon",
            payload={
                "subject_entity_id": None,
                "predicate": "旧游标",
                "value": True,
                "valid_from_node_id": None,
                "valid_to_node_id": None,
            },
            evidence={"quote": "旧游标", "start": 0, "end": 3},
            identity_hash=command_hash({"legacy": version.id}),
        )
        session.add(generated)
        session.flush()
        generated_id = generated.id
    cursor = base64.urlsafe_b64encode(
        json.dumps(
            {
                "v": 1,
                "scope": [project["id"], seeded_chapter, "pending"],
                "after": generated_id,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).decode()
    response = client.get(
        f"/api/v1/projects/{project['id']}/memory-candidates",
        params={
            "chapter_id": seeded_chapter,
            "status": "pending",
            "cursor": cursor,
        },
    )
    assert response.status_code == 200
    assert response.json()["items"] == []


def test_unified_cursor_orders_same_id_and_timestamp_by_origin(
    client, project, seeded_chapter
):
    candidate_id, _ = _seed_candidate(client, project["id"])
    vault = client.app.state.vault_registry.require(project["id"], job_only=True)
    with vault.database.session_scope() as session:
        from novel_harness.services.versions import create_version

        imported = session.get(MemoryCandidate, candidate_id)
        version = create_version(
            session, seeded_chapter, content="同键", source="manual"
        )
        session.add(
            GeneratedMemoryCandidate(
                id=candidate_id,
                project_id=project["id"],
                chapter_id=seeded_chapter,
                source_version_id=version.id,
                kind="canon",
                payload={
                    "subject_entity_id": None,
                    "predicate": "同键",
                    "value": True,
                    "valid_from_node_id": None,
                    "valid_to_node_id": None,
                },
                evidence={"quote": "同键", "start": 0, "end": 2},
                identity_hash=command_hash({"same-key": candidate_id}),
                created_at=imported.created_at,
            )
        )
    url = f"/api/v1/projects/{project['id']}/memory-candidates"
    first = client.get(url, params={"limit": 1})
    second = client.get(
        url, params={"limit": 1, "cursor": first.json()["next_cursor"]}
    )
    assert first.status_code == second.status_code == 200
    assert [first.json()["items"][0]["origin"], second.json()["items"][0]["origin"]] == [
        "generated",
        "import",
    ]
    assert first.json()["items"][0]["id"] == second.json()["items"][0]["id"]
    assert second.json()["next_cursor"] is None


def test_candidate_page_indexes_are_created_and_repaired(client, project):
    vault = client.app.state.vault_registry.require(project["id"], job_only=True)
    expected = {
        "memory_candidates": {
            "ix_memory_candidates_project_created_id": (
                "project_id",
                "created_at",
                "id",
            ),
            "ix_memory_candidates_project_status_created_id": (
                "project_id",
                "status",
                "created_at",
                "id",
            ),
            "ix_memory_candidates_project_chapter_created_id": (
                "project_id",
                "chapter_id",
                "created_at",
                "id",
            ),
            "ix_memory_candidates_project_status_chapter_created_id": (
                "project_id",
                "status",
                "chapter_id",
                "created_at",
                "id",
            ),
        },
        "generated_memory_candidates": {
            "ix_generated_memory_candidates_project_created_id": (
                "project_id",
                "created_at",
                "id",
            ),
            "ix_generated_memory_candidates_project_status_created_id": (
                "project_id",
                "status",
                "created_at",
                "id",
            ),
            "ix_generated_memory_candidates_project_chapter_created_id": (
                "project_id",
                "chapter_id",
                "created_at",
                "id",
            ),
            "ix_generated_memory_candidates_project_status_chapter_created_id": (
                "project_id",
                "status",
                "chapter_id",
                "created_at",
                "id",
            ),
        },
    }
    with vault.database.engine.begin() as connection:
        for table_indexes in expected.values():
            for index_name in table_indexes:
                connection.exec_driver_sql(f'DROP INDEX IF EXISTS "{index_name}"')
    vault.database.create_schema()
    with vault.database.engine.connect() as connection:
        for table, table_indexes in expected.items():
            indexes = {
                row[1]: row
                for row in connection.exec_driver_sql(f'PRAGMA index_list("{table}")')
            }
            for index_name, expected_columns in table_indexes.items():
                assert index_name in indexes
                columns = tuple(
                    row[2]
                    for row in connection.exec_driver_sql(
                        f'PRAGMA index_info("{index_name}")'
                    )
                )
                assert columns == expected_columns


@pytest.mark.parametrize(
    ("where", "index_suffix"),
    [
        ("project_id=:project_id", "project_created_id"),
        (
            "project_id=:project_id AND status=:status",
            "project_status_created_id",
        ),
        (
            "project_id=:project_id AND chapter_id=:chapter_id",
            "project_chapter_created_id",
        ),
        (
            "project_id=:project_id AND status=:status AND chapter_id=:chapter_id",
            "project_status_chapter_created_id",
        ),
    ],
)
@pytest.mark.parametrize(
    "table", ["memory_candidates", "generated_memory_candidates"]
)
def test_candidate_page_query_plan_uses_scope_order_index(
    client, project, seeded_chapter, table, where, index_suffix
):
    vault = client.app.state.vault_registry.require(project["id"], job_only=True)
    params = {
        "project_id": project["id"],
        "status": "pending",
        "chapter_id": seeded_chapter,
    }
    with vault.database.engine.connect() as connection:
        plan = connection.exec_driver_sql(
            f"EXPLAIN QUERY PLAN SELECT id FROM {table} WHERE {where} "
            "ORDER BY created_at,id LIMIT 51",
            params,
        ).fetchall()
    detail = "\n".join(str(row[3]) for row in plan)
    table_prefix = table.removesuffix("s")
    assert f"ix_{table_prefix}s_{index_suffix}" in detail
    assert "USE TEMP B-TREE FOR ORDER BY" not in detail


def test_candidate_id_collision_is_stably_ambiguous(client, project, seeded_chapter):
    candidate_id, _ = _seed_candidate(client, project["id"])
    vault = client.app.state.vault_registry.require(project["id"], job_only=True)
    with vault.database.session_scope() as session:
        from novel_harness.services.versions import create_version

        version = create_version(session, seeded_chapter, content="冲突", source="manual")
        session.add(
            GeneratedMemoryCandidate(
                id=candidate_id,
                project_id=project["id"],
                chapter_id=seeded_chapter,
                source_version_id=version.id,
                kind="canon",
                payload={
                    "subject_entity_id": None,
                    "predicate": "冲突",
                    "value": True,
                    "valid_from_node_id": None,
                    "valid_to_node_id": None,
                },
                evidence={"quote": "冲突", "start": 0, "end": 2},
                identity_hash=command_hash({"collision": candidate_id}),
            )
        )

    response = client.post(
        f"/api/v1/projects/{project['id']}/memory-candidates/{candidate_id}/confirm",
        json={"revision": 1},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "MEMORY_CANDIDATE_AMBIGUOUS"


def test_bulk_candidate_id_collision_rolls_back_every_import_candidate(
    client, project, seeded_chapter
):
    collision_id, _ = _seed_candidate(client, project["id"])
    other_id, _ = _seed_candidate(
        client,
        project["id"],
        payload={"predicate": "其他规则", "value": "值"},
    )
    vault = client.app.state.vault_registry.require(project["id"], job_only=True)
    with vault.database.session_scope() as session:
        from novel_harness.services.versions import create_version

        version = create_version(session, seeded_chapter, content="冲突", source="manual")
        session.add(
            GeneratedMemoryCandidate(
                id=collision_id,
                project_id=project["id"],
                chapter_id=seeded_chapter,
                source_version_id=version.id,
                kind="canon",
                payload={
                    "subject_entity_id": None,
                    "predicate": "冲突",
                    "value": True,
                    "valid_from_node_id": None,
                    "valid_to_node_id": None,
                },
                evidence={"quote": "冲突", "start": 0, "end": 2},
                identity_hash=command_hash({"bulk-collision": collision_id}),
            )
        )
    response = client.post(
        f"/api/v1/projects/{project['id']}/memory-candidates/bulk-confirm",
        json={"entries": [
            {"candidate_id": other_id, "revision": 1},
            {"candidate_id": collision_id, "revision": 1},
        ]},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "MEMORY_CANDIDATE_AMBIGUOUS"
    with vault.database.session_scope() as session:
        assert session.scalar(select(func.count()).select_from(CanonFact)) == 0
        assert session.get(MemoryCandidate, other_id).status == "pending"
        assert session.get(MemoryCandidate, collision_id).status == "pending"


@pytest.mark.parametrize(
    ("kind", "model"),
    [
        ("entity", Entity),
        ("relation", EntityRelation),
        ("canon", CanonFact),
        ("timeline", TimelineEvent),
        ("plot", PlotThread),
        ("node", StoryNode),
        ("node_update", StoryNode),
        ("style_rule", StyleRule),
        ("idea", Idea),
        ("entity_state", EntityState),
        ("chapter_summary", ChapterSummary),
    ],
)
def test_each_import_candidate_kind_promotes_through_typed_path(
    client, project, kind, model
):
    project_id = project["id"]
    source_node = client.post(
        f"/api/v1/projects/{project_id}/nodes",
        json={"kind": "chapter", "title": f"来源章-{kind}", "order_index": 1},
    ).json()
    chapter_id = source_node["id"]
    vault = client.app.state.vault_registry.require(project_id, job_only=True)
    with vault.database.session_scope() as session:
        from novel_harness.services.versions import create_version

        source_version = create_version(
            session, chapter_id, content="已导入正文", source="import"
        )
        source_version_id = source_version.id
    if kind == "entity":
        payload = {
            "kind": "character",
            "name": "阿雾",
            "summary": "邮差",
            "profile": {},
            "state": {},
        }
    elif kind == "relation":
        for name in ("阿雾", "林鐘"):
            response = client.post(
                f"/api/v1/projects/{project_id}/entities",
                json={"kind": "character", "name": name},
            )
            assert response.status_code == 201
        payload = {
            "source_entity_name": " 阿雾 ",
            "target_entity_name": "林鐘",
            "relation_type": "盟友",
            "description": "同行",
        }
    elif kind == "canon":
        payload = {"predicate": "雾潮规则", "value": "每七日退潮"}
    elif kind == "timeline":
        payload = {"title": "第一次退潮", "story_time": "第七日", "sort_key": 7}
    elif kind == "plot":
        payload = {"kind": "main", "title": "送信", "promise": "送完亡者之信"}
    elif kind == "node":
        payload = {"kind": "chapter", "title": "新章", "order_index": 9}
    elif kind == "style_rule":
        payload = {"instruction": "保持克制叙事", "weight": 1.0}
    elif kind == "idea":
        payload = {"title": "钟楼回声", "content": "钟声只被死者听见"}
    elif kind in {"node_update", "chapter_summary"}:
        if kind == "node_update":
            payload = {
                "node_id": chapter_id,
                "expected_revision": 1,
                "summary": "已完成开场",
                "target_words": 3200,
            }
        else:
            payload = {
                "chapter_id": chapter_id,
                "version_id": source_version_id,
                "content_hash": hashlib.sha256("已导入正文".encode()).hexdigest(),
                "title": "已导入章",
                "recap": "主角抵达雾城。",
                "details": {"events": ["抵达"]},
            }
    else:
        client.post(
            f"/api/v1/projects/{project_id}/entities",
            json={"kind": "character", "name": "阿雾"},
        )
        payload = {
            "entity_name": "阿雾",
            "data": {"location": "雾城"},
            "valid_from_node_id": chapter_id,
        }

    candidate_id, _ = _seed_candidate(
        client,
        project_id,
        kind=kind,
        payload=payload,
        chapter_id=chapter_id,
        source_version_id=source_version_id,
    )
    response = client.post(
        f"/api/v1/projects/{project_id}/memory-candidates/{candidate_id}/confirm",
        json={"revision": 1, "decision": "confirm"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["promotion_fingerprint"]
    promoted_id = response.json()["promoted_record_id"]
    with vault.database.session_scope() as session:
        promoted = session.get(model, promoted_id)
        assert promoted is not None
        if kind == "node_update":
            assert promoted.summary == "已完成开场"
            assert promoted.target_words == 3200
        if kind == "chapter_summary":
            assert promoted.origin == "import_confirmed"
            assert promoted.status == "valid"
        if kind in {"canon", "entity_state"}:
            assert promoted.source_version_id == source_version_id
        session.get(ChapterVersion, source_version_id).source = "manual"

    invalid_source_replay = client.post(
        f"/api/v1/projects/{project_id}/memory-candidates/{candidate_id}/confirm",
        json={"revision": 2},
    )
    assert invalid_source_replay.status_code == 409
    assert (
        invalid_source_replay.json()["detail"]["code"]
        == "CANDIDATE_SOURCE_VERSION_CHANGED"
    )

    with vault.database.session_scope() as session:
        session.get(ChapterVersion, source_version_id).source = "import"
        candidate = session.get(MemoryCandidate, candidate_id)
        candidate.promotion_fingerprint = None
    legacy_replay = client.post(
        f"/api/v1/projects/{project_id}/memory-candidates/{candidate_id}/confirm",
        json={"revision": 2},
    )
    assert legacy_replay.status_code == 200, legacy_replay.text
    assert legacy_replay.json()["promotion_fingerprint"]

    with vault.database.session_scope() as session:
        candidate = session.get(MemoryCandidate, candidate_id)
        promoted = session.get(model, candidate.promoted_record_id)
        duplicate = _duplicate_promoted_record(session, model, promoted)
        duplicate_id = duplicate.id
        candidate.promotion_fingerprint = None
    ambiguous_original = client.post(
        f"/api/v1/projects/{project_id}/memory-candidates/{candidate_id}/confirm",
        json={"revision": 2},
    )
    assert ambiguous_original.status_code == 409
    assert (
        ambiguous_original.json()["detail"]["code"]
        == "CANDIDATE_PROMOTION_MISSING"
    )
    with vault.database.session_scope() as session:
        candidate = session.get(MemoryCandidate, candidate_id)
        assert candidate.promotion_fingerprint is None
        candidate.promoted_record_id = duplicate_id
    ambiguous_swapped = client.post(
        f"/api/v1/projects/{project_id}/memory-candidates/{candidate_id}/confirm",
        json={"revision": 2},
    )
    assert ambiguous_swapped.status_code == 409
    assert ambiguous_swapped.json()["detail"]["code"] == "CANDIDATE_PROMOTION_MISSING"

    with vault.database.session_scope() as session:
        candidate = session.get(MemoryCandidate, candidate_id)
        candidate.promoted_record_id = "missing-promoted-record"
    broken_replay = client.post(
        f"/api/v1/projects/{project_id}/memory-candidates/{candidate_id}/confirm",
        json={"revision": 2},
    )
    assert broken_replay.status_code == 409
    assert broken_replay.json()["detail"]["code"] == "CANDIDATE_PROMOTION_MISSING"


@pytest.mark.parametrize("kind", ["canon", "entity_state"])
def test_import_candidate_rejects_payload_source_version_injection(
    client, project, kind
):
    project_id = project["id"]
    chapter = client.post(
        f"/api/v1/projects/{project_id}/nodes",
        json={"kind": "chapter", "title": "注入来源", "order_index": 1},
    ).json()
    vault = client.app.state.vault_registry.require(project_id, job_only=True)
    with vault.database.session_scope() as session:
        from novel_harness.services.versions import create_version

        version = create_version(
            session, chapter["id"], content="注入测试", source="import"
        )
        injected_version_id = version.id
    if kind == "canon":
        payload = {
            "predicate": "注入",
            "value": True,
            "source_version_id": injected_version_id,
        }
    else:
        client.post(
            f"/api/v1/projects/{project_id}/entities",
            json={"kind": "character", "name": "阿雾"},
        )
        payload = {
            "entity_name": "阿雾",
            "data": {"knows": True},
            "valid_from_node_id": chapter["id"],
            "source_version_id": injected_version_id,
        }
    candidate_id, _ = _seed_candidate(client, project_id, kind=kind, payload=payload)
    response = client.post(
        f"/api/v1/projects/{project_id}/memory-candidates/{candidate_id}/confirm",
        json={"revision": 1},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INVALID_MEMORY_CANDIDATE"


def test_import_candidate_rejects_nonimport_source_version_on_first_confirm(
    client, project
):
    project_id = project["id"]
    chapter = client.post(
        f"/api/v1/projects/{project_id}/nodes",
        json={"kind": "chapter", "title": "手工来源章", "order_index": 1},
    ).json()
    vault = client.app.state.vault_registry.require(project_id, job_only=True)
    with vault.database.session_scope() as session:
        from novel_harness.services.versions import create_version

        version = create_version(
            session, chapter["id"], content="手工正文", source="manual"
        )
        version_id = version.id
    candidate_id, _ = _seed_candidate(
        client,
        project_id,
        kind="entity",
        payload={
            "kind": "character",
            "name": "阿雾",
            "summary": "",
            "profile": {},
            "state": {},
        },
        chapter_id=chapter["id"],
        source_version_id=version_id,
    )
    response = client.post(
        f"/api/v1/projects/{project_id}/memory-candidates/{candidate_id}/confirm",
        json={"revision": 1},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "CANDIDATE_SOURCE_VERSION_CHANGED"


def test_import_source_document_without_chapter_accepts_candidate_version_binding(
    client, project
):
    project_id = project["id"]
    chapter = client.post(
        f"/api/v1/projects/{project_id}/nodes",
        json={"kind": "chapter", "title": "拆分章节", "order_index": 1},
    ).json()
    vault = client.app.state.vault_registry.require(project_id, job_only=True)
    with vault.database.session_scope() as session:
        from novel_harness.services.versions import create_version

        version = create_version(
            session, chapter["id"], content="拆分正文", source="import"
        )
        version_id = version.id
    candidate_id, source_id = _seed_candidate(
        client,
        project_id,
        chapter_id=chapter["id"],
        source_version_id=version_id,
    )
    with vault.database.session_scope() as session:
        session.get(SourceDocument, source_id).chapter_id = None
    response = client.post(
        f"/api/v1/projects/{project_id}/memory-candidates/{candidate_id}/confirm",
        json={"revision": 1},
    )
    assert response.status_code == 200, response.text
    with vault.database.session_scope() as session:
        candidate = session.get(MemoryCandidate, candidate_id)
        promoted = session.get(CanonFact, candidate.promoted_record_id)
        assert promoted.source_version_id == version_id


def test_confirmed_import_replay_keeps_historical_version_after_new_current(
    client, project
):
    project_id = project["id"]
    chapter = client.post(
        f"/api/v1/projects/{project_id}/nodes",
        json={"kind": "chapter", "title": "历史版本章", "order_index": 1},
    ).json()
    vault = client.app.state.vault_registry.require(project_id, job_only=True)
    with vault.database.session_scope() as session:
        from novel_harness.services.versions import create_version

        version = create_version(
            session, chapter["id"], content="导入版本", source="import"
        )
        version_id = version.id
    candidate_id, _ = _seed_candidate(
        client,
        project_id,
        chapter_id=chapter["id"],
        source_version_id=version_id,
    )
    url = f"/api/v1/projects/{project_id}/memory-candidates/{candidate_id}/confirm"
    confirmed = client.post(url, json={"revision": 1})
    assert confirmed.status_code == 200, confirmed.text
    with vault.database.session_scope() as session:
        from novel_harness.services.versions import create_version

        create_version(session, chapter["id"], content="后续编辑", source="manual")
    replay = client.post(url, json={"revision": 2})
    assert replay.status_code == 200, replay.text
    assert replay.json()["promoted_record_id"] == confirmed.json()["promoted_record_id"]


def test_chapter_summary_can_confirm_an_immutable_noncurrent_import_version(
    client, project
):
    project_id = project["id"]
    chapter = client.post(
        f"/api/v1/projects/{project_id}/nodes",
        json={"kind": "chapter", "title": "历史摘要章", "order_index": 1},
    ).json()
    vault = client.app.state.vault_registry.require(project_id, job_only=True)
    with vault.database.session_scope() as session:
        from novel_harness.services.versions import create_version

        imported = create_version(
            session, chapter["id"], content="原始导入正文", source="import"
        )
        imported_id = imported.id
        create_version(session, chapter["id"], content="后续编辑", source="manual")
    candidate_id, _ = _seed_candidate(
        client,
        project_id,
        kind="chapter_summary",
        chapter_id=chapter["id"],
        source_version_id=imported_id,
        payload={
            "chapter_id": chapter["id"],
            "version_id": imported_id,
            "content_hash": hashlib.sha256("原始导入正文".encode()).hexdigest(),
            "title": "历史摘要章",
            "recap": "原始版本摘要",
            "details": {},
        },
    )
    response = client.post(
        f"/api/v1/projects/{project_id}/memory-candidates/{candidate_id}/confirm",
        json={"revision": 1},
    )
    assert response.status_code == 200, response.text
    with vault.database.session_scope() as session:
        summary = session.get(ChapterSummary, response.json()["promoted_record_id"])
        assert summary.version_id == imported_id
        assert summary.content_hash == hashlib.sha256(
            "原始导入正文".encode()
        ).hexdigest()


@pytest.mark.parametrize("kind", ["canon", "entity_state"])
def test_null_candidate_source_promotes_null_source_binding(client, project, kind):
    project_id = project["id"]
    if kind == "canon":
        payload = {"predicate": "无版本来源", "value": True}
    else:
        client.post(
            f"/api/v1/projects/{project_id}/entities",
            json={"kind": "character", "name": "阿雾"},
        )
        chapter = client.post(
            f"/api/v1/projects/{project_id}/nodes",
            json={"kind": "chapter", "title": "无版本状态", "order_index": 1},
        ).json()
        payload = {
            "entity_name": "阿雾",
            "data": {"location": "雾城"},
            "valid_from_node_id": chapter["id"],
        }
    candidate_id, _ = _seed_candidate(client, project_id, kind=kind, payload=payload)
    response = client.post(
        f"/api/v1/projects/{project_id}/memory-candidates/{candidate_id}/confirm",
        json={"revision": 1},
    )
    assert response.status_code == 200, response.text
    vault = client.app.state.vault_registry.require(project_id, job_only=True)
    model = CanonFact if kind == "canon" else EntityState
    with vault.database.session_scope() as session:
        promoted = session.get(model, response.json()["promoted_record_id"])
        assert promoted.source_version_id is None


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("missing", "CANDIDATE_SOURCE_VERSION_CHANGED"),
        ("cross_project", "CANDIDATE_SOURCE_VERSION_CHANGED"),
        ("cross_chapter", "CANDIDATE_SOURCE_VERSION_CHANGED"),
        ("source_deleted", "CANDIDATE_EVIDENCE_CHANGED"),
    ],
)
def test_confirmed_import_replay_rejects_broken_source_provenance(
    client, project, mutation, expected_code
):
    project_id = project["id"]
    chapter = client.post(
        f"/api/v1/projects/{project_id}/nodes",
        json={"kind": "chapter", "title": "来源完整性", "order_index": 1},
    ).json()
    vault = client.app.state.vault_registry.require(project_id, job_only=True)
    with vault.database.session_scope() as session:
        from novel_harness.services.versions import create_version

        version = create_version(
            session, chapter["id"], content="完整来源", source="import"
        )
        version_id = version.id
    candidate_id, source_id = _seed_candidate(
        client,
        project_id,
        chapter_id=chapter["id"],
        source_version_id=version_id,
    )
    url = f"/api/v1/projects/{project_id}/memory-candidates/{candidate_id}/confirm"
    confirmed = client.post(url, json={"revision": 1})
    assert confirmed.status_code == 200, confirmed.text
    if mutation == "missing":
        with closing(sqlite3.connect(vault.database.path)) as connection, connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute(
                "DELETE FROM chapter_versions WHERE id = ?", (version_id,)
            )
    elif mutation == "source_deleted":
        from novel_harness.db.base import utc_now

        with vault.database.session_scope() as session:
            session.get(SourceDocument, source_id).deleted_at = utc_now()
    else:
        with vault.database.session_scope() as session:
            from novel_harness.services.versions import create_version

            if mutation == "cross_project":
                other_project = Project(title="其他项目")
                session.add(other_project)
                session.flush()
                other_chapter = StoryNode(
                    project_id=other_project.id,
                    kind="chapter",
                    title="他项目章节",
                )
                session.add(other_chapter)
                session.flush()
                other_version = ChapterVersion(
                    project_id=other_project.id,
                    chapter_id=other_chapter.id,
                    content="他项目正文",
                    source="import",
                )
                session.add(other_version)
                session.flush()
            else:
                other_chapter = StoryNode(
                    project_id=project_id,
                    kind="chapter",
                    title="其他章节",
                    order_index=2,
                )
                session.add(other_chapter)
                session.flush()
                other_version = create_version(
                    session,
                    other_chapter.id,
                    content="其他正文",
                    source="import",
                )
            session.get(MemoryCandidate, candidate_id).source_version_id = (
                other_version.id
            )
    replay = client.post(url, json={"revision": 2})
    assert replay.status_code == 409
    assert replay.json()["detail"]["code"] == expected_code


@pytest.mark.parametrize("mutation", ["physical", "soft", "type", "project"])
def test_confirmed_replay_rejects_tampered_promotion(client, project, mutation):
    candidate_id, _ = _seed_candidate(
        client,
        project["id"],
        kind="entity",
        payload={
            "kind": "character",
            "name": "阿雾",
            "summary": "",
            "profile": {},
            "state": {},
        },
    )
    url = f"/api/v1/projects/{project['id']}/memory-candidates/{candidate_id}/confirm"
    confirmed = client.post(url, json={"revision": 1})
    assert confirmed.status_code == 200
    vault = client.app.state.vault_registry.require(project["id"], job_only=True)
    with vault.database.session_scope() as session:
        candidate = session.get(MemoryCandidate, candidate_id)
        promoted = session.get(Entity, candidate.promoted_record_id)
        if mutation == "physical":
            session.delete(promoted)
        elif mutation == "soft":
            from novel_harness.db.base import utc_now

            promoted.deleted_at = utc_now()
        elif mutation == "type":
            candidate.promoted_type = "timeline"
        else:
            session.add(Project(id="foreign-project", title="他项目"))
            session.flush()
            promoted.project_id = "foreign-project"
    replay = client.post(url, json={"revision": 2})
    assert replay.status_code == 409
    assert replay.json()["detail"]["code"] == "CANDIDATE_PROMOTION_MISSING"


def test_import_replay_rejects_same_type_pointer_swap(client, project):
    candidate_id, _ = _seed_candidate(client, project["id"])
    url = f"/api/v1/projects/{project['id']}/memory-candidates/{candidate_id}/confirm"
    confirmed = client.post(url, json={"revision": 1})
    assert confirmed.status_code == 200, confirmed.text
    replacement = client.post(
        f"/api/v1/projects/{project['id']}/canon",
        json={"predicate": "替换目标", "value": True},
    )
    assert replacement.status_code == 201
    vault = client.app.state.vault_registry.require(project["id"], job_only=True)
    with vault.database.session_scope() as session:
        candidate = session.get(MemoryCandidate, candidate_id)
        candidate.promoted_record_id = replacement.json()["id"]
        candidate.promotion_fingerprint = None
    replay = client.post(url, json={"revision": 2})
    assert replay.status_code == 409
    assert replay.json()["detail"]["code"] == "CANDIDATE_PROMOTION_MISSING"


def test_bulk_confirm_replays_committed_exact_entries_without_new_promotions(
    client, project
):
    first_id, _ = _seed_candidate(client, project["id"])
    second_id, _ = _seed_candidate(
        client,
        project["id"],
        payload={"predicate": "响应丢失后的第二条", "value": True},
    )
    url = f"/api/v1/projects/{project['id']}/memory-candidates/bulk-confirm"
    payload = {
        "entries": [
            {"candidate_id": first_id, "revision": 1},
            {"candidate_id": second_id, "revision": 1},
        ]
    }
    first = client.post(url, json=payload)
    assert first.status_code == 200, first.text
    from novel_harness.services.memory_candidates import _promotion_fingerprint

    vault = client.app.state.vault_registry.require(project["id"], job_only=True)
    with vault.database.session_scope() as session:
        for candidate_id in (first_id, second_id):
            candidate = session.get(MemoryCandidate, candidate_id)
            expected_fingerprint = _promotion_fingerprint(
                candidate,
                origin="import",
                promoted_type=candidate.kind,
                promoted_record_id=candidate.promoted_record_id,
            )
            assert candidate.promotion_fingerprint == expected_fingerprint
    vault.database.dispose()
    replay = client.post(url, json=payload)
    assert replay.status_code == 200, replay.text
    assert [row["promoted_record_id"] for row in replay.json()["items"]] == [
        row["promoted_record_id"] for row in first.json()["items"]
    ]
    with vault.database.session_scope() as session:
        assert session.scalar(select(func.count()).select_from(CanonFact)) == 2


def test_bulk_confirm_accepts_valid_confirmed_replay_mixed_with_pending(
    client, project
):
    confirmed_id, _ = _seed_candidate(client, project["id"])
    pending_id, _ = _seed_candidate(
        client,
        project["id"],
        payload={"predicate": "混合重放待确认", "value": True},
    )
    single_url = (
        f"/api/v1/projects/{project['id']}/memory-candidates/"
        f"{confirmed_id}/confirm"
    )
    confirmed = client.post(single_url, json={"revision": 1})
    assert confirmed.status_code == 200, confirmed.text

    bulk = client.post(
        f"/api/v1/projects/{project['id']}/memory-candidates/bulk-confirm",
        json={
            "entries": [
                {"candidate_id": confirmed_id, "revision": 1},
                {"candidate_id": pending_id, "revision": 1},
            ]
        },
    )
    assert bulk.status_code == 200, bulk.text
    assert bulk.json()["items"][0]["promoted_record_id"] == confirmed.json()[
        "promoted_record_id"
    ]
    vault = client.app.state.vault_registry.require(project["id"], job_only=True)
    with vault.database.session_scope() as session:
        assert session.scalar(select(func.count()).select_from(CanonFact)) == 2
        assert session.get(MemoryCandidate, pending_id).status == "confirmed"


@pytest.mark.parametrize("tamper", ["fingerprint", "older_revision"])
def test_bulk_confirm_invalid_confirmed_replay_rolls_back_pending(
    client, project, tamper
):
    confirmed_id, _ = _seed_candidate(client, project["id"])
    pending_id, _ = _seed_candidate(
        client,
        project["id"],
        payload={"predicate": f"回滚-{tamper}", "value": True},
    )
    single_url = (
        f"/api/v1/projects/{project['id']}/memory-candidates/"
        f"{confirmed_id}/confirm"
    )
    confirmed = client.post(single_url, json={"revision": 1})
    assert confirmed.status_code == 200, confirmed.text
    requested_revision = 1
    vault = client.app.state.vault_registry.require(project["id"], job_only=True)
    with vault.database.session_scope() as session:
        candidate = session.get(MemoryCandidate, confirmed_id)
        if tamper == "fingerprint":
            candidate.promotion_fingerprint = "f" * 64
        else:
            candidate.revision = 4

    response = client.post(
        f"/api/v1/projects/{project['id']}/memory-candidates/bulk-confirm",
        json={
            "entries": [
                {"candidate_id": pending_id, "revision": 1},
                {
                    "candidate_id": confirmed_id,
                    "revision": requested_revision,
                },
            ]
        },
    )
    assert response.status_code == 409
    expected = (
        "CANDIDATE_PROMOTION_MISSING"
        if tamper == "fingerprint"
        else "revision_conflict"
    )
    assert response.json()["detail"]["code"] == expected
    with vault.database.session_scope() as session:
        assert session.scalar(select(func.count()).select_from(CanonFact)) == 1
        assert session.get(MemoryCandidate, pending_id).status == "pending"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("content", "被篡改的正文"),
        ("content_hash", "0" * 64),
        ("category", "other"),
        ("relative_path", "设定/改名.md"),
        ("content_revision", 2),
    ],
)
def test_candidate_reloads_canonical_source_identity_before_confirm(
    client, project, field, value
):
    candidate_id, source_id = _seed_candidate(client, project["id"])
    vault = client.app.state.vault_registry.require(project["id"], job_only=True)
    with vault.database.session_scope() as session:
        session.execute(
            text(f'UPDATE source_documents SET "{field}"=:value WHERE id=:id'),
            {"value": value, "id": source_id},
        )

    response = client.post(
        f"/api/v1/projects/{project['id']}/memory-candidates/{candidate_id}/confirm",
        json={"revision": 1},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "CANDIDATE_EVIDENCE_CHANGED"


@pytest.mark.parametrize(
    "evidence",
    [
        [{"quote": "雾", "start": -1, "end": 1}],
        [{"quote": "雾", "start": 0, "end": 0}],
        [{"quote": "雾", "start": 0, "end": 10_000}],
        [{"quote": "错", "start": 0, "end": 1}],
    ],
)
def test_candidate_evidence_bounds_and_quote_are_stable_conflicts(
    client, project, evidence
):
    candidate_id, _ = _seed_candidate(client, project["id"], evidence=evidence)
    response = client.post(
        f"/api/v1/projects/{project['id']}/memory-candidates/{candidate_id}/confirm",
        json={"revision": 1},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "CANDIDATE_EVIDENCE_CHANGED"


def test_confirm_and_reject_replays_are_idempotent_and_revision_safe(client, project):
    confirmed_id, _ = _seed_candidate(client, project["id"])
    base = f"/api/v1/projects/{project['id']}/memory-candidates"
    first = client.post(f"{base}/{confirmed_id}/confirm", json={"revision": 1})
    client.app.state.vault_registry.require(project["id"]).database.dispose()
    replay = client.post(f"{base}/{confirmed_id}/confirm", json={"revision": 1})
    current_replay = client.post(f"{base}/{confirmed_id}/confirm", json={"revision": 2})
    assert first.status_code == replay.status_code == current_replay.status_code == 200
    assert {
        first.json()["promoted_record_id"],
        replay.json()["promoted_record_id"],
        current_replay.json()["promoted_record_id"],
    } == {first.json()["promoted_record_id"]}

    rejected_id, _ = _seed_candidate(
        client,
        project["id"],
        payload={"predicate": "另一规则", "value": "值"},
    )
    rejected = client.post(f"{base}/{rejected_id}/reject", json={"revision": 1})
    rejected_replay = client.post(
        f"{base}/{rejected_id}/reject", json={"revision": 1}
    )
    forbidden = client.post(f"{base}/{rejected_id}/confirm", json={"revision": 2})
    assert rejected.status_code == rejected_replay.status_code == 200
    assert rejected.json()["revision"] == rejected_replay.json()["revision"] == 2
    assert forbidden.status_code == 409
    assert forbidden.json()["detail"]["code"] == "MEMORY_CANDIDATE_REJECTED"


def test_stale_patch_returns_public_current_and_edit_clears_conflict(client, project):
    candidate_id, _ = _seed_candidate(client, project["id"], kind="entity", payload={
        "kind": "character", "name": "阿雾", "summary": "", "profile": {}, "state": {}
    })
    entity = client.post(
        f"/api/v1/projects/{project['id']}/entities",
        json={"kind": "character", "name": "阿雾"},
    )
    assert entity.status_code == 201
    base = f"/api/v1/projects/{project['id']}/memory-candidates/{candidate_id}"
    conflict = client.post(base + "/confirm", json={"revision": 1})
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["current"]["status"] == "conflict"
    edited = client.patch(
        base,
        json={"revision": 2, "payload": {
            "kind": "character", "name": "阿雨", "summary": "", "profile": {}, "state": {}
        }},
    )
    assert edited.status_code == 200
    assert edited.json()["status"] == "pending"
    assert edited.json()["conflict"] == {}
    stale = client.patch(base, json={"revision": 2, "payload": edited.json()["payload"]})
    assert stale.status_code == 409
    assert stale.json()["detail"]["current"]["revision"] == 3


@pytest.mark.parametrize(
    "body",
    [
        {"revision": 1},
        {"revision": 1, "kind": None},
        {"revision": 1, "payload": None},
        {"revision": 1, "evidence": None},
    ],
)
def test_import_patch_requires_a_nonnull_real_edit(client, project, body):
    candidate_id, _ = _seed_candidate(client, project["id"])
    response = client.patch(
        f"/api/v1/projects/{project['id']}/memory-candidates/{candidate_id}",
        json=body,
    )
    assert response.status_code == 422
    vault = client.app.state.vault_registry.require(project["id"], job_only=True)
    with vault.database.session_scope() as session:
        candidate = session.get(MemoryCandidate, candidate_id)
        assert candidate.revision == 1
        assert candidate.status == "pending"


def test_import_patch_rejects_canonical_noop_without_clearing_conflict(client, project):
    payload = {
        "kind": "character",
        "name": "阿雾",
        "summary": "",
        "profile": {},
        "state": {},
    }
    candidate_id, _ = _seed_candidate(
        client, project["id"], kind="entity", payload=payload
    )
    vault = client.app.state.vault_registry.require(project["id"], job_only=True)
    with vault.database.session_scope() as session:
        candidate = session.get(MemoryCandidate, candidate_id)
        candidate.status = "conflict"
        candidate.conflict = {"code": "CANDIDATE_RECORD_CONFLICT"}
    response = client.patch(
        f"/api/v1/projects/{project['id']}/memory-candidates/{candidate_id}",
        json={"revision": 1, "payload": payload},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "NO_CANDIDATE_CHANGES"
    with vault.database.session_scope() as session:
        candidate = session.get(MemoryCandidate, candidate_id)
        assert candidate.revision == 1
        assert candidate.status == "conflict"
        assert candidate.conflict == {"code": "CANDIDATE_RECORD_CONFLICT"}


def test_create_separate_is_individual_and_cannot_bypass_ambiguous_reference(
    client, project
):
    payload = {"kind": "character", "name": "阿雾", "summary": "", "profile": {}, "state": {}}
    client.post(
        f"/api/v1/projects/{project['id']}/entities",
        json={"kind": "character", "name": "阿雾"},
    )
    candidate_id, _ = _seed_candidate(client, project["id"], kind="entity", payload=payload)
    base = f"/api/v1/projects/{project['id']}/memory-candidates/{candidate_id}"
    assert client.post(base + "/confirm", json={"revision": 1}).status_code == 409
    separate = client.post(
        base + "/confirm",
        json={"revision": 2, "resolution": "create_separate"},
    )
    assert separate.status_code == 200
    vault = client.app.state.vault_registry.require(project["id"], job_only=True)
    with vault.database.session_scope() as session:
        assert session.scalar(select(func.count()).select_from(Entity)) == 2

    relation_id, _ = _seed_candidate(
        client,
        project["id"],
        kind="relation",
        payload={
            "source_entity_name": "不存在",
            "target_entity_name": "阿雾",
            "relation_type": "盟友",
            "description": "",
        },
    )
    ambiguous = client.post(
        f"/api/v1/projects/{project['id']}/memory-candidates/{relation_id}/confirm",
        json={"revision": 1, "resolution": "create_separate"},
    )
    assert ambiguous.status_code == 409
    assert ambiguous.json()["detail"]["code"] == "CANDIDATE_REFERENCE_AMBIGUOUS"


@pytest.mark.parametrize(
    ("existing_range", "candidate_range", "conflicts"),
    [
        ((0, 2), (1, 3), True),
        ((1, 2), (0, 3), True),
        ((1, None), (3, None), True),
        ((0, 1), (2, 3), False),
    ],
)
def test_entity_state_candidate_uses_closed_story_intervals(
    client, project, existing_range, candidate_range, conflicts
):
    project_id = project["id"]
    entity = client.post(
        f"/api/v1/projects/{project_id}/entities",
        json={"kind": "character", "name": "阿雾"},
    ).json()
    nodes = [
        client.post(
            f"/api/v1/projects/{project_id}/nodes",
            json={"kind": "chapter", "title": f"第{index + 1}章", "order_index": index},
        ).json()
        for index in range(4)
    ]
    existing_start, existing_end = existing_range
    existing = client.post(
        f"/api/v1/projects/{project_id}/entities/{entity['id']}/states",
        json={
            "data": {"mood": "calm", "place": "tower"},
            "valid_from_node_id": nodes[existing_start]["id"],
            "valid_to_node_id": (
                nodes[existing_end]["id"] if existing_end is not None else None
            ),
            "status": "confirmed",
        },
    )
    assert existing.status_code == 201, existing.text
    candidate_start, candidate_end = candidate_range
    candidate_id, _ = _seed_candidate(
        client,
        project_id,
        kind="entity_state",
        payload={
            "entity_name": "阿雾",
            "data": {"mood": "angry", "place": "tower", "new": 1},
            "valid_from_node_id": nodes[candidate_start]["id"],
            "valid_to_node_id": (
                nodes[candidate_end]["id"] if candidate_end is not None else None
            ),
        },
    )
    response = client.post(
        f"/api/v1/projects/{project_id}/memory-candidates/{candidate_id}/confirm",
        json={"revision": 1},
    )
    if not conflicts:
        assert response.status_code == 200, response.text
        return
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "CANDIDATE_RECORD_CONFLICT"
    current = response.json()["detail"]["current"]
    assert current["status"] == "conflict"
    assert current["revision"] == 2
    details = current["conflict"]
    assert details["code"] == "CANDIDATE_ENTITY_STATE_CONFLICT"
    assert details["existing_state_ids"] == [existing.json()["id"]]
    assert details["ranges"][0]["existing_start"] == nodes[existing_start]["id"]
    assert {item["key"] for item in details["differences"]} == {"mood", "new"}


def test_entity_state_conflict_rolls_back_entire_bulk(client, project):
    project_id = project["id"]
    entity = client.post(
        f"/api/v1/projects/{project_id}/entities",
        json={"kind": "character", "name": "阿雾"},
    ).json()
    node = client.post(
        f"/api/v1/projects/{project_id}/nodes",
        json={"kind": "chapter", "title": "第一章", "order_index": 1},
    ).json()
    existing = client.post(
        f"/api/v1/projects/{project_id}/entities/{entity['id']}/states",
        json={
            "data": {"alive": True},
            "valid_from_node_id": node["id"],
            "status": "confirmed",
        },
    )
    assert existing.status_code == 201
    canon_id, _ = _seed_candidate(client, project_id)
    state_id, _ = _seed_candidate(
        client,
        project_id,
        kind="entity_state",
        payload={
            "entity_name": "阿雾",
            "data": {"alive": False},
            "valid_from_node_id": node["id"],
        },
    )
    response = client.post(
        f"/api/v1/projects/{project_id}/memory-candidates/bulk-confirm",
        json={"entries": [
            {"candidate_id": canon_id, "revision": 1},
            {"candidate_id": state_id, "revision": 1},
        ]},
    )
    assert response.status_code == 409
    vault = client.app.state.vault_registry.require(project_id, job_only=True)
    with vault.database.session_scope() as session:
        assert session.scalar(select(func.count()).select_from(CanonFact)) == 0
        assert session.get(MemoryCandidate, canon_id).status == "pending"
        assert session.get(MemoryCandidate, state_id).status == "pending"
        assert session.scalar(select(func.count()).select_from(EntityState)) == 1


def test_entity_state_candidate_rejects_unknown_story_range(client, project):
    client.post(
        f"/api/v1/projects/{project['id']}/entities",
        json={"kind": "character", "name": "阿雾"},
    )
    candidate_id, _ = _seed_candidate(
        client,
        project["id"],
        kind="entity_state",
        payload={
            "entity_name": "阿雾",
            "data": {"alive": True},
            "valid_from_node_id": "missing-node",
        },
    )
    response = client.post(
        f"/api/v1/projects/{project['id']}/memory-candidates/{candidate_id}/confirm",
        json={"revision": 1},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "REFERENCE_NOT_FOUND"


def test_node_update_rejects_nonimported_node_and_forbidden_fields(client, project):
    node = client.post(
        f"/api/v1/projects/{project['id']}/nodes",
        json={"kind": "chapter", "title": "手动章", "order_index": 1},
    ).json()
    candidate_id, _ = _seed_candidate(
        client,
        project["id"],
        kind="node_update",
        payload={"node_id": node["id"], "expected_revision": 1, "summary": "更新"},
    )
    response = client.post(
        f"/api/v1/projects/{project['id']}/memory-candidates/{candidate_id}/confirm",
        json={"revision": 1},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "CANDIDATE_NODE_NOT_IMPORTED"

    forbidden_id, _ = _seed_candidate(
        client,
        project["id"],
        kind="node_update",
        payload={
            "node_id": node["id"],
            "expected_revision": 1,
            "title": "越权改名",
        },
    )
    forbidden = client.post(
        f"/api/v1/projects/{project['id']}/memory-candidates/{forbidden_id}/confirm",
        json={"revision": 1},
    )
    assert forbidden.status_code == 422
    assert forbidden.json()["detail"]["code"] == "INVALID_MEMORY_CANDIDATE"


def _seed_node_update_candidate(client, project_id: str):
    node = client.post(
        f"/api/v1/projects/{project_id}/nodes",
        json={
            "kind": "chapter",
            "title": "索引更新章",
            "summary": "OLD-NODE-MARKER",
            "order_index": 1,
        },
    ).json()
    vault = client.app.state.vault_registry.require(project_id, job_only=True)
    with vault.database.session_scope() as session:
        from novel_harness.services.versions import create_version

        version = create_version(
            session, node["id"], content="已导入正文", source="import"
        )
        version_id = version.id
    candidate_id, _ = _seed_candidate(
        client,
        project_id,
        kind="node_update",
        chapter_id=node["id"],
        source_version_id=version_id,
        payload={
            "node_id": node["id"],
            "expected_revision": 1,
            "summary": "NEW-NODE-MARKER",
            "target_words": 4321,
        },
    )
    return node["id"], candidate_id


def test_node_update_confirmation_resyncs_search_projection(client, project):
    project_id = project["id"]
    node_id, candidate_id = _seed_node_update_candidate(client, project_id)
    response = client.post(
        f"/api/v1/projects/{project_id}/memory-candidates/{candidate_id}/confirm",
        json={"revision": 1},
    )
    assert response.status_code == 200, response.text
    vault = client.app.state.vault_registry.require(project_id, job_only=True)
    with vault.database.session_scope() as session:
        document = session.execute(
            text("SELECT body,data FROM search_documents WHERE key=:key"),
            {"key": f"node:{node_id}"},
        ).mappings().one()
        chunks = session.scalars(
            text("SELECT body FROM search_chunks WHERE document_key=:key"),
            {"key": f"node:{node_id}"},
        ).all()
        assert "NEW-NODE-MARKER" in document["body"]
        assert "OLD-NODE-MARKER" not in document["body"]
        assert json.loads(document["data"])["record"]["target_words"] == 4321
        assert any("NEW-NODE-MARKER" in body for body in chunks)


def test_node_update_projection_failure_rolls_back_core_update(
    client, project, monkeypatch
):
    project_id = project["id"]
    node_id, candidate_id = _seed_node_update_candidate(client, project_id)
    from novel_harness.services import search_index

    def fail_sync(_session, _record, **_kwargs):
        raise RuntimeError("injected projection failure")

    monkeypatch.setattr(search_index, "sync_record", fail_sync)
    failed = client.post(
        f"/api/v1/projects/{project_id}/memory-candidates/{candidate_id}/confirm",
        json={"revision": 1},
    )
    assert failed.status_code == 500
    vault = client.app.state.vault_registry.require(project_id, job_only=True)
    with vault.database.session_scope() as session:
        node = session.get(StoryNode, node_id)
        candidate = session.get(MemoryCandidate, candidate_id)
        body = session.scalar(
            text("SELECT body FROM search_documents WHERE key=:key"),
            {"key": f"node:{node_id}"},
        )
        assert node.summary == "OLD-NODE-MARKER"
        assert node.target_words == 0
        assert candidate.status == "pending"
        assert "OLD-NODE-MARKER" in body
        assert "NEW-NODE-MARKER" not in body


def test_style_rule_candidate_rejects_unowned_feedback_references(client, project):
    candidate_id, _ = _seed_candidate(
        client,
        project["id"],
        kind="style_rule",
        payload={
            "instruction": "保持克制",
            "source_feedback_ids": ["foreign-feedback"],
        },
    )
    response = client.post(
        f"/api/v1/projects/{project['id']}/memory-candidates/{candidate_id}/confirm",
        json={"revision": 1},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INVALID_MEMORY_CANDIDATE"


def test_candidate_dedupe_is_recomputed_from_canonical_sorted_json(client, project):
    candidate_id, source_id = _seed_candidate(client, project["id"])
    vault = client.app.state.vault_registry.require(project["id"], job_only=True)
    with vault.database.session_scope() as session:
        candidate = session.get(MemoryCandidate, candidate_id)
        candidate.dedupe_key = "f" * 64
    response = client.post(
        f"/api/v1/projects/{project['id']}/memory-candidates/{candidate_id}/confirm",
        json={"revision": 1},
    )
    assert response.status_code == 200
    from novel_harness.services.memory_candidates import import_candidate_dedupe_key

    expected = import_candidate_dedupe_key(
        source_document_id=source_id,
        source_hash=response.json()["source_hash"],
        kind=response.json()["kind"],
        payload=response.json()["payload"],
        evidence=response.json()["evidence"],
    )
    assert response.json()["dedupe_key"] == expected


def test_candidate_actions_do_not_reveal_another_project(client, project):
    candidate_id, _ = _seed_candidate(client, project["id"])
    other = client.post("/api/v1/projects", json={"title": "隔离项目"}).json()
    base = f"/api/v1/projects/{other['id']}/memory-candidates/{candidate_id}"
    for response in (
        client.patch(base, json={"revision": 1, "payload": {}}),
        client.post(base + "/confirm", json={"revision": 1}),
        client.post(base + "/reject", json={"revision": 1}),
    ):
        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "MEMORY_CANDIDATE_NOT_FOUND"


def test_chapter_summary_rejects_mutated_immutable_version(client, project):
    node = client.post(
        f"/api/v1/projects/{project['id']}/nodes",
        json={"kind": "chapter", "title": "已导入章", "order_index": 1},
    ).json()
    vault = client.app.state.vault_registry.require(project["id"], job_only=True)
    with vault.database.session_scope() as session:
        from novel_harness.services.versions import create_version

        version = create_version(
            session, node["id"], content="原始正文", source="import"
        )
        version_id = version.id
    payload = {
        "chapter_id": node["id"],
        "version_id": version_id,
        "content_hash": hashlib.sha256("原始正文".encode()).hexdigest(),
        "title": "已导入章",
        "recap": "摘要",
        "details": {},
    }
    candidate_id, _ = _seed_candidate(
        client,
        project["id"],
        kind="chapter_summary",
        payload=payload,
        chapter_id=node["id"],
        source_version_id=version_id,
    )
    with vault.database.session_scope() as session:
        session.execute(
            text("UPDATE chapter_versions SET content='tampered' WHERE id=:id"),
            {"id": version_id},
        )
    response = client.post(
        f"/api/v1/projects/{project['id']}/memory-candidates/{candidate_id}/confirm",
        json={"revision": 1},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "CANDIDATE_SOURCE_VERSION_CHANGED"


def test_chapter_summary_candidate_must_match_its_bound_chapter(client, project):
    chapters = [
        client.post(
            f"/api/v1/projects/{project['id']}/nodes",
            json={"kind": "chapter", "title": title, "order_index": index},
        ).json()
        for index, title in enumerate(("第一章", "第二章"), start=1)
    ]
    vault = client.app.state.vault_registry.require(project["id"], job_only=True)
    with vault.database.session_scope() as session:
        from novel_harness.services.versions import create_version

        version = create_version(
            session, chapters[0]["id"], content="第一章正文", source="import"
        )
        version_id = version.id
    candidate_id, _ = _seed_candidate(
        client,
        project["id"],
        kind="chapter_summary",
        chapter_id=chapters[1]["id"],
        source_version_id=version_id,
        payload={
            "chapter_id": chapters[0]["id"],
            "version_id": version_id,
            "content_hash": hashlib.sha256("第一章正文".encode()).hexdigest(),
            "title": "第一章",
            "recap": "摘要",
            "details": {},
        },
    )
    response = client.post(
        f"/api/v1/projects/{project['id']}/memory-candidates/{candidate_id}/confirm",
        json={"revision": 1},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "CANDIDATE_SOURCE_VERSION_CHANGED"


def test_bulk_confirm_is_all_or_nothing_for_stale_entry(client, project):
    first_id, _ = _seed_candidate(client, project["id"])
    second_id, _ = _seed_candidate(
        client,
        project["id"],
        payload={"predicate": "第二条", "value": "值"},
    )
    base = f"/api/v1/projects/{project['id']}/memory-candidates"
    edited = client.patch(
        f"{base}/{second_id}",
        json={"revision": 1, "payload": {"predicate": "已修改", "value": "值"}},
    )
    assert edited.status_code == 200
    response = client.post(
        f"{base}/bulk-confirm",
        json={"entries": [
            {"candidate_id": first_id, "revision": 1},
            {"candidate_id": second_id, "revision": 1},
        ]},
    )
    assert response.status_code == 409
    vault = client.app.state.vault_registry.require(project["id"], job_only=True)
    with vault.database.session_scope() as session:
        assert session.scalar(select(func.count()).select_from(CanonFact)) == 0
        assert session.get(MemoryCandidate, first_id).status == "pending"
        assert session.get(MemoryCandidate, second_id).status == "pending"


def test_bulk_confirm_rejects_duplicates_limit_and_conflict_candidate(client, project):
    candidate_id, _ = _seed_candidate(client, project["id"])
    base = f"/api/v1/projects/{project['id']}/memory-candidates/bulk-confirm"
    duplicate = client.post(
        base,
        json={"entries": [
            {"candidate_id": candidate_id, "revision": 1},
            {"candidate_id": candidate_id, "revision": 1},
        ]},
    )
    assert duplicate.status_code == 422
    assert duplicate.json()["detail"]["code"] == "DUPLICATE_MEMORY_CANDIDATE"
    too_many = client.post(
        base,
        json={"entries": [
            {"candidate_id": f"candidate-{index}", "revision": 1}
            for index in range(101)
        ]},
    )
    assert too_many.status_code == 422

    vault = client.app.state.vault_registry.require(project["id"], job_only=True)
    with vault.database.session_scope() as session:
        candidate = session.get(MemoryCandidate, candidate_id)
        candidate.status = "conflict"
        candidate.conflict = {"code": "CANDIDATE_RECORD_CONFLICT"}
    conflict = client.post(
        base,
        json={"entries": [{"candidate_id": candidate_id, "revision": 1}]},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "CANDIDATE_RECORD_CONFLICT"


def test_bulk_database_exception_rolls_back_all_promotions(
    client, project, monkeypatch
):
    first_id, _ = _seed_candidate(client, project["id"])
    second_id, _ = _seed_candidate(
        client,
        project["id"],
        payload={"predicate": "第二条", "value": "值"},
    )
    from novel_harness.services import memory_candidates as service

    original = service.add_canon
    calls = 0

    def fail_second(session, project_id, values):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected database failure")
        return original(session, project_id, values)

    monkeypatch.setattr(service, "add_canon", fail_second)
    response = client.post(
        f"/api/v1/projects/{project['id']}/memory-candidates/bulk-confirm",
        json={"entries": [
            {"candidate_id": first_id, "revision": 1},
            {"candidate_id": second_id, "revision": 1},
        ]},
    )
    assert response.status_code == 500
    vault = client.app.state.vault_registry.require(project["id"], job_only=True)
    with vault.database.session_scope() as session:
        assert session.scalar(select(func.count()).select_from(CanonFact)) == 0
        assert session.get(MemoryCandidate, first_id).status == "pending"
        assert session.get(MemoryCandidate, second_id).status == "pending"


def test_concurrent_confirm_creates_one_promoted_record(client, project):
    candidate_id, _ = _seed_candidate(client, project["id"])
    url = f"/api/v1/projects/{project['id']}/memory-candidates/{candidate_id}/confirm"
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: client.post(url, json={"revision": 1}), range(2)))
    assert [response.status_code for response in responses] == [200, 200]
    assert len({response.json()["promoted_record_id"] for response in responses}) == 1
    vault = client.app.state.vault_registry.require(project["id"], job_only=True)
    with vault.database.session_scope() as session:
        assert session.scalar(select(func.count()).select_from(CanonFact)) == 1


def test_bulk_confirm_promotes_all_candidates_in_one_success(client, project):
    first_id, _ = _seed_candidate(client, project["id"])
    second_id, _ = _seed_candidate(
        client,
        project["id"],
        payload={"predicate": "第二条", "value": "值"},
    )
    response = client.post(
        f"/api/v1/projects/{project['id']}/memory-candidates/bulk-confirm",
        json={"entries": [
            {"candidate_id": first_id, "revision": 1},
            {"candidate_id": second_id, "revision": 1},
        ]},
    )
    assert response.status_code == 200, response.text
    assert {item["status"] for item in response.json()["items"]} == {"confirmed"}
    vault = client.app.state.vault_registry.require(project["id"], job_only=True)
    with vault.database.session_scope() as session:
        assert session.scalar(select(func.count()).select_from(CanonFact)) == 2
