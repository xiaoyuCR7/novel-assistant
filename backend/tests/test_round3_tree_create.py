"""Creation must share edit/restore/trash's ancestry and transaction guarantees."""

import sqlite3
from contextlib import closing

import pytest
from fastapi import HTTPException
from sqlalchemy import event, select
from test_round2_tree_restore import mark_deleted
from test_round2_tree_restore import tree as tree

from novel_harness.db.models import Project, StoryNode
from novel_harness.services import library, story, versions


@pytest.mark.parametrize("broken", ["deleted", "missing", "cycle", "foreign"])
def test_create_checks_all_ancestors(tree, client, project, broken):
    if broken == "missing":
        with closing(sqlite3.connect(tree.database.path)) as connection, connection:
            connection.execute("UPDATE story_nodes SET parent_id='missing' WHERE id='parent'")
    else:
        with tree.database.session_scope() as session:
            ancestor = session.get(StoryNode, "grandparent")
            if broken == "deleted":
                mark_deleted(session, ancestor)
            elif broken == "cycle":
                ancestor.parent_id = "parent"
            else:
                session.add(Project(id="foreign-project", title="Foreign"))
                session.flush()
                ancestor.project_id = "foreign-project"
    result = client.post(
        f"/api/v1/projects/{project['id']}/nodes",
        json={"kind": "scene", "title": "Must not be created", "parent_id": "child"},
    )
    status, code = {
        "deleted": (422, "NODE_PARENT_UNAVAILABLE"),
        "missing": (422, "NODE_PARENT_UNAVAILABLE"),
        "cycle": (422, "NODE_CYCLE"),
        "foreign": (409, "CROSS_PROJECT_REFERENCE"),
    }[broken]
    assert result.status_code == status
    assert result.json()["detail"]["code"] == code
    with tree.database.session_scope() as session:
        assert len(list(session.scalars(select(StoryNode)))) == (2 if broken == "deleted" else 3)


def test_create_locks_before_node_reads(tree, project):
    observed = []

    def observe(connection, _cursor, statement, *_args):
        if statement.lstrip().upper().startswith("SELECT") and "story_nodes" in statement:
            observed.append(statement)
            assert connection.connection.driver_connection.in_transaction

    event.listen(tree.database.engine, "before_cursor_execute", observe)
    try:
        with tree.database.session_scope() as session:
            node = story.add_node(session, project["id"], {
                "kind": "scene", "title": "Safe child", "parent_id": "child",
            })
            assert node.parent_id == "child"
    finally:
        event.remove(tree.database.engine, "before_cursor_execute", observe)
    assert observed


def test_create_rechecks_cached_parent_after_competing_delete(tree, project, monkeypatch):
    original = versions.begin_version_write
    competed = []
    with tree.database.session_scope() as session:
        parent = session.get(StoryNode, "child")
        assert parent.deleted_at is None

        def compete_then_lock(target):
            if target is session and not competed:
                competed.append(True)
                with tree.database.session_scope() as rival:
                    library.trash_item(rival, "node", "child")
            original(target)

        monkeypatch.setattr(versions, "begin_version_write", compete_then_lock)
        with pytest.raises(HTTPException) as error:
            story.add_node(session, project["id"], {
                "kind": "scene", "title": "Orphan", "parent_id": "child",
            })
        assert error.value.detail["code"] == "NODE_PARENT_UNAVAILABLE"
        assert competed == [True]
    with tree.database.session_scope() as session:
        assert not session.scalar(select(StoryNode).where(StoryNode.title == "Orphan"))
