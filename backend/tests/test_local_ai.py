import json
import sqlite3

import httpx
import pytest

from novel_harness.ai.base import AITextRequest, ProviderConfigurationError, ProviderExecutionError


def test_local_provider_rejects_remote_endpoints_and_cloud_models():
    from novel_harness.ai.local_provider import LocalProvider

    for endpoint in ["https://example.com", "http://192.168.1.3:11434", "http://localhost.evil:80"]:
        with pytest.raises(ProviderConfigurationError):
            LocalProvider(endpoint, "local-model")
    with pytest.raises(ProviderConfigurationError):
        LocalProvider("http://127.0.0.1:11434", "some-model:cloud")


def test_local_structured_request_is_stateless_and_validated():
    from novel_harness.ai.local_provider import LocalProvider

    calls = []

    def handle(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": '{"recap":"本章摘要"}'}})

    provider = LocalProvider(
        "http://127.0.0.1:11434", "local-model", transport=httpx.MockTransport(handle)
    )
    result = provider.generate_structured(
        AITextRequest(
            task="chapter_summary", developer_instruction="仅总结正文", user_prompt="章节内容"
        ),
        {"type": "object"},
    )
    assert result.data["recap"] == "本章摘要"
    assert calls[0]["stream"] is False
    assert calls[0]["format"] == {"type": "object"}
    assert len(calls[0]["messages"]) == 2


def test_local_provider_does_not_follow_redirects():
    from novel_harness.ai.local_provider import LocalProvider

    provider = LocalProvider(
        "http://127.0.0.1:11434",
        "local-model",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(307, headers={"Location": "https://example.com"})
        ),
    )
    with pytest.raises(ProviderExecutionError):
        provider.generate_text(
            AITextRequest(task="draft", developer_instruction="写作", user_prompt="正文")
        )


def test_optional_vector_index_is_project_local_and_stale_vectors_are_excluded(client, project):
    from novel_harness.services.local_vectors import LocalVectorIndex
    from novel_harness.services.retrieval import search

    class Embeddings:
        model = "test-local-embedding"

        def __init__(self):
            self.query_calls = 0

        def embed(self, text):
            self.query_calls += 1
            return [1.0, 0.0]

    vault = client.app.state.vault_registry.require(project["id"])
    base = f"/api/v1/projects/{project['id']}"
    item = client.post(base + "/library/entity", json={"title": "星河", "content": "航行者"}).json()
    embeddings = Embeddings()
    vectors = LocalVectorIndex(vault.root, embeddings)
    with vault.database.session_scope() as session:
        assert vectors.rebuild(session) == 1
        embeddings.query_calls = 0
        result = search(session, "离港", vectors=vectors)
        assert result["items"][0]["channels"] == ["vector"]
        assert result["vectors"] == "ready"
        assert embeddings.query_calls == 1
    client.patch(
        base + f"/library/entity/{item['id']}",
        json={"revision": item["revision"], "content": "新设定"},
    )
    with vault.database.session_scope() as session:
        embeddings.query_calls = 0
        result = search(session, "离港", vectors=vectors)
        assert result["items"] == []
        assert result["vectors"] == "needs_rebuild"
        assert embeddings.query_calls == 0
    other = client.post("/api/v1/projects", json={"title": "另一本小说"}).json()
    other_vault = client.app.state.vault_registry.require(other["id"])
    assert not (other_vault.root / "rag/vectors/vectors.db").exists()


def test_vector_failure_falls_back_to_fts(client, project):
    from novel_harness.services.retrieval import search

    class Broken:
        def status(self):
            return "ready"

        def search(self, query, limit):
            raise ProviderExecutionError("offline")

    client.post(f"/api/v1/projects/{project['id']}/library/entity", json={"title": "星河"})
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        result = search(session, "星河", vectors=Broken())
        assert result["items"]
        assert result["vectors"] == "degraded"


def test_vector_state_is_disabled_without_a_provider(client, project):
    from novel_harness.services.retrieval import search

    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        result = search(session, "anything")

    assert result["vectors"] == "disabled"


def test_vector_state_never_exposes_an_unknown_legacy_label(client, project):
    from novel_harness.services.retrieval import search

    class LegacyEnabled:
        def status(self):
            return "enabled"

        def search(self, query, limit):
            pytest.fail("An invalid adapter state must not enable vector search")

    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        result = search(session, "anything", vectors=LegacyEnabled())

    assert result["vectors"] == "degraded"


def test_non_durable_vector_status_failure_degrades_and_keeps_fts(client, project):
    from novel_harness.services.retrieval import search

    class BrokenStatus:
        def status(self):
            raise sqlite3.OperationalError("corrupt vector cache")

        def search(self, query, limit):
            pytest.fail("A failed readiness probe must not enable vector search")

    client.post(
        f"/api/v1/projects/{project['id']}/library/entity",
        json={"title": "状态失败仍可检索", "content": "STATUS_FAILURE_NEEDLE"},
    )
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        result = search(session, "STATUS_FAILURE_NEEDLE", vectors=BrokenStatus())

    assert result["vectors"] == "degraded"
    assert result["items"]
    assert "fts" in result["items"][0]["channels"]


def test_rag_health_reports_degraded_when_vector_status_probe_fails(
    client, project, monkeypatch
):
    from novel_harness.services.local_vectors import LocalVectorIndex

    class Embeddings:
        model = "broken-status-embedding"
        endpoint = "local"

    def fail_status(_self):
        raise sqlite3.OperationalError("corrupt vector cache")

    monkeypatch.setattr(LocalVectorIndex, "status", fail_status)
    client.app.state.embedding_provider = Embeddings()

    response = client.get(f"/api/v1/projects/{project['id']}/rag/health")

    assert response.status_code == 200
    assert response.json()["vectors"] == "degraded"


@pytest.mark.parametrize(
    ("outcome", "raises"),
    [("known", False), ("unknown", True)],
)
def test_durable_vector_provider_failure_preserves_outcome_semantics(
    client, project, outcome, raises
):
    from novel_harness.services.retrieval import search

    class Broken:
        def status(self):
            return "ready"

        def search(self, query, limit):
            raise ProviderExecutionError("offline", outcome=outcome)

    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        session.info["durable_retrieval"] = True
        if raises:
            with pytest.raises(ProviderExecutionError, match="offline"):
                search(session, "anything", vectors=Broken())
        else:
            assert search(session, "anything", vectors=Broken())["vectors"] == "degraded"


@pytest.mark.parametrize(
    ("failure", "raises"),
    [
        (ProviderExecutionError("known", outcome="known"), False),
        (ProviderExecutionError("unknown", outcome="unknown"), True),
        (sqlite3.OperationalError("corrupt vector cache"), True),
    ],
)
def test_durable_vector_status_failure_preserves_outcome_semantics(
    client, project, failure, raises
):
    from novel_harness.services.retrieval import search

    class BrokenStatus:
        def status(self):
            raise failure

        def search(self, query, limit):
            pytest.fail("A failed readiness probe must not enable vector search")

    client.post(
        f"/api/v1/projects/{project['id']}/library/entity",
        json={"title": "Durable fallback", "content": "DURABLE_STATUS_NEEDLE"},
    )
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        session.info["durable_retrieval"] = True
        if raises:
            with pytest.raises(type(failure), match=str(failure)):
                search(session, "DURABLE_STATUS_NEEDLE", vectors=BrokenStatus())
        else:
            result = search(session, "DURABLE_STATUS_NEEDLE", vectors=BrokenStatus())
            assert result["vectors"] == "degraded"
            assert result["items"]


def test_rag_health_and_search_share_vector_readiness(client, project):
    from novel_harness.services.local_vectors import LocalVectorIndex

    class Embeddings:
        model = "health-embedding"
        endpoint = "local"

        def __init__(self):
            self.calls = 0

        def embed(self, text):
            self.calls += 1
            return [1.0, 0.0]

    provider = Embeddings()
    client.app.state.embedding_provider = provider
    base = f"/api/v1/projects/{project['id']}"
    client.post(base + "/library/entity", json={"title": "状态针", "content": "HEALTH_NEEDLE"})

    health = client.get(base + "/rag/health").json()
    search = client.get(base + "/library/search", params={"q": "HEALTH_NEEDLE"}).json()
    assert health["vectors"] == search["vectors"] == "needs_rebuild"
    assert provider.calls == 0

    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        LocalVectorIndex(vault.root, provider).rebuild(session)

    assert client.get(base + "/rag/health").json()["vectors"] == "ready"
    assert client.get(base + "/library/search", params={"q": "semantic"}).json()[
        "vectors"
    ] == "ready"
