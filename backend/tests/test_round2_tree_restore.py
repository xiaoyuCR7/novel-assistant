"""Restore/edit/trash preserve a live, same-project, acyclic ancestor chain."""

import sqlite3
from contextlib import closing
from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import event, select

from novel_harness.db.base import utc_now
from novel_harness.db.models import Project, StoryNode
from novel_harness.services import library, versions


@pytest.fixture
def tree(client, project):
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        for item_id, parent_id, kind in (
            ("grandparent", None, "volume"),
            ("parent", "grandparent", "chapter"),
            ("child", "parent", "scene"),
        ):
            session.add(
                StoryNode(
                    id=item_id,
                    project_id=project["id"],
                    parent_id=parent_id,
                    title=item_id,
                    kind=kind,
                )
            )
            session.flush()
    return vault


def node_url(project, item_id):
    return f"/api/v1/projects/{project['id']}/library/node/{item_id}"


def restore_url(project, item_id):
    return f"/api/v1/projects/{project['id']}/trash/node/{item_id}/restore"


def stored_node(vault, item_id):
    with vault.database.session_scope() as session:
        item = session.scalar(
            select(StoryNode).where(StoryNode.id == item_id).execution_options(include_deleted=True)
        )
        return library.present("node", item)


def mark_deleted(session, node):
    node.deleted_at = utc_now()
    node.purge_after = node.deleted_at + timedelta(days=30)
    node.revision += 1


def test_child_restore_rejected_atomically_until_ancestors_restored(tree, client, project):
    for item_id in ("child", "parent", "grandparent"):
        assert client.delete(node_url(project, item_id)).status_code == 200
    before = stored_node(tree, "child")
    rejected = client.post(restore_url(project, "child"))
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["code"] == "NODE_PARENT_UNAVAILABLE"
    assert rejected.json()["detail"]["message"]
    assert stored_node(tree, "child") == before
    assert (
        client.delete(f"/api/v1/projects/{project['id']}/trash/node/child/purge").status_code == 422
    )
    assert stored_node(tree, "parent")["deleted_at"]
    for item_id in ("grandparent", "parent", "child"):
        restored = client.post(restore_url(project, item_id))
        assert restored.status_code == 200
        assert restored.json()["deleted_at"] is None
        assert restored.json()["purge_after"] is None
        assert restored.json()["revision"] == 3
    navigation = client.get(f"/api/v1/projects/{project['id']}/workspace/navigation").json()
    assert {node["id"] for node in navigation["nodes"]} == {"grandparent", "parent", "child"}


@pytest.mark.parametrize("broken", ["deleted", "missing", "cycle", "foreign"])
def test_restore_validates_entire_ancestor_chain(tree, client, project, broken):
    assert client.delete(node_url(project, "child")).status_code == 200
    if broken == "missing":
        # A legacy damaged Vault can contain dangling IDs; do not disable production constraints.
        with closing(sqlite3.connect(tree.database.path)) as connection, connection:
            connection.execute("UPDATE story_nodes SET parent_id='missing' WHERE id='parent'")
    else:
        with tree.database.session_scope() as session:
            grandparent = session.get(StoryNode, "grandparent")
            if broken == "deleted":
                mark_deleted(session, grandparent)
            elif broken == "cycle":
                grandparent.parent_id = "parent"
            else:
                session.add(Project(id="foreign-project", title="Foreign"))
                session.flush()
                grandparent.project_id = "foreign-project"
    before = stored_node(tree, "child")
    result = client.post(restore_url(project, "child"))
    expected = {
        "deleted": (422, "NODE_PARENT_UNAVAILABLE"),
        "missing": (422, "NODE_PARENT_UNAVAILABLE"),
        "cycle": (422, "NODE_CYCLE"),
        "foreign": (409, "CROSS_PROJECT_REFERENCE"),
    }
    assert result.status_code == expected[broken][0]
    assert result.json()["detail"]["code"] == expected[broken][1]
    assert stored_node(tree, "child") == before


@pytest.mark.parametrize("fields", [{}, {"order_index": 3}])
@pytest.mark.parametrize("broken", ["deleted", "missing", "cycle", "foreign"])
def test_legacy_orphan_edits_fail_gracefully_and_can_move_to_root(
    tree, client, project, fields, broken
):
    if broken == "missing":
        with closing(sqlite3.connect(tree.database.path)) as connection, connection:
            connection.execute("UPDATE story_nodes SET parent_id='missing' WHERE id='parent'")
    else:
        with tree.database.session_scope() as session:
            grandparent = session.get(StoryNode, "grandparent")
            if broken == "deleted":
                mark_deleted(session, grandparent)
            elif broken == "cycle":
                grandparent.parent_id = "parent"
            else:
                session.add(Project(id="foreign-project", title="Foreign"))
                session.flush()
                grandparent.project_id = "foreign-project"
    before = stored_node(tree, "child")
    result = client.patch(
        node_url(project, "child"), json={"revision": 1, "title": "Changed", "fields": fields}
    )
    expected = {
        "deleted": (422, "NODE_PARENT_UNAVAILABLE"),
        "missing": (422, "NODE_PARENT_UNAVAILABLE"),
        "cycle": (422, "NODE_CYCLE"),
        "foreign": (409, "CROSS_PROJECT_REFERENCE"),
    }
    assert result.status_code == expected[broken][0]
    assert result.json()["detail"]["code"] == expected[broken][1]
    assert stored_node(tree, "child") == before
    repaired = client.patch(
        node_url(project, "child"),
        json={"revision": 1, "title": "Repaired", "fields": {"parent_id": None}},
    )
    assert repaired.status_code == 200
    assert repaired.json()["record"]["parent_id"] is None
    assert repaired.json()["revision"] == 2


@pytest.mark.parametrize("operation", ["restore", "trash"])
def test_node_validation_reads_happen_under_existing_writer_lock(tree, operation):
    if operation == "restore":
        with tree.database.session_scope() as session:
            library.trash_item(session, "node", "child")
    observed = []

    def observe(connection, _cursor, statement, *_args):
        if statement.lstrip().upper().startswith("SELECT") and "story_nodes" in statement:
            observed.append(statement)
            assert connection.connection.driver_connection.in_transaction, (
                "Node lifecycle validation must acquire BEGIN IMMEDIATE before any node read"
            )

    event.listen(tree.database.engine, "before_cursor_execute", observe)
    try:
        with tree.database.session_scope() as session:
            if operation == "restore":
                library.restore_item(session, "node", "child")
            else:
                library.trash_item(session, "node", "child")
    finally:
        event.remove(tree.database.engine, "before_cursor_execute", observe)
    assert observed


@pytest.mark.parametrize("operation", ["restore", "trash"])
def test_competing_parent_trash_child_restore_rechecks_after_lock(tree, operation, monkeypatch):
    with tree.database.session_scope() as session:
        library.trash_item(session, "node", "child")
    original = versions.begin_version_write
    competed = []
    with tree.database.session_scope() as target_session:
        # Simulate a caller that read an ancestor before another writer commits.
        stale_parent = target_session.get(StoryNode, "parent")
        assert stale_parent.deleted_at is None

        def competing_write_then_lock(session):
            if session is target_session and not competed:
                competed.append(True)
                with tree.database.session_scope() as rival:
                    if operation == "restore":
                        library.trash_item(rival, "node", "parent")
                    else:
                        library.restore_item(rival, "node", "child")
            original(session)

        monkeypatch.setattr(versions, "begin_version_write", competing_write_then_lock)
        with pytest.raises(HTTPException) as error:
            if operation == "restore":
                library.restore_item(target_session, "node", "child")
            else:
                library.trash_item(target_session, "node", "parent")
        assert competed == [True]
        assert error.value.detail["code"] == (
            "NODE_PARENT_UNAVAILABLE" if operation == "restore" else "NODE_HAS_CHILDREN"
        )
    child, parent = stored_node(tree, "child"), stored_node(tree, "parent")
    assert child["deleted_at"] or parent["deleted_at"] is None
    if operation == "restore":
        assert child["revision"] == 2 and child["purge_after"]
    else:
        assert parent["revision"] == 1
