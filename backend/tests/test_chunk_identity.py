from job_helpers import run_job

from novel_harness.ai.demo import DemoProvider


def test_chunked_entities_keep_names_in_model_context(client, project, seeded_chapter):
    captured = []

    class Provider(DemoProvider):
        def generate_text(self, request):
            captured.append(str(request.context))
            return super().generate_text(request)

    client.app.state.ai_provider = Provider()
    base = f"/api/v1/projects/{project['id']}"
    for name in ["UniquePersonAlpha", "UniquePersonBeta"]:
        client.post(base + "/library/entity", json={"title": name, "content": "左眼受伤"})
    result = run_job(
        client,
        base + "/ai/jobs",
        json={
            "project_id": project["id"],
            "chapter_id": seeded_chapter,
            "task_type": "chat",
            "instructions": "Compare two characters",
        },
    ).json()
    assert result["status"] == "succeeded"
    assert all(name in captured[-1] for name in ["UniquePersonAlpha", "UniquePersonBeta"])


def test_unbuilt_migrated_index_cannot_silently_omit_references(client, project, seeded_chapter):
    from sqlalchemy import text

    base = f"/api/v1/projects/{project['id']}"
    client.post(base + "/library/entity", json={"title": "ExistingPerson", "content": "existing"})
    database = client.app.state.vault_registry.require(project["id"]).database
    with database.engine.begin() as connection:
        connection.execute(text("DROP TABLE search_chunks"))
        connection.execute(text("DROP TABLE search_chunk_fts"))
    database.create_schema()
    result = run_job(
        client,
        base + "/ai/jobs",
        json={
            "project_id": project["id"],
            "chapter_id": seeded_chapter,
            "task_type": "chat",
        },
    ).json()
    assert result["status"] == "recovery_required"
    assert result["recovery_reason"] == "RAG_REBUILD_REQUIRED"


def test_review_can_quote_actual_supplied_entity_title(client, project, seeded_chapter):
    base = f"/api/v1/projects/{project['id']}"
    entity = client.post(
        base + "/library/entity", json={"title": "UniquePersonAlpha", "content": "左眼受伤"}
    ).json()

    class Provider(DemoProvider):
        def generate_structured(self, request, schema):
            result = super().generate_structured(request, schema)
            if request.task == "style_review":
                result.data["issues"] = [
                    {
                        "code": "name",
                        "severity": "warning",
                        "message": "名字提示",
                        "evidence": ["UniquePersonAlpha"],
                        "related_entity_ids": [entity["id"]],
                    }
                ]
            return result

    client.app.state.ai_provider = Provider()
    result = run_job(
        client,
        base + "/ai/jobs",
        json={
            "project_id": project["id"],
            "chapter_id": seeded_chapter,
            "task_type": "full_chapter",
        },
    ).json()
    assert result["status"] == "succeeded"


def test_removed_search_candidate_returns_source_changed_not_server_error(
    client, project, monkeypatch
):
    from sqlalchemy import text

    from novel_harness.services import retrieval

    base = f"/api/v1/projects/{project['id']}"
    item = client.post(
        base + "/library/idea", json={"title": "NeedleCandidate", "content": "needle"}
    ).json()
    database = client.app.state.vault_registry.require(project["id"]).database
    original = retrieval.execute_candidates
    changed = False

    def execute(session, sql, params):
        nonlocal changed
        if sql.startswith("SELECT c.*,d.data") and not changed:
            changed = True
            with database.engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM search_chunks WHERE document_key=:key"),
                    {"key": "idea:" + item["id"]},
                )
        return original(session, sql, params)

    monkeypatch.setattr(retrieval, "execute_candidates", execute)
    response = client.get(base + "/library/search", params={"q": "needle"})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "SOURCE_CHANGED"


def test_long_source_cannot_crowd_out_other_matching_sources(client, project):
    base = f"/api/v1/projects/{project['id']}"
    first = client.post(
        base + "/library/idea", json={"title": "needle", "content": "ordinary " * 15000}
    ).json()
    second = client.post(
        base + "/library/idea",
        json={
            "title": "second",
            "content": "needle " + " ".join("filler" + str(i) for i in range(300)),
        },
    ).json()
    result = client.post(base + "/rag/search", json={"query": "needle", "limit": 30}).json()
    assert {first["id"], second["id"]} <= {item["id"] for item in result["items"]}
