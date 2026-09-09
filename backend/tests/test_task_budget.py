"""Read-only first-stage capacity reports share runtime request accounting."""

from dataclasses import asdict

import pytest
from sqlalchemy import event

from novel_harness.ai.base import ExecutionLimits, check_input_budget
from novel_harness.ai.demo import DemoProvider
from novel_harness.db.models import (
    CanonFact,
    ChapterDocument,
    Entity,
    Project,
    StoryNode,
    StyleProfile,
)
from novel_harness.services.context import estimate_tokens
from novel_harness.services.job_store import JobStore
from novel_harness.services.pipeline import (
    _STAGE_OUTPUTS,
    CreationPipeline,
    _compact_output_schema,
)
from novel_harness.services.writing_context import build_context, collect_task_hard_context


def prepared(client, project, chapter_id, task="continue", budget=12000):
    base = f"/api/v1/projects/{project['id']}"
    document = client.get(base + f"/chapters/{chapter_id}").json()
    saved = client.put(
        base + f"/chapters/{chapter_id}",
        json={
            "content": "风" * 7000,
            "contract": {},
            "revision": document["revision"],
        },
    ).json()
    assert (
        client.put(
            "/api/v1/settings/model",
            json={
                "mode": "demo",
                "context_capacity": 131072,
                "output_token_budget": 4096,
            },
        ).status_code
        == 200
    )
    command = dict(
        project_id=project["id"],
        chapter_id=chapter_id,
        task_type=task,
        instructions="继续当前故事",
        token_budget=budget,
        expected_revision=saved["revision"],
    )
    return base, command


def test_large_draft_reports_task_limit_and_raised_budget_runs(client, project, seeded_chapter):
    base, command = prepared(client, project, seeded_chapter)
    response = client.post(base + "/ai/jobs/preflight", json=command)
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["can_fit"] is False
    assert 12000 < report["required_input_tokens"] < 20000
    assert report["effective_input_limit"] == 12000
    assert report["estimated"] is True
    assert report["scope"] == "first-stage-hard-only"
    command["token_budget"] = 20000
    raised = client.post(base + "/ai/jobs/preflight", json=command).json()
    assert raised["can_fit"] is True
    assert raised["required_input_tokens"] == report["required_input_tokens"]
    receipt = client.post(base + "/ai/jobs", json=command, headers={"Idempotency-Key": "budget"})
    assert receipt.status_code == 202, receipt.text
    database = client.app.state.vault_registry.require(project["id"]).database
    store = JobStore(database, project["id"])
    CreationPipeline(None).run_durable(
        store, store.claim(receipt.json()["id"], "budget"), lambda observer: DemoProvider()
    )
    assert store.read(receipt.json()["id"])["status"] == "succeeded"
    with database.job_session_scope() as session:
        assert session.get(ChapterDocument, seeded_chapter).content == "风" * 7000


@pytest.mark.parametrize("task", ["chat", "continue", "rewrite", "review", "full_chapter", "plan"])
def test_preflight_matches_actual_first_stage_including_schema_and_prefix(
    client,
    project,
    seeded_chapter,
    task,
):
    base, command = prepared(client, project, seeded_chapter, task, 20000)
    response = client.post(base + "/ai/jobs/preflight", json=command)
    assert response.status_code == 200, response.text
    database = client.app.state.vault_registry.require(project["id"]).database
    with database.job_session_scope() as session:
        document = session.get(ChapterDocument, seeded_chapter)
        packet = build_context(
            session,
            session.get(Project, project["id"]),
            seeded_chapter,
            document.contract,
            20000,
            task,
            can_retrieve=lambda hard: False,
        )
        snapshot = {
            "token_budget": 20000,
            "execution_limits": ExecutionLimits(context_capacity=131072).model_dump(),
            "fragments": [asdict(f) for f in packet.fragments if f.hard],
        }
        request = CreationPipeline(None)._initial_request(
            task, command["instructions"], document.contract, snapshot
        )
        schema_type = _STAGE_OUTPUTS.get(request.task)
        tokens = check_input_budget(
            request,
            _compact_output_schema(schema_type.model_json_schema()) if schema_type else None,
        )
        assert response.json()["required_input_tokens"] == tokens
        fragments = request.context["context_packet"]["fragments"]
        assert all(f["source_type"] != "chapter_contract" for f in fragments)
        if task in {"chat", "continue", "rewrite", "review"}:
            assert (
                next(f["content"] for f in fragments if f["source_type"] == "current_draft")
                == "风" * 7000
            )


def test_capacity_reserves_output_and_report_discloses_no_private_content(
    client, project, seeded_chapter
):
    base, command = prepared(client, project, seeded_chapter, budget=20000)
    client.put(
        "/api/v1/settings/model",
        json={"mode": "demo", "context_capacity": 16000, "output_token_budget": 4096},
    )
    response = client.post(base + "/ai/jobs/preflight", json=command)
    assert response.status_code == 200, response.text
    assert response.json()["effective_input_limit"] == 11904
    assert response.json()["can_fit"] is False
    assert set(response.json()) == {
        "required_input_tokens",
        "token_budget",
        "output_token_budget",
        "context_capacity",
        "effective_input_limit",
        "can_fit",
        "estimated",
        "scope",
        "message",
    }
    assert "风" not in response.text and project["premise"] not in response.text


def test_preflight_never_retrieves_calls_provider_writes_or_cleans(
    client, project, seeded_chapter, monkeypatch
):
    base, command = prepared(client, project, seeded_chapter)

    def forbidden(*args, **kwargs):
        pytest.fail("preflight attempted side effect or retrieval")

    monkeypatch.setattr(DemoProvider, "generate_text", forbidden)
    monkeypatch.setattr(DemoProvider, "generate_structured", forbidden)
    monkeypatch.setattr("novel_harness.services.writing_context.search", forbidden)
    monkeypatch.setattr("novel_harness.services.library.collect_expired", forbidden)
    monkeypatch.setattr(JobStore, "enqueue", forbidden)
    monkeypatch.setattr("novel_harness.services.versions.get_or_create_document", forbidden)
    database = client.app.state.vault_registry.require(project["id"]).database
    monkeypatch.setattr(database, "drain_file_deletions", forbidden)

    class ForbiddenEmbedding:
        def embed(self, *args, **kwargs):
            forbidden()

    client.app.state.embedding_provider = ForbiddenEmbedding()
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(database.engine, "before_cursor_execute", record)
    try:
        response = client.post(base + "/ai/jobs/preflight", json=command)
    finally:
        event.remove(database.engine, "before_cursor_execute", record)
    assert response.status_code == 200, response.text
    assert not any(
        sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER"))
        for sql in statements
    )
    assert not any(
        "chapter_summaries" in sql.lower() or "search_documents" in sql.lower()
        for sql in statements
    )


def test_preflight_validates_revision_and_project_scope_without_creating_document(
    client, project, seeded_chapter
):
    base = f"/api/v1/projects/{project['id']}"
    command = dict(
        project_id=project["id"], chapter_id=seeded_chapter, task_type="chat", token_budget=12000
    )
    assert client.post(base + "/ai/jobs/preflight", json=command).status_code == 422
    command["expected_revision"] = 1
    # Node creation has no document yet: a made-up revision is not an observed revision.
    database = client.app.state.vault_registry.require(project["id"]).database
    with database.job_session_scope() as session:
        assert session.get(ChapterDocument, seeded_chapter) is None
    missing = client.post(base + "/ai/jobs/preflight", json=command)
    assert missing.status_code == 409
    with database.job_session_scope() as session:
        assert session.get(ChapterDocument, seeded_chapter) is None
    base, command = prepared(client, project, seeded_chapter)
    command["expected_revision"] += 1
    stale = client.post(base + "/ai/jobs/preflight", json=command)
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "SOURCE_CHANGED"
    other = client.post("/api/v1/projects", json={"title": "Other"}).json()
    command["project_id"] = other["id"]
    assert (
        client.post(f"/api/v1/projects/{other['id']}/ai/jobs/preflight", json=command).status_code
        == 404
    )
    command.update(chapter_id=None, expected_revision=None, task_type="chat")
    assert client.post(base + "/ai/jobs/preflight", json=command).status_code == 409
    command["project_id"] = project["id"]
    assert client.post(base + "/ai/jobs/preflight", json=command).json()["can_fit"] is True


def test_mandatory_author_material_is_included_and_future_canon_is_excluded(
    client, project, seeded_chapter
):
    base, command = prepared(client, project, seeded_chapter, "rewrite", 200000)
    baseline = client.post(base + "/ai/jobs/preflight", json=command).json()[
        "required_input_tokens"
    ]
    database = client.app.state.vault_registry.require(project["id"]).database
    canon_text, style_text, entity_text = "确认事实" * 200, "主动文风" * 200, "必需人物" * 200
    future_text = "未来秘密不得泄露" * 400
    with database.job_session_scope() as session:
        current = session.get(StoryNode, seeded_chapter)
        future = StoryNode(
            project_id=project["id"],
            kind="chapter",
            title="Future",
            parent_id=current.parent_id,
            order_index=99,
        )
        entity = Entity(
            project_id=project["id"], kind="character", name="Required", summary=entity_text
        )
        style = StyleProfile(
            project_id=project["id"],
            name="Active",
            is_active=True,
            is_pinned=True,
            config={"instruction": style_text},
        )
        session.add_all([future, entity, style])
        session.flush()
        fact = CanonFact(
            project_id=project["id"],
            predicate="known",
            value=canon_text,
            status="confirmed",
            is_pinned=True,
        )
        future_fact = CanonFact(
            project_id=project["id"],
            predicate="future",
            value=future_text,
            status="confirmed",
            is_pinned=True,
            valid_from_node_id=future.id,
        )
        session.add_all([fact, future_fact])
        session.flush()
        document = session.get(ChapterDocument, seeded_chapter)
        document.contract = {"required_entity_ids": [entity.id]}
        session.flush()
        hard, _, _ = collect_task_hard_context(
            session,
            session.get(Project, project["id"]),
            seeded_chapter,
            document.contract,
            "rewrite",
        )
        sources = {f.source_id: f for f in hard}
        assert sources[fact.id].hard and canon_text in sources[fact.id].content
        assert sources[style.id].hard and style_text in sources[style.id].content
        assert sources[entity.id].hard and entity_text in sources[entity.id].content
        assert future_fact.id not in sources
        assert any(
            f.source_type == "project_core" and project["premise"] in f.content for f in hard
        )
        future_id = future_fact.id
    result = client.post(base + "/ai/jobs/preflight", json=command)
    assert result.status_code == 200, result.text
    tokens = result.json()["required_input_tokens"]
    assert tokens - baseline > estimate_tokens(canon_text + style_text + entity_text)
    with database.job_session_scope() as session:
        session.get(CanonFact, future_id).value = future_text * 2
    assert (
        client.post(base + "/ai/jobs/preflight", json=command).json()["required_input_tokens"]
        == tokens
    )
