from sqlalchemy import event

from novel_harness.services.retrieval import search


def test_empty_trash_collection_uses_one_expiry_query(client, project):
    vault = client.app.state.vault_registry.require(project["id"])
    statements = []

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        if "purge_after <=" in statement:
            statements.append(statement)

    event.listen(vault.database.engine, "before_cursor_execute", record)
    try:
        assert client.get(f"/api/v1/projects/{project['id']}/ai/jobs").status_code == 200
        assert statements == []
        assert client.get(f"/api/v1/projects/{project['id']}/library").status_code == 200
    finally:
        event.remove(vault.database.engine, "before_cursor_execute", record)
    assert len(statements) == 1


def test_excluded_documents_are_filtered_before_json_loading(client, project):
    base = f"/api/v1/projects/{project['id']}"
    entity = client.post(base + "/entities", json={"kind": "character", "name": "sample"}).json()
    vault = client.app.state.vault_registry.require(project["id"])
    statements = []

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        if "FROM search_documents" in statement or "JOIN search_documents" in statement:
            statements.append(statement)

    event.listen(vault.database.engine, "before_cursor_execute", record)
    try:
        with vault.database.session_scope() as session:
            assert not search(session, exclude_ids={entity["id"]})["items"]
    finally:
        event.remove(vault.database.engine, "before_cursor_execute", record)
    assert "NOT IN" in statements[0]
    assert "SELECT *" not in statements[0]


def test_empty_query_does_not_call_optional_embedding_provider(client, project):
    class Unnecessary:
        def search(self, query, limit):
            pytest.fail("An empty query must not call the model")

    import pytest

    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        search(session, vectors=Unnecessary())
