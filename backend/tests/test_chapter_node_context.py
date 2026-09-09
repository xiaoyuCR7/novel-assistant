import json
from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from novel_harness.ai.demo import DemoProvider
from novel_harness.db.models import (
    AIJob,
    CanonFact,
    ChapterDocument,
    Entity,
    Project,
    StoryNode,
)
from novel_harness.services.job_context import (
    assert_source,
    capture_source,
    capture_summary_source,
    hard_context_hash,
)
from novel_harness.services.writing_context import build_context, collect_hard_context


def _vault(client, project):
    return client.app.state.vault_registry.require(project["id"])


def _entity(client, project, name):
    response = client.post(
        f"/api/v1/projects/{project['id']}/entities",
        json={"kind": "character", "name": name},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_current_chapter_node_and_late_pov_are_mandatory_hard_context(
    client, project, seeded_chapter
):
    vault = _vault(client, project)
    with vault.database.session_scope() as session:
        entities = [
            Entity(
                id=f"00000000-0000-0000-0000-{index:012d}",
                project_id=project["id"],
                kind="character",
                name=f"配角 {index:02d}",
                summary="COMMON_NEEDLE 可检索背景",
            )
            for index in range(34)
        ]
        pov = Entity(
            id="zzzzzzzz-zzzz-zzzz-zzzz-zzzzzzzzzzzz",
            project_id=project["id"],
            kind="character",
            name="最后的视角角色",
            summary="当前视角必须保留",
        )
        session.add_all([*entities, pov])
        session.flush()
        chapter = session.get(StoryNode, seeded_chapter)
        chapter.title = "密雨"
        chapter.summary = "UNIQUE_OUTLINE"
        chapter.target_words = 1800
        chapter.pov_entity_id = pov.id
        expected = {
            "id": chapter.id,
            "title": chapter.title,
            "summary": chapter.summary,
            "target_words": chapter.target_words,
            "pov_entity_id": chapter.pov_entity_id,
            "parent_id": chapter.parent_id,
            "order_index": chapter.order_index,
        }

    with vault.database.session_scope() as session:
        from novel_harness.services.retrieval import search

        retrieved_ids = {
            item["id"]
            for item in search(
                session,
                "COMMON_NEEDLE",
                chapter_id=seeded_chapter,
                limit=30,
            )["items"]
        }
        assert len(retrieved_ids) == 30
        assert pov.id not in retrieved_ids
        packet = build_context(
            session,
            session.get(Project, project["id"]),
            seeded_chapter,
            {},
            4_000,
            "draft",
            "COMMON_NEEDLE",
        )

    assert packet.over_budget is False
    chapter_fragment = next(
        fragment for fragment in packet.fragments if fragment.source_type == "chapter_node"
    )
    assert chapter_fragment.hard is True
    assert json.loads(chapter_fragment.content) == expected
    assert pov.id in {
        fragment.source_id for fragment in packet.fragments if fragment.hard
    }


def test_contract_and_node_pov_conflict_blocks_preflight_and_enqueue_before_provider(
    client, project, seeded_chapter
):
    calls = []

    class Recorder(DemoProvider):
        def generate_text(self, request):
            calls.append(request)
            return super().generate_text(request)

        def generate_structured(self, request, schema):
            calls.append(request)
            return super().generate_structured(request, schema)

    client.app.state.ai_provider = Recorder()
    node_pov = _entity(client, project, "节点视角")
    contract_pov = _entity(client, project, "契约视角")
    vault = _vault(client, project)
    with vault.database.session_scope() as session:
        session.get(StoryNode, seeded_chapter).pov_entity_id = node_pov["id"]

    chapter_url = f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}"
    current = client.get(chapter_url).json()
    saved = client.put(
        chapter_url,
        json={
            "content": current["content"],
            "contract": {"pov_entity_id": contract_pov["id"]},
            "revision": current["revision"],
        },
    )
    assert saved.status_code == 200, saved.text
    command = {
        "project_id": project["id"],
        "chapter_id": seeded_chapter,
        "task_type": "draft",
        "expected_revision": saved.json()["revision"],
    }
    base = f"/api/v1/projects/{project['id']}/ai/jobs"

    preflight = client.post(base + "/preflight", json=command)
    assert preflight.status_code == 409
    assert preflight.json()["detail"]["code"] == "CHAPTER_POV_CONFLICT"

    enqueue = client.post(
        base,
        json=command,
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert enqueue.status_code == 409
    assert enqueue.json()["detail"]["code"] == "CHAPTER_POV_CONFLICT"
    assert calls == []
    with vault.database.job_session_scope() as session:
        assert session.scalar(select(func.count(AIJob.id))) == 0


def test_invalid_persisted_contract_is_a_stable_domain_error_for_preflight_and_enqueue(
    client, project, seeded_chapter
):
    vault = _vault(client, project)
    chapter_url = f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}"
    current = client.get(chapter_url).json()
    with vault.database.job_session_scope() as session:
        valid_source = capture_source(session, project["id"], seeded_chapter)
    with vault.database.session_scope() as session:
        document = session.get(ChapterDocument, seeded_chapter)
        document.contract = {"required_entity_ids": "not-a-list"}

    with vault.database.job_session_scope() as session:
        with pytest.raises(HTTPException) as error:
            assert_source(session, valid_source)
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "INVALID_CHAPTER_CONTRACT"

    command = {
        "project_id": project["id"],
        "chapter_id": seeded_chapter,
        "task_type": "draft",
        "expected_revision": current["revision"],
    }
    base = f"/api/v1/projects/{project['id']}/ai/jobs"
    for response in (
        client.post(base + "/preflight", json=command),
        client.post(
            base,
            json=command,
            headers={"Idempotency-Key": uuid4().hex},
        ),
    ):
        assert response.status_code == 422
        assert response.json()["detail"] == {
            "code": "INVALID_CHAPTER_CONTRACT",
            "message": "章节创作契约格式无效，请修正后重试。",
        }
    with vault.database.job_session_scope() as session:
        assert session.scalar(select(func.count(AIJob.id))) == 0


@pytest.mark.parametrize(
    ("field", "new_value"),
    [
        ("title", "改名后的章节"),
        ("summary", "改写后的章节意图"),
        ("target_words", 4321),
        ("order_index", 17),
    ],
)
def test_chapter_node_scalar_semantic_change_invalidates_source(
    client, project, seeded_chapter, field, new_value
):
    vault = _vault(client, project)
    with vault.database.job_session_scope() as session:
        source = capture_source(session, project["id"], seeded_chapter)
    assert source["chapter_node_hash"]

    with vault.database.session_scope() as session:
        setattr(session.get(StoryNode, seeded_chapter), field, new_value)

    with vault.database.job_session_scope() as session:
        with pytest.raises(HTTPException) as error:
            assert_source(session, source)
    assert error.value.detail["code"] == "SOURCE_CHANGED"


@pytest.mark.parametrize("field", ["pov_entity_id", "parent_id"])
def test_chapter_node_reference_semantic_change_invalidates_source(
    client, project, seeded_chapter, field
):
    base = f"/api/v1/projects/{project['id']}"
    if field == "pov_entity_id":
        new_value = _entity(client, project, "新视角")["id"]
    else:
        response = client.post(
            base + "/nodes",
            json={"kind": "volume", "title": "新父节点", "order_index": 9},
        )
        assert response.status_code == 201, response.text
        new_value = response.json()["id"]

    vault = _vault(client, project)
    with vault.database.job_session_scope() as session:
        source = capture_source(session, project["id"], seeded_chapter)
    with vault.database.session_scope() as session:
        setattr(session.get(StoryNode, seeded_chapter), field, new_value)

    with vault.database.job_session_scope() as session:
        with pytest.raises(HTTPException) as error:
            assert_source(session, source)
    assert error.value.detail["code"] == "SOURCE_CHANGED"


def test_chapter_workflow_status_change_does_not_invalidate_source(
    client, project, seeded_chapter
):
    vault = _vault(client, project)
    with vault.database.job_session_scope() as session:
        source = capture_source(session, project["id"], seeded_chapter)
    with vault.database.session_scope() as session:
        session.get(StoryNode, seeded_chapter).status = "completed"
    with vault.database.job_session_scope() as session:
        assert_source(session, source)


def test_legacy_writing_source_without_chapter_node_hash_keeps_other_safety_checks(
    client, project, seeded_chapter
):
    vault = _vault(client, project)
    pov = _entity(client, project, "旧任务时未隐式引用的节点视角")
    client.get(f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}")
    with vault.database.session_scope() as session:
        session.get(StoryNode, seeded_chapter).pov_entity_id = pov["id"]
        session.get(ChapterDocument, seeded_chapter).contract = {
            "purpose": "legacy ordering",
            "custom": {"beta": 2, "alpha": 1},
        }
    with vault.database.job_session_scope() as session:
        legacy_source = capture_source(session, project["id"], seeded_chapter)
        hard, _ = collect_hard_context(
            session,
            session.get(Project, project["id"]),
            seeded_chapter,
            legacy_source["contract"],
            include_chapter_node_semantics=False,
        )
        assert list(legacy_source["contract"]["custom"]) == ["beta", "alpha"]
        hard = [
            replace(
                fragment,
                content=json.dumps(legacy_source["contract"], ensure_ascii=False),
            )
            if fragment.source_type == "chapter_contract"
            else fragment
            for fragment in hard
        ]
        legacy_source["hard_hash"] = hard_context_hash(
            hard
        )
    del legacy_source["chapter_node_hash"]

    with vault.database.job_session_scope() as session:
        assert_source(session, legacy_source)
    assert "chapter_node_hash" not in legacy_source

    with vault.database.session_scope() as session:
        session.add(
            CanonFact(
                project_id=project["id"],
                predicate="新增硬约束",
                value="不可违背",
                is_pinned=True,
            )
        )
    with vault.database.job_session_scope() as session:
        with pytest.raises(HTTPException) as error:
            assert_source(session, legacy_source)
    assert error.value.detail["code"] == "SOURCE_CHANGED"


def test_hard_context_json_is_canonical_across_equivalent_contract_key_order(
    client, project, seeded_chapter
):
    vault = _vault(client, project)
    contracts = (
        {
            "purpose": "推进调查",
            "custom": {"beta": 2, "alpha": 1},
        },
        {
            "custom": {"alpha": 1, "beta": 2},
            "purpose": "推进调查",
        },
    )
    with vault.database.job_session_scope() as session:
        project_row = session.get(Project, project["id"])
        first, _ = collect_hard_context(
            session, project_row, seeded_chapter, contracts[0]
        )
        second, _ = collect_hard_context(
            session, project_row, seeded_chapter, contracts[1]
        )

    assert contracts[0] == contracts[1]
    assert hard_context_hash(first) == hard_context_hash(second)
    for fragment in first + second:
        if fragment.source_type in {"chapter_contract", "chapter_node"}:
            assert fragment.content == json.dumps(
                json.loads(fragment.content), ensure_ascii=False, sort_keys=True
            )


def test_summary_source_and_project_scope_keep_their_existing_shape(
    client, project, seeded_chapter
):
    vault = _vault(client, project)
    with vault.database.job_session_scope() as session:
        summary_source = capture_summary_source(session, project["id"], seeded_chapter)
        project_source = capture_source(session, project["id"], None)

    assert "chapter_node_hash" not in summary_source
    assert project_source["chapter_node_hash"] is None
    assert not any(
        item["type"] == "chapter_node"
        for item in project_source.get("hard", [])
    )
