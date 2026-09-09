"""Candidate limits must never outrun eligibility or expand full source bodies."""

import pytest
from sqlalchemy import event, text

from novel_harness.db.models import CanonFact, Entity, Idea, StoryNode
from novel_harness.services.job_state import command_hash
from novel_harness.services.local_vectors import LocalVectorIndex
from novel_harness.services.retrieval import search


def vault_for(client, project):
    return client.app.state.vault_registry.require(project["id"])


def test_b10_exclusions_apply_before_fts_limit(client, project):
    vault = vault_for(client, project)
    with vault.database.session_scope() as session:
        excluded = [
            Idea(project_id=project["id"], title="needle", content="needle") for _ in range(100)
        ]
        survivor = Idea(project_id=project["id"], title="other", content="needle " + "filler " * 50)
        session.add_all(excluded + [survivor])
        session.flush()
        result = search(session, "needle", exclude_ids={item.id for item in excluded})
        assert [item["id"] for item in result["items"]] == [survivor.id]


def test_long_source_returns_local_chunk_and_full_source_hash(client, project):
    vault = vault_for(client, project)
    body = "平静的一天。" * 700 + "\nneedle unique passage\n" + "另一段日常。" * 700
    with vault.database.session_scope() as session:
        item = Idea(project_id=project["id"], title="长素材", content=body)
        session.add(item)
        session.flush()
        hit = search(session, "needle")["items"][0]
        assert "needle" in hit["content"]
        assert len(hit["content"]) <= 1200
        chunk = hit["chunk"]
        source_body = session.scalar(
            text("SELECT body FROM search_documents WHERE key=:key"), {"key": f"idea:{item.id}"}
        )
        assert source_body[chunk["start_offset"] : chunk["end_offset"]] == hit["content"]
        assert chunk["source_hash"] == command_hash(hit["record"])
        assert hit["record"]["content"] == body


def test_candidate_search_does_not_load_whole_library_json(client, project):
    vault = vault_for(client, project)
    with vault.database.session_scope() as session:
        session.add_all(
            [
                Idea(project_id=project["id"], title=f"unrelated{i}", content="quiet")
                for i in range(130)
            ]
        )
        session.add(Idea(project_id=project["id"], title="needle", content="target"))
        session.flush()
        statements = []

        def observe(_conn, _cursor, statement, _params, _context, _many):
            statements.append(statement.lower())

        event.listen(vault.database.engine, "before_cursor_execute", observe)
        try:
            assert len(search(session, "needle")["items"]) == 1
        finally:
            event.remove(vault.database.engine, "before_cursor_execute", observe)
        body_reads = [
            sql
            for sql in statements
            if "select" in sql and "data" in sql and "search_documents" in sql
        ]
        assert body_reads
        assert all("where" in sql and (" in (" in sql or "key=" in sql) for sql in body_reads)


def test_fact_eligibility_precedes_candidate_limit(client, project):
    vault = vault_for(client, project)
    with vault.database.session_scope() as session:
        now = StoryNode(project_id=project["id"], title="now", kind="chapter", order_index=1)
        future = StoryNode(project_id=project["id"], title="future", kind="chapter", order_index=2)
        session.add_all([now, future])
        session.flush()
        session.add_all(
            [
                CanonFact(
                    project_id=project["id"],
                    predicate="needle",
                    value="needle",
                    valid_from_node_id=future.id,
                )
                for _ in range(100)
            ]
        )
        valid = Idea(project_id=project["id"], title="eligible", content="needle " + "filler " * 50)
        session.add(valid)
        session.flush()
        hits = search(session, "needle", chapter_id=now.id)["items"]
        assert any(hit["id"] == valid.id and "fts" in hit["channels"] for hit in hits)
        assert not any(hit["type"] == "canon" for hit in hits)


class CountingEmbeddings:
    model = "test-model"

    def __init__(self):
        self.calls = []
        self.fail = False

    def embed(self, body):
        self.calls.append(body)
        if self.fail:
            raise ValueError("embedding unavailable")
        return [1.0, 0.0]


def test_embedding_endpoint_is_part_of_model_identity(client, project):
    vault = vault_for(client, project)
    first = CountingEmbeddings()
    first.endpoint = 'http://127.0.0.1:11434'
    with vault.database.session_scope() as session:
        session.add(Idea(project_id=project['id'], title='item', content='text'))
    with vault.database.session_scope() as session:
        LocalVectorIndex(vault.root, first).rebuild(session)
    other = CountingEmbeddings()
    other.endpoint = 'http://127.0.0.1:11435'
    index = LocalVectorIndex(vault.root, other)
    assert index.status() == 'needs_rebuild'
    with vault.database.session_scope() as session:
        index.rebuild(session)
    assert other.calls


def test_vector_rebuild_is_incremental_and_uses_shared_chunks(client, project):
    vault = vault_for(client, project)
    provider = CountingEmbeddings()
    vectors = LocalVectorIndex(vault.root, provider)
    with vault.database.session_scope() as session:
        session.add_all(
            [
                Idea(project_id=project["id"], title="a", content="short"),
                Idea(project_id=project["id"], title="b", content="long " * 600),
            ]
        )
    with vault.database.session_scope() as session:
        assert vectors.rebuild(session) == 2
        initial_calls = len(provider.calls)
        assert initial_calls > 2
        assert vectors.rebuild(session) == 2
        assert len(provider.calls) == initial_calls
        chunks = set(session.scalars(text("SELECT chunk_key FROM search_chunks")))
        hits = {key for key, _ in vectors.search_chunks("query", 100)}
        assert hits <= chunks
        assert len(hits) == 2
        assert {key.rsplit(':', 2)[0] for key in hits} == {key.rsplit(':', 2)[0] for key in chunks}
    base = f"/api/v1/projects/{project['id']}"
    item = client.get(base + "/library").json()[0]
    client.patch(
        base + f"/library/{item['type']}/{item['id']}",
        json={"revision": item["revision"], "content": "changed"},
    )
    with vault.database.session_scope() as session:
        before = len(provider.calls)
        vectors.rebuild(session)
        assert len(provider.calls) == before + 1
        assert vectors.status() == "ready"


def test_failed_vector_rebuild_preserves_prior_cache(client, project):
    vault = vault_for(client, project)
    provider = CountingEmbeddings()
    vectors = LocalVectorIndex(vault.root, provider)
    with vault.database.session_scope() as session:
        session.add(Idea(project_id=project["id"], title="first", content="original"))
    with vault.database.session_scope() as session:
        vectors.rebuild(session)
    with vectors.connect() as db:
        before = db.execute("SELECT * FROM vectors").fetchall()
    provider.fail = True
    with vault.database.session_scope() as session:
        session.add(Idea(project_id=project["id"], title="second", content="new"))
    with vault.database.session_scope() as session:
        with pytest.raises(ValueError, match="unavailable"):
            vectors.rebuild(session)
    with vectors.connect() as db:
        assert db.execute("SELECT * FROM vectors").fetchall() == before


def test_vector_eligibility_is_applied_before_candidate_limit(client, project):
    vault = vault_for(client, project)
    vectors = LocalVectorIndex(vault.root, CountingEmbeddings())
    with vault.database.session_scope() as session:
        excluded = [
            Idea(project_id=project["id"], title=f"excluded{i}", content="quiet")
            for i in range(100)
        ]
        valid = Idea(project_id=project["id"], title="eligible", content="quiet")
        session.add_all(excluded + [valid])
        session.flush()
        excluded_ids, valid_id = {item.id for item in excluded}, valid.id
    with vault.database.session_scope() as session:
        vectors.rebuild(session)
        result = search(session, "semantic-only-query", vectors=vectors, exclude_ids=excluded_ids)
        assert [item["id"] for item in result["items"]] == [valid_id]


def test_structural_channel_is_bounded_and_uses_chunk_identity(client, project):
    vault = vault_for(client, project)
    with vault.database.session_scope() as session:
        node = StoryNode(project_id=project["id"], title="now", kind="chapter")
        session.add(node)
        session.add_all(
            [
                Entity(
                    project_id=project["id"],
                    kind="character",
                    name=f"person{i}",
                    summary="bio " * 1000,
                )
                for i in range(130)
            ]
        )
        session.flush()
        result = search(session, chapter_id=node.id, limit=500)
        assert len(result["items"]) <= 30
        assert all(hit.get("chunk") and len(hit["content"]) <= 1200 for hit in result["items"])


def test_structure_and_pinning_do_not_replace_a_matching_later_chunk(client, project):
    vault = vault_for(client, project)
    with vault.database.session_scope() as session:
        node = StoryNode(project_id=project["id"], title="now", kind="chapter")
        entity = Entity(
            project_id=project["id"],
            kind="character",
            name="person",
            is_pinned=True,
            summary="ordinary " * 600 + "\nneedle special\n" + "ordinary " * 600,
        )
        session.add_all([node, entity])
        session.flush()
        hit = search(session, "needle", chapter_id=node.id)["items"][0]
        assert "needle" in hit["content"]
        assert "fts" in hit["channels"]
