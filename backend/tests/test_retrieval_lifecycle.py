from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock, get_ident

import pytest
from fastapi import HTTPException
from sqlalchemy import bindparam, event, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from novel_harness.db.models import (
    CanonFact,
    ChapterDocument,
    ChapterSummary,
    ChapterVersion,
    Entity,
    EntityRelation,
    Idea,
    PlotThread,
    Project,
    StoryNode,
)
from novel_harness.db.session import Database


def _later_chapter(client, project_id, seeded_chapter):
    workspace = client.get(f"/api/v1/projects/{project_id}/workspace").json()
    seeded = next(node for node in workspace["nodes"] if node["id"] == seeded_chapter)
    return client.post(
        f"/api/v1/projects/{project_id}/nodes",
        json={
            "kind": "chapter",
            "parent_id": seeded["parent_id"],
            "title": "第二章",
            "order_index": 2,
        },
    ).json()


def _projection(client, project_id, key):
    database = client.app.state.vault_registry.require(project_id).database
    with database.session_scope() as session:
        return session.scalar(
            text("SELECT data FROM search_documents WHERE key=:key"), {"key": key}
        )


def _source_version(client, project_id, chapter_id, version_id="source-version"):
    database = client.app.state.vault_registry.require(project_id).database
    with database.session_scope() as session:
        session.add(
            ChapterVersion(
                id=version_id,
                project_id=project_id,
                chapter_id=chapter_id,
                content="不可变正文",
                summary="来源版本",
                word_count=6,
                source="manual",
            )
        )
    return version_id


def _entity_dependencies(client, project_id, relation_endpoint="target"):
    base = f"/api/v1/projects/{project_id}"
    subject = client.post(
        base + "/entities",
        json={
            "kind": "character",
            "name": "依赖主体",
            "summary": "ENTITY_DEPENDENCY_NEEDLE",
        },
    ).json()
    other = client.post(
        base + "/entities",
        json={"kind": "character", "name": "关系另一端"},
    ).json()
    canon = client.post(
        base + "/canon",
        json={
            "subject_entity_id": subject["id"],
            "predicate": "CANON_DEPENDENCY_NEEDLE",
            "value": "必须保留但可暂时失活",
        },
    ).json()
    relation = client.post(
        base + "/relations",
        json={
            "source_entity_id": (
                subject["id"] if relation_endpoint == "source" else other["id"]
            ),
            "target_entity_id": (
                subject["id"] if relation_endpoint == "target" else other["id"]
            ),
            "relation_type": "守护",
            "description": "RELATION_DEPENDENCY_NEEDLE",
        },
    ).json()
    pinned = client.patch(
        base + f"/library/canon/{canon['id']}",
        json={"revision": canon["revision"], "is_pinned": True},
    ).json()
    return subject, other, pinned, relation


def _foreign_source_versions(client, project_id):
    database = client.app.state.vault_registry.require(project_id).database
    with database.session_scope() as session:
        session.add(Project(id="foreign-project", title="同库外部项目"))
        session.flush()
        session.add(
            StoryNode(
                id="foreign-chapter",
                project_id="foreign-project",
                kind="chapter",
                title="外部章节",
                order_index=1,
            )
        )
        session.flush()
        session.add_all(
            [
                ChapterVersion(
                    id="foreign-version",
                    project_id="foreign-project",
                    chapter_id="foreign-chapter",
                    content="外部正文",
                    source="manual",
                ),
                # A corrupt cross-column pair must not pass merely because the
                # version row itself claims to belong to the current project.
                ChapterVersion(
                    id="foreign-chapter-version",
                    project_id=project_id,
                    chapter_id="foreign-chapter",
                    content="错误归属正文",
                    source="manual",
                ),
            ]
        )


def _assert_no_material_or_projection(client, project_id, kind):
    workspace_key = "canon_facts" if kind == "canon" else "plots"
    assert client.get(f"/api/v1/projects/{project_id}/workspace").json()[workspace_key] == []
    database = client.app.state.vault_registry.require(project_id).database
    with database.session_scope() as session:
        assert (
            session.scalar(
                text("SELECT count(*) FROM search_documents WHERE source_type=:kind"),
                {"kind": kind},
            )
            == 0
        )


def test_canon_create_rejects_reversed_range_before_persistence(
    client, project, seeded_chapter, monkeypatch
):
    from novel_harness.services import story

    add_calls = []
    original_add_record = story.add_record

    def track_add(*args, **kwargs):
        add_calls.append((args, kwargs))
        return original_add_record(*args, **kwargs)

    monkeypatch.setattr(story, "add_record", track_add)
    later = _later_chapter(client, project["id"], seeded_chapter)

    response = client.post(
        f"/api/v1/projects/{project['id']}/canon",
        json={
            "predicate": "location",
            "value": "tower",
            "valid_from_node_id": later["id"],
            "valid_to_node_id": seeded_chapter,
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INVALID_STORY_RANGE"
    assert add_calls == []
    _assert_no_material_or_projection(client, project["id"], "canon")


def test_plot_create_rejects_reversed_range_before_persistence(
    client, project, seeded_chapter, monkeypatch
):
    from novel_harness.services import story

    add_calls = []
    original_add_record = story.add_record

    def track_add(*args, **kwargs):
        add_calls.append((args, kwargs))
        return original_add_record(*args, **kwargs)

    monkeypatch.setattr(story, "add_record", track_add)
    later = _later_chapter(client, project["id"], seeded_chapter)

    response = client.post(
        f"/api/v1/projects/{project['id']}/plots",
        json={
            "kind": "main",
            "title": "逆序线索",
            "start_node_id": later["id"],
            "due_node_id": seeded_chapter,
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INVALID_STORY_RANGE"
    assert add_calls == []
    _assert_no_material_or_projection(client, project["id"], "plot")


def test_canon_edit_rejects_reversed_range_and_preserves_projection(
    client, project, seeded_chapter
):
    later = _later_chapter(client, project["id"], seeded_chapter)
    base = f"/api/v1/projects/{project['id']}"
    canon = client.post(
        base + "/canon",
        json={
            "predicate": "location",
            "value": "dock",
            "valid_from_node_id": seeded_chapter,
            "valid_to_node_id": later["id"],
        },
    ).json()
    projection = _projection(client, project["id"], f"canon:{canon['id']}")

    response = client.patch(
        base + f"/library/canon/{canon['id']}",
        json={
            "revision": canon["revision"],
            "fields": {
                "valid_from_node_id": later["id"],
                "valid_to_node_id": seeded_chapter,
            },
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INVALID_STORY_RANGE"
    stored = client.get(base + f"/library/canon/{canon['id']}").json()
    assert stored["record"]["valid_from_node_id"] == seeded_chapter
    assert stored["record"]["valid_to_node_id"] == later["id"]
    assert _projection(client, project["id"], f"canon:{canon['id']}") == projection


def test_plot_edit_rejects_reversed_range_and_preserves_projection(
    client, project, seeded_chapter
):
    later = _later_chapter(client, project["id"], seeded_chapter)
    base = f"/api/v1/projects/{project['id']}"
    plot = client.post(
        base + "/plots",
        json={
            "kind": "main",
            "title": "正序线索",
            "start_node_id": seeded_chapter,
            "due_node_id": later["id"],
        },
    ).json()
    projection = _projection(client, project["id"], f"plot:{plot['id']}")

    response = client.patch(
        base + f"/library/plot/{plot['id']}",
        json={
            "revision": plot["revision"],
            "fields": {
                "start_node_id": later["id"],
                "due_node_id": seeded_chapter,
            },
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INVALID_STORY_RANGE"
    stored = client.get(base + f"/library/plot/{plot['id']}").json()
    assert stored["record"]["start_node_id"] == seeded_chapter
    assert stored["record"]["due_node_id"] == later["id"]
    assert _projection(client, project["id"], f"plot:{plot['id']}") == projection


def test_canon_create_rejects_missing_and_foreign_source_versions_without_residue(
    client, project, seeded_chapter, monkeypatch
):
    from novel_harness.services import story

    add_calls = []
    original_add_record = story.add_record

    def track_add(*args, **kwargs):
        add_calls.append((args, kwargs))
        return original_add_record(*args, **kwargs)

    monkeypatch.setattr(story, "add_record", track_add)
    _foreign_source_versions(client, project["id"])
    base = f"/api/v1/projects/{project['id']}"

    missing = client.post(
        base + "/canon",
        json={"predicate": "source", "value": "missing", "source_version_id": "missing"},
    )
    foreign = client.post(
        base + "/canon",
        json={
            "predicate": "source",
            "value": "foreign",
            "source_version_id": "foreign-version",
        },
    )
    foreign_chapter = client.post(
        base + "/canon",
        json={
            "predicate": "source",
            "value": "foreign chapter",
            "source_version_id": "foreign-chapter-version",
        },
    )

    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "REFERENCE_NOT_FOUND"
    assert foreign.status_code == 409
    assert foreign.json()["detail"]["code"] == "CROSS_PROJECT_REFERENCE"
    assert foreign_chapter.status_code == 409
    assert foreign_chapter.json()["detail"]["code"] == "CROSS_PROJECT_REFERENCE"
    assert add_calls == []
    _assert_no_material_or_projection(client, project["id"], "canon")


def test_canon_source_version_validation_has_create_edit_parity(
    client, project, seeded_chapter
):
    source_version_id = _source_version(client, project["id"], seeded_chapter)
    _foreign_source_versions(client, project["id"])
    base = f"/api/v1/projects/{project['id']}"
    canon = client.post(
        base + "/canon",
        json={
            "predicate": "source",
            "value": "valid",
            "source_version_id": source_version_id,
        },
    )
    assert canon.status_code == 201
    canon = canon.json()
    projection = _projection(client, project["id"], f"canon:{canon['id']}")

    for version_id, status, code in [
        ("missing", 404, "REFERENCE_NOT_FOUND"),
        ("foreign-version", 409, "CROSS_PROJECT_REFERENCE"),
        ("foreign-chapter-version", 409, "CROSS_PROJECT_REFERENCE"),
    ]:
        response = client.patch(
            base + f"/library/canon/{canon['id']}",
            json={
                "revision": canon["revision"],
                "fields": {"source_version_id": version_id},
            },
        )
        assert response.status_code == status
        assert response.json()["detail"]["code"] == code

    stored = client.get(base + f"/library/canon/{canon['id']}").json()
    assert stored["revision"] == canon["revision"]
    assert stored["record"]["source_version_id"] == source_version_id
    assert _projection(client, project["id"], f"canon:{canon['id']}") == projection


@pytest.mark.parametrize(
    ("kind", "model", "values"),
    [
        (
            "canon",
            CanonFact,
            {"predicate": "locked", "value": True, "valid_from_node_id": "{node}"},
        ),
        (
            "plot",
            PlotThread,
            {
                "kind": "main",
                "title": "锁内线索",
                "start_node_id": "{node}",
            },
        ),
    ],
)
def test_material_create_write_lock_serializes_a_later_node_delete(
    client, project, seeded_chapter, kind, model, values
):
    from novel_harness.services import story
    from novel_harness.services.library import trash_item

    database = client.app.state.vault_registry.require(project["id"]).database
    create_reached_flush = Event()
    release_create = Event()
    delete_lock_attempted = Event()
    delete_lock_acquired = Event()
    delete_worker = {}
    ordering = []
    ordering_lock = Lock()

    def record_order(marker):
        with ordering_lock:
            ordering.append(marker)

    def pause_after_validation(session, _flush_context, _instances):
        if session.info.get("pause_material_create") and any(
            isinstance(row, model) for row in session.new
        ):
            create_reached_flush.set()
            assert release_create.wait(5)

    def observe_begin(_connection, _cursor, statement, _parameters, _context, _many):
        if (
            get_ident() == delete_worker.get("ident")
            and statement.strip().upper().startswith("BEGIN IMMEDIATE")
        ):
            delete_lock_attempted.set()

    def observe_begin_complete(
        _connection, _cursor, statement, _parameters, _context, _many
    ):
        if (
            get_ident() == delete_worker.get("ident")
            and statement.strip().upper().startswith("BEGIN IMMEDIATE")
            and not delete_lock_acquired.is_set()
        ):
            delete_lock_acquired.set()
            record_order("delete_lock_acquired")

    event.listen(Session, "before_flush", pause_after_validation)
    event.listen(database.engine, "before_cursor_execute", observe_begin)
    event.listen(database.engine, "after_cursor_execute", observe_begin_complete)
    try:
        def create_material():
            with database.session_scope() as session:
                session.info["pause_material_create"] = True
                checked = {
                    key: value.replace("{node}", seeded_chapter)
                    if isinstance(value, str)
                    else value
                    for key, value in values.items()
                }
                add = story.add_canon if kind == "canon" else story.add_plot
                created_id = add(session, project["id"], checked).id
                record_order("create_ready_to_commit")
            return created_id

        def delete_node():
            delete_worker["ident"] = get_ident()
            assert create_reached_flush.wait(5)
            with database.session_scope() as session:
                result = trash_item(session, "node", seeded_chapter)
            record_order("delete_committed")
            return result

        with ThreadPoolExecutor(max_workers=2) as pool:
            creating = pool.submit(create_material)
            assert create_reached_flush.wait(5)
            with database.engine.connect() as contender:
                contender.exec_driver_sql("PRAGMA busy_timeout=0")
                try:
                    contender.exec_driver_sql("BEGIN IMMEDIATE")
                except OperationalError:
                    create_holds_write_lock = True
                else:
                    create_holds_write_lock = False
                    contender.rollback()
                finally:
                    contender.exec_driver_sql("PRAGMA busy_timeout=5000")
            deleting = pool.submit(delete_node)
            assert delete_lock_attempted.wait(5)
            release_create.set()
            created_id = creating.result(timeout=5)
            deleted = deleting.result(timeout=5)
    finally:
        release_create.set()
        event.remove(Session, "before_flush", pause_after_validation)
        event.remove(database.engine, "before_cursor_execute", observe_begin)
        event.remove(database.engine, "after_cursor_execute", observe_begin_complete)

    assert create_holds_write_lock
    assert ordering == [
        "create_ready_to_commit",
        "delete_lock_acquired",
        "delete_committed",
    ]
    assert deleted["id"] == seeded_chapter
    with database.session_scope() as session:
        assert session.get(model, created_id) is not None
        deleted_node = session.scalar(
            select(StoryNode)
            .where(StoryNode.id == seeded_chapter)
            .execution_options(include_deleted=True)
        )
        assert deleted_node.deleted_at is not None


@pytest.mark.parametrize(
    ("case", "kind", "model"),
    [
        ("canon_range", "canon", CanonFact),
        ("plot_start", "plot", PlotThread),
        ("canon_source", "canon", CanonFact),
    ],
)
def test_material_create_rejects_a_reference_deleted_by_an_earlier_writer(
    client, project, seeded_chapter, case, kind, model
):
    from novel_harness.services import story
    from novel_harness.services.library import trash_item

    database = client.app.state.vault_registry.require(project["id"]).database
    source_version_id = (
        _source_version(client, project["id"], seeded_chapter, "deleted-source-version")
        if case == "canon_source"
        else None
    )
    delete_committed = Event()

    def delete_node():
        with database.session_scope() as session:
            trash_item(session, "node", seeded_chapter)
        delete_committed.set()

    def create_material():
        assert delete_committed.wait(5)
        values = {
            "canon_range": {
                "predicate": "deleted",
                "value": True,
                "valid_from_node_id": seeded_chapter,
            },
            "plot_start": {
                "kind": "main",
                "title": "已删除起点",
                "start_node_id": seeded_chapter,
            },
            "canon_source": {
                "predicate": "deleted source",
                "value": True,
                "source_version_id": source_version_id,
            },
        }[case]
        add = story.add_canon if kind == "canon" else story.add_plot
        try:
            with database.session_scope() as session:
                # A long-lived service session may already have loaded an
                # auditable tombstone explicitly; validation must not rely on
                # the default loader criterion to hide it from session.get().
                session.get(
                    StoryNode,
                    seeded_chapter,
                    execution_options={"include_deleted": True},
                )
                add(session, project["id"], values)
        except HTTPException as exc:
            return ("http", exc.status_code, exc.detail.get("code"))
        except Exception as exc:  # Turn an accidental 500 into an assertion failure.
            return ("exception", type(exc).__name__, str(exc))
        return ("created", None, None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        deleting = pool.submit(delete_node)
        creating = pool.submit(create_material)
        deleting.result(timeout=5)
        result = creating.result(timeout=5)

    with database.session_scope() as session:
        row_count = session.scalar(select(model.id).where(model.project_id == project["id"]))
        projection_count = session.scalar(
            text("SELECT count(*) FROM search_documents WHERE source_type=:kind"),
            {"kind": kind},
        )
    assert (result, row_count, projection_count) == (
        ("http", 404, "REFERENCE_NOT_FOUND"),
        None,
        0,
    )


@pytest.mark.parametrize("relation_endpoint", ["source", "target"])
def test_entity_delete_retains_dependents_and_reports_stable_inactive_ids(
    client, project, relation_endpoint
):
    subject, _, canon, relation = _entity_dependencies(
        client, project["id"], relation_endpoint
    )
    base = f"/api/v1/projects/{project['id']}"

    response = client.delete(base + f"/library/entity/{subject['id']}")

    assert response.status_code == 200
    assert response.json()["inactive_dependents"] == [
        {"type": "canon", "id": canon["id"], "reason": "entity_deleted"},
        {"type": "relation", "id": relation["id"], "reason": "entity_deleted"},
    ]
    database = client.app.state.vault_registry.require(project["id"]).database
    with database.session_scope() as session:
        assert session.get(CanonFact, canon["id"]) is not None
        assert session.get(EntityRelation, relation["id"]) is not None
        assert session.scalar(
            text(
                "SELECT count(*) FROM search_documents "
                "WHERE key IN (:canon_key,:relation_key)"
            ),
            {"canon_key": f"canon:{canon['id']}", "relation_key": f"relation:{relation['id']}"},
        ) == 0


def test_deleted_entity_dependents_exit_hard_search_and_explicit_reference_channels(
    client, project, seeded_chapter
):
    from novel_harness.services.writing_context import collect_hard_context

    subject, _, canon, relation = _entity_dependencies(client, project["id"])
    base = f"/api/v1/projects/{project['id']}"
    assert client.delete(base + f"/library/entity/{subject['id']}").status_code == 200

    database = client.app.state.vault_registry.require(project["id"]).database
    with database.session_scope() as session:
        hard, _ = collect_hard_context(
            session,
            session.get(Project, project["id"]),
            seeded_chapter,
            {},
        )
    assert canon["id"] not in {fragment.source_id for fragment in hard}
    for needle, dependent_id in (
        ("CANON_DEPENDENCY_NEEDLE", canon["id"]),
        ("RELATION_DEPENDENCY_NEEDLE", relation["id"]),
    ):
        hits = client.get(base + "/library/search", params={"q": needle}).json()["items"]
        assert dependent_id not in {item["id"] for item in hits}
        kind = "canon" if dependent_id == canon["id"] else "relation"
        explicit = client.post(
            base + "/ai/jobs/preflight",
            json={
                "project_id": project["id"],
                "task_type": "chat",
                "instructions": f"[[ref:{kind}:{dependent_id}]]",
            },
        )
        assert explicit.status_code == 422
        assert explicit.json()["detail"]["code"] == "REFERENCE_UNAVAILABLE"


def test_authoritative_dependency_check_blocks_corrupt_projection_and_vector_candidates(
    client, project
):
    from novel_harness.services.library import present
    from novel_harness.services.retrieval import search
    from novel_harness.services.search_index import insert_projection, remove_projection

    subject, _, canon, relation = _entity_dependencies(client, project["id"])
    base = f"/api/v1/projects/{project['id']}"
    client.delete(base + f"/library/entity/{subject['id']}")
    database = client.app.state.vault_registry.require(project["id"]).database

    class CapturingVectors:
        def __init__(self):
            self.eligible_keys = set()

        def search_chunks(self, _query, _limit, *, eligible_keys=None):
            self.eligible_keys = set(eligible_keys or ())
            return [(key, 1.0) for key in sorted(self.eligible_keys)]

    vectors = CapturingVectors()
    with database.session_scope() as session:
        # Deliberately corrupt the derived index after deletion. Correctness must
        # still come from the authoritative Canon/Relation/Entity rows.
        for kind, model, item_id in (
            ("canon", CanonFact, canon["id"]),
            ("relation", EntityRelation, relation["id"]),
        ):
            record = session.get(model, item_id)
            remove_projection(session, f"{kind}:{item_id}")
            insert_projection(session, present(kind, record))
        result = search(session, "DEPENDENCY_NEEDLE", vectors=vectors)

    assert not {canon["id"], relation["id"]} & {
        item["id"] for item in result["items"]
    }
    assert not any(
        key.startswith((f"canon:{canon['id']}:", f"relation:{relation['id']}:"))
        for key in vectors.eligible_keys
    )
    for kind, item_id in (("canon", canon["id"]), ("relation", relation["id"])):
        response = client.post(
            base + "/ai/jobs/preflight",
            json={
                "project_id": project["id"],
                "task_type": "chat",
                "instructions": f"[[ref:{kind}:{item_id}]]",
            },
        )
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "REFERENCE_UNAVAILABLE"


def test_entity_restore_reactivates_retained_dependencies_without_recreation(
    client, project, seeded_chapter
):
    from novel_harness.services.search_index import remove_projection
    from novel_harness.services.writing_context import collect_hard_context

    subject, _, canon, relation = _entity_dependencies(client, project["id"])
    base = f"/api/v1/projects/{project['id']}"
    client.delete(base + f"/library/entity/{subject['id']}")
    database = client.app.state.vault_registry.require(project["id"]).database
    with database.session_scope() as session:
        remove_projection(session, f"canon:{canon['id']}")
        remove_projection(session, f"relation:{relation['id']}")

    restored = client.post(base + f"/trash/entity/{subject['id']}/restore")

    assert restored.status_code == 200
    assert restored.json()["reactivated_dependents"] == [
        {"type": "canon", "id": canon["id"]},
        {"type": "relation", "id": relation["id"]},
    ]
    for needle, kind, item_id in (
        ("CANON_DEPENDENCY_NEEDLE", "canon", canon["id"]),
        ("RELATION_DEPENDENCY_NEEDLE", "relation", relation["id"]),
    ):
        hits = client.get(base + "/library/search", params={"q": needle}).json()["items"]
        assert item_id in {item["id"] for item in hits}
        explicit = client.post(
            base + "/ai/jobs/preflight",
            json={
                "project_id": project["id"],
                "task_type": "chat",
                "instructions": f"[[ref:{kind}:{item_id}]]",
            },
        )
        assert explicit.status_code == 200
    with database.session_scope() as session:
        hard, _ = collect_hard_context(
            session,
            session.get(Project, project["id"]),
            seeded_chapter,
            {},
        )
    assert canon["id"] in {fragment.source_id for fragment in hard}


def test_entity_delete_rolls_back_when_dependent_projection_sync_fails(
    client, project, monkeypatch
):
    from novel_harness.services import search_index

    subject, _, canon, _ = _entity_dependencies(client, project["id"])
    base = f"/api/v1/projects/{project['id']}"
    original_sync = search_index.sync_record

    def fail_on_dependent(session, record, **kwargs):
        if isinstance(record, CanonFact):
            raise RuntimeError("projection failure")
        return original_sync(session, record, **kwargs)

    monkeypatch.setattr(search_index, "sync_record", fail_on_dependent)
    response = client.delete(base + f"/library/entity/{subject['id']}")
    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "INTERNAL_ERROR"

    database = client.app.state.vault_registry.require(project["id"]).database
    with database.session_scope() as session:
        assert session.get(Entity, subject["id"]) is not None
        assert session.scalar(
            text("SELECT count(*) FROM search_documents WHERE key=:key"),
            {"key": f"canon:{canon['id']}"},
        ) == 1


def test_pending_and_retracted_canon_never_report_dependency_lifecycle_transition(
    client, project, seeded_chapter
):
    from novel_harness.services.writing_context import collect_hard_context

    subject, _, confirmed, relation = _entity_dependencies(client, project["id"])
    base = f"/api/v1/projects/{project['id']}"
    inactive_canon = [
        client.post(
            base + "/canon",
            json={
                "subject_entity_id": subject["id"],
                "predicate": f"{status.upper()}_DEPENDENCY_NEEDLE",
                "value": "not projectable",
                "status": status,
            },
        ).json()
        for status in ("pending", "retracted")
    ]

    deleted = client.delete(base + f"/library/entity/{subject['id']}").json()
    assert deleted["inactive_dependents"] == [
        {"type": "canon", "id": confirmed["id"], "reason": "entity_deleted"},
        {"type": "relation", "id": relation["id"], "reason": "entity_deleted"},
    ]
    restored = client.post(base + f"/trash/entity/{subject['id']}/restore").json()
    assert restored["reactivated_dependents"] == [
        {"type": "canon", "id": confirmed["id"]},
        {"type": "relation", "id": relation["id"]},
    ]

    database = client.app.state.vault_registry.require(project["id"]).database
    with database.session_scope() as session:
        hard, _ = collect_hard_context(
            session,
            session.get(Project, project["id"]),
            seeded_chapter,
            {},
        )
        hard_ids = {fragment.source_id for fragment in hard}
        projected = set(
            session.scalars(
                text(
                    "SELECT source_id FROM search_documents "
                    "WHERE source_id IN :ids"
                ).bindparams(bindparam("ids", expanding=True)),
                {"ids": [item["id"] for item in inactive_canon]},
            )
        )
    assert projected == set()
    assert not {item["id"] for item in inactive_canon} & hard_ids


def test_restore_requires_an_actual_entity_tombstone_without_mutating_active_entity(
    client, project
):
    base = f"/api/v1/projects/{project['id']}"
    entity = client.post(
        base + "/entities",
        json={"kind": "character", "name": "从未删除", "summary": "ACTIVE_ENTITY_NEEDLE"},
    ).json()
    database = client.app.state.vault_registry.require(project["id"]).database
    with database.session_scope() as session:
        projection_before = session.scalar(
            text("SELECT data FROM search_documents WHERE key=:key"),
            {"key": f"entity:{entity['id']}"},
        )

    response = client.post(base + f"/trash/entity/{entity['id']}/restore")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "MATERIAL_NOT_FOUND"
    with database.session_scope() as session:
        stored = session.get(Entity, entity["id"])
        assert stored.revision == entity["revision"]
        assert stored.deleted_at is None
        assert session.scalar(
            text("SELECT data FROM search_documents WHERE key=:key"),
            {"key": f"entity:{entity['id']}"},
        ) == projection_before


@pytest.mark.parametrize(
    ("operation", "expected_revision", "lifecycle_field"),
    [
        ("delete", 2, "inactive_dependents"),
        ("restore", 3, "reactivated_dependents"),
    ],
)
def test_entity_lifecycle_serializes_two_connections_to_one_transition(
    client, project, monkeypatch, operation, expected_revision, lifecycle_field
):
    from novel_harness.services import library

    base = f"/api/v1/projects/{project['id']}"
    entity = client.post(
        base + "/entities", json={"kind": "character", "name": "并发生命周期"}
    ).json()
    database = client.app.state.vault_registry.require(project["id"]).database
    if operation == "restore":
        with database.session_scope() as session:
            library.trash_item(session, "entity", entity["id"])

    original_require = library.require_item
    both_read_without_lock = Barrier(2)

    def coordinated_require(session, kind, item_id, *, deleted=False):
        item = original_require(session, kind, item_id, deleted=deleted)
        in_transaction = session.connection().connection.driver_connection.in_transaction
        if kind == "entity" and not in_transaction:
            both_read_without_lock.wait(5)
        return item

    monkeypatch.setattr(library, "require_item", coordinated_require)
    start = Event()

    def transition():
        start.wait(5)
        try:
            with database.session_scope() as session:
                result = (
                    library.trash_item(session, "entity", entity["id"])
                    if operation == "delete"
                    else library.restore_item(session, "entity", entity["id"])
                )
            return ("ok", result)
        except HTTPException as exc:
            return ("http", exc.status_code, exc.detail["code"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(transition) for _ in range(2)]
        start.set()
        results = [future.result(timeout=10) for future in futures]

    successes = [result[1] for result in results if result[0] == "ok"]
    failures = [result for result in results if result[0] == "http"]
    assert len(successes) == 1
    assert failures == [("http", 404, "MATERIAL_NOT_FOUND")]
    assert successes[0]["revision"] == expected_revision
    assert successes[0][lifecycle_field] == []
    with database.session_scope() as session:
        stored = session.get(
            Entity,
            entity["id"],
            execution_options={"include_deleted": True},
        )
        assert stored.revision == expected_revision
        assert (stored.deleted_at is not None) is (operation == "delete")


def test_dependency_response_reports_only_real_pre_to_post_activity_transitions(
    client, project
):
    subject, other, canon, relation = _entity_dependencies(client, project["id"])
    base = f"/api/v1/projects/{project['id']}"

    other_deleted = client.delete(base + f"/library/entity/{other['id']}").json()
    assert other_deleted["inactive_dependents"] == [
        {"type": "relation", "id": relation["id"], "reason": "entity_deleted"}
    ]

    subject_deleted = client.delete(base + f"/library/entity/{subject['id']}").json()
    assert subject_deleted["inactive_dependents"] == [
        {"type": "canon", "id": canon["id"], "reason": "entity_deleted"}
    ]
    subject_restored = client.post(
        base + f"/trash/entity/{subject['id']}/restore"
    ).json()
    assert subject_restored["reactivated_dependents"] == [
        {"type": "canon", "id": canon["id"]}
    ]
    other_restored = client.post(base + f"/trash/entity/{other['id']}/restore").json()
    assert other_restored["reactivated_dependents"] == [
        {"type": "relation", "id": relation["id"]}
    ]

    client.delete(base + f"/library/relation/{relation['id']}")
    subject_deleted_again = client.delete(
        base + f"/library/entity/{subject['id']}"
    ).json()
    assert subject_deleted_again["inactive_dependents"] == [
        {"type": "canon", "id": canon["id"], "reason": "entity_deleted"}
    ]


def test_dependency_indexes_are_added_to_old_vault_and_used_by_sqlite(tmp_path):
    database = Database(tmp_path / "old-vault.db")
    database.create_schema()
    expected = {
        "ix_canon_facts_subject_entity_id",
        "ix_entity_relations_source_entity_id",
        "ix_entity_relations_target_entity_id",
    }
    with database.engine.begin() as connection:
        for name in expected:
            connection.exec_driver_sql(f'DROP INDEX IF EXISTS "{name}"')

    database.create_schema()

    with database.engine.connect() as connection:
        indexes = {
            row[1]
            for table in ("canon_facts", "entity_relations")
            for row in connection.exec_driver_sql(f'PRAGMA index_list("{table}")')
        }
        canon_plan = " ".join(
            row[3]
            for row in connection.exec_driver_sql(
                "EXPLAIN QUERY PLAN SELECT * FROM canon_facts "
                "WHERE subject_entity_id='entity'"
            )
        )
        relation_plan = " ".join(
            row[3]
            for row in connection.exec_driver_sql(
                "EXPLAIN QUERY PLAN SELECT * FROM entity_relations "
                "WHERE source_entity_id='entity' OR target_entity_id='entity'"
            )
        )
    database.dispose()

    assert expected <= indexes
    assert "SCAN canon_facts" not in canon_plan
    assert "SCAN entity_relations" not in relation_plan


def test_dependent_activity_select_count_is_constant_with_many_records(client, project):
    from novel_harness.services.library import trash_item

    database = client.app.state.vault_registry.require(project["id"]).database

    def measure(count, suffix):
        with database.session_scope() as session:
            entity = Entity(
                id=f"counted-{suffix}",
                project_id=project["id"],
                kind="character",
                name=f"Counted {suffix}",
            )
            session.add(entity)
            session.flush()
            session.add_all(
                CanonFact(
                    project_id=project["id"],
                    subject_entity_id=entity.id,
                    predicate=f"COUNTED_{suffix}_{index}",
                    value=index,
                )
                for index in range(count)
            )

        statements = []

        def record_select(_connection, _cursor, statement, _parameters, _context, _many):
            normalized = " ".join(statement.upper().split())
            if normalized.startswith("SELECT"):
                statements.append(normalized)

        event.listen(database.engine, "before_cursor_execute", record_select)
        try:
            with database.session_scope() as session:
                trash_item(session, "entity", entity.id)
        finally:
            event.remove(database.engine, "before_cursor_execute", record_select)
        entity_selects = sum(" FROM ENTITIES " in statement for statement in statements)
        dependent_selects = sum(
            " FROM CANON_FACTS " in statement or " FROM ENTITY_RELATIONS " in statement
            for statement in statements
        )
        return entity_selects, dependent_selects

    small = measure(1, "small")
    large = measure(25, "large")

    assert small == large
    assert small[1] == 1


def test_rebuild_index_dependency_select_count_is_constant_with_many_canon_records(
    client, project
):
    from novel_harness.services.search_index import rebuild_index

    database = client.app.state.vault_registry.require(project["id"]).database

    def measure(count, suffix):
        with database.session_scope() as session:
            entity = Entity(
                id=f"rebuild-counted-{suffix}",
                project_id=project["id"],
                kind="character",
                name=f"Rebuild Counted {suffix}",
            )
            session.add(entity)
            session.flush()
            session.add_all(
                CanonFact(
                    project_id=project["id"],
                    subject_entity_id=entity.id,
                    predicate=f"REBUILD_{suffix}_{index}",
                    value=index,
                )
                for index in range(count)
            )

        statements = []

        def record_select(_connection, _cursor, statement, _parameters, _context, _many):
            normalized = " ".join(statement.upper().split())
            if normalized.startswith("SELECT"):
                statements.append(normalized)

        event.listen(database.engine, "before_cursor_execute", record_select)
        try:
            with database.session_scope() as session:
                rebuild_index(session)
        finally:
            event.remove(database.engine, "before_cursor_execute", record_select)
        return sum(" FROM ENTITIES " in statement for statement in statements)

    assert measure(1, "small") == measure(25, "large") == 2


def test_rebuild_indexes_chapter_manuscript_and_summary_once(
    client, project, seeded_chapter, monkeypatch
):
    from novel_harness.services import search_index

    database = client.app.state.vault_registry.require(project["id"]).database
    version_id = _source_version(
        client, project["id"], seeded_chapter, "rebuild-exactly-once-version"
    )
    with database.session_scope() as session:
        document = session.get(ChapterDocument, seeded_chapter)
        if document is None:
            session.add(
                ChapterDocument(
                    chapter_id=seeded_chapter,
                    project_id=project["id"],
                    content="重建正文",
                )
            )
        session.add(
            ChapterSummary(
                id="rebuild-exactly-once-summary",
                project_id=project["id"],
                chapter_id=seeded_chapter,
                version_id=version_id,
                title="第一章总结",
                content_hash="0" * 64,
                recap="重建总结",
                details={},
            )
        )

    record_calls = []
    manuscript_calls = []
    original_record = search_index.sync_record
    original_manuscript = search_index.sync_manuscript

    def track_record(session, record, **kwargs):
        record_calls.append((type(record).__name__, record.id))
        return original_record(session, record, **kwargs)

    def track_manuscript(session, chapter_id):
        manuscript_calls.append(chapter_id)
        return original_manuscript(session, chapter_id)

    monkeypatch.setattr(search_index, "sync_record", track_record)
    monkeypatch.setattr(search_index, "sync_manuscript", track_manuscript)
    with database.session_scope() as session:
        search_index.rebuild_index(session)

    assert record_calls.count(("StoryNode", seeded_chapter)) == 1
    assert record_calls.count(("ChapterSummary", "rebuild-exactly-once-summary")) == 1
    assert manuscript_calls.count(seeded_chapter) == 1


def test_query_token_selection_is_stable_bounded_and_preserves_chinese_tokens():
    from novel_harness.services import retrieval
    from novel_harness.services.search_index import tokenize

    selector = getattr(retrieval, "select_query_tokens", None)
    assert callable(selector)

    short = "alpha beta alpha 甲乙丙"
    assert selector(short) == tokenize(short)

    unique = [f"word{index}" for index in range(90)]
    assert selector(" ".join(unique)) == unique[:40] + unique[-40:]

    repeated = ["repeat"] * 100 + [f"item{index}" for index in range(81)]
    selected = selector(" ".join(repeated))
    assert len(selected) == 80
    assert selected.count("repeat") == 1
    assert selected == ["repeat", *[f"item{index}" for index in range(39)]] + [
        f"item{index}" for index in range(41, 81)
    ]

    assert selector("甲乙丙") == ["甲", "乙", "丙", "甲乙", "乙丙"]


@pytest.mark.parametrize(
    ("tail", "title"),
    [("tailneedle", "English tail"), ("尾针", "中文尾项")],
)
def test_long_query_keeps_a_unique_tail_token_for_fts(client, project, tail, title):
    from novel_harness.services.retrieval import search

    database = client.app.state.vault_registry.require(project["id"]).database
    with database.session_scope() as session:
        source = Idea(project_id=project["id"], title=title, content=tail)
        session.add(source)
        session.flush()
        source_id = source.id

    query = " ".join([*(f"word{index}" for index in range(80)), tail])
    with database.session_scope() as session:
        result = search(session, query)

    assert [item["id"] for item in result["items"]] == [source_id]
    assert result["items"][0]["channels"] == ["fts"]


def test_long_query_is_passed_to_vector_retrieval_without_truncation(client, project):
    from novel_harness.services.retrieval import search

    class RecordingVectors:
        def __init__(self):
            self.queries = []

        def status(self):
            return "ready"

        def search(self, query, limit):
            self.queries.append(query)
            return []

    query = " ".join(f"word{index}" for index in range(100)) + " 最终要求"
    vectors = RecordingVectors()
    database = client.app.state.vault_registry.require(project["id"]).database
    with database.session_scope() as session:
        search(session, query, vectors=vectors)

    assert vectors.queries == [query]
