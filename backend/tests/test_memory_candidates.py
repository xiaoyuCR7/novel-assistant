import base64
import hashlib
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from importlib import import_module

import pytest
from fastapi import HTTPException
from job_helpers import complete_summary, run_job, save_chapter
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import event, func, inspect, select, update
from sqlalchemy.orm import Session

from novel_harness.ai.base import AITextRequest, ProviderExecutionError
from novel_harness.ai.demo import DemoProvider
from novel_harness.db.models import (
    AIJob,
    CanonFact,
    ChapterSummary,
    ChapterVersion,
    EntityState,
    GeneratedMemoryCandidate,
    MemoryCandidate,
    PlotThread,
    TimelineEvent,
)
from novel_harness.db.session import Database
from novel_harness.schemas.summaries import GeneratedSummary
from novel_harness.services import summary_generation


def _canon_proposal(quote="收起铜钥匙", start=1, end=6):
    return {
        "kind": "canon",
        "payload": {
            "subject_entity_id": None,
            "predicate": "owns",
            "value": "key",
            "valid_from_node_id": None,
            "valid_to_node_id": None,
        },
        "evidence": {"quote": quote, "start": start, "end": end},
    }


def _make_version(client, project_id, chapter_id, content="她收起铜钥匙"):
    chapter_url = f"/api/v1/projects/{project_id}/chapters/{chapter_id}"
    response = client.post(
        chapter_url + "/versions",
        json={
            "content": content,
            "expected_revision": client.get(chapter_url).json()["revision"],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _persist_candidate(client, project_id, chapter_id, proposal=None, content="她收起铜钥匙"):
    from novel_harness.services.memory_candidates import persist_candidates

    version = _make_version(client, project_id, chapter_id, content)
    database = client.app.state.vault_registry.require(project_id).database
    with database.session_scope() as session:
        row = persist_candidates(
            session,
            source_version=session.get(ChapterVersion, version["id"]),
            proposals=[proposal or _canon_proposal()],
        )[0]
        return row.id


def _candidate_url(project_id, candidate_id=""):
    suffix = f"/{candidate_id}" if candidate_id else ""
    return f"/api/v1/projects/{project_id}/memory-candidates{suffix}"


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


def _persist_same_version(client, project_id, chapter_id, proposals):
    from novel_harness.services.memory_candidates import persist_candidates

    version = _make_version(client, project_id, chapter_id)
    database = client.app.state.vault_registry.require(project_id).database
    ids = []
    with database.session_scope() as session:
        source = session.get(ChapterVersion, version["id"])
        for offset in range(0, len(proposals), 100):
            ids.extend(
                row.id
                for row in persist_candidates(
                    session,
                    source_version=source,
                    proposals=proposals[offset : offset + 100],
                )
            )
    return ids


def test_proposals_are_typed_strict_and_old_summary_output_stays_compatible():
    memory = import_module("novel_harness.schemas.memory")
    adapter = TypeAdapter(memory.MemoryCandidateProposal)

    parsed = adapter.validate_python(_canon_proposal())
    assert parsed.kind == "canon"
    with pytest.raises(ValidationError):
        adapter.validate_python(
            {
                **_canon_proposal(),
                "payload": {**_canon_proposal()["payload"], "unexpected": True},
            }
        )
    old = GeneratedSummary.model_validate(
        {
            "recap": "她收起铜钥匙",
            "plot_changes": [],
            "character_states": [],
            "knowledge_boundaries": [],
            "world_changes": [],
            "open_threads": [],
            "end_state": "",
            "fact_candidates": [],
            "content_findings": [],
            "content_observations": [],
            "evidence": [
                {"field": "recap", "index": 0, "quote": "她收起铜钥匙"}
            ],
        }
    )
    assert old.memory_candidates is None


def test_provider_schema_keeps_a_strict_candidate_envelope_and_payload_key_guide():
    from novel_harness.ai.prompts import (
        CHAPTER_SUMMARY_INSTRUCTION,
        CONTINUITY_INSTRUCTION,
    )

    field = GeneratedSummary.model_json_schema()["properties"]["memory_candidates"]
    envelope = field["anyOf"][0]["items"]
    assert envelope["additionalProperties"] is False
    assert envelope["required"] == ["kind", "payload", "evidence"]
    assert envelope["properties"]["kind"]["enum"] == [
        "canon",
        "entity_state",
        "timeline",
        "plot",
    ]
    assert envelope["properties"]["evidence"]["additionalProperties"] is False
    branches = envelope["properties"]["payload"]["oneOf"]
    assert len(branches) == 4
    for branch in branches:
        assert branch["additionalProperties"] is False
        assert branch["required"]
        assert set(branch["required"]).issubset(branch["properties"])
    for instruction in (CHAPTER_SUMMARY_INSTRUCTION, CONTINUITY_INSTRUCTION):
        assert "canon(subject_entity_id?,predicate,value" in instruction
        assert "entity_state(entity_id,data,valid_from_node_id" in instruction
        assert "evidence={quote,start,end}" in instruction


def test_persist_candidates_validates_exact_evidence_and_deduplicates(
    client, project, seeded_chapter
):
    candidate_service = import_module("novel_harness.services.memory_candidates")
    generated_model = GeneratedMemoryCandidate
    version_data = _make_version(client, project["id"], seeded_chapter)
    database = client.app.state.vault_registry.require(project["id"]).database

    with database.session_scope() as session:
        version = session.get(ChapterVersion, version_data["id"])
        first = candidate_service.persist_candidates(
            session, source_version=version, proposals=[_canon_proposal()]
        )
        second = candidate_service.persist_candidates(
            session, source_version=version, proposals=[_canon_proposal()]
        )
        assert [item.id for item in first] == [item.id for item in second]
        assert session.scalar(select(func.count()).select_from(generated_model)) == 1
        assert first[0].source_version_id == version.id
        assert first[0].status == "pending"


@pytest.mark.parametrize(
    ("proposal", "code"),
    [
        (_canon_proposal(quote="铜钥匙", start=0, end=3), "INVALID_MEMORY_EVIDENCE"),
        (
            {
                **_canon_proposal(),
                "payload": {
                    **_canon_proposal()["payload"],
                    "subject_entity_id": "missing-entity",
                },
            },
            "INVALID_MEMORY_CANDIDATE",
        ),
    ],
)
def test_invalid_candidate_leaves_no_row(
    client, project, seeded_chapter, proposal, code
):
    candidate_service = import_module("novel_harness.services.memory_candidates")
    generated_model = GeneratedMemoryCandidate
    version_data = _make_version(client, project["id"], seeded_chapter)
    database = client.app.state.vault_registry.require(project["id"]).database

    with pytest.raises(HTTPException) as caught:
        with database.session_scope() as session:
            candidate_service.persist_candidates(
                session,
                source_version=session.get(ChapterVersion, version_data["id"]),
                proposals=[proposal],
            )
    assert caught.value.detail["code"] == code
    with database.session_scope() as session:
        assert session.scalar(select(func.count()).select_from(generated_model)) == 0


def test_candidate_source_version_must_fall_inside_its_story_range(
    client, project, seeded_chapter
):
    candidate_service = import_module("novel_harness.services.memory_candidates")
    generated_model = GeneratedMemoryCandidate
    version_data = _make_version(client, project["id"], seeded_chapter)
    later = client.post(
        f"/api/v1/projects/{project['id']}/nodes",
        json={"kind": "chapter", "title": "第二章", "order_index": 2},
    ).json()
    proposal = _canon_proposal()
    proposal["payload"].update(
        valid_from_node_id=later["id"], valid_to_node_id=later["id"]
    )
    database = client.app.state.vault_registry.require(project["id"]).database

    with pytest.raises(HTTPException) as caught:
        with database.session_scope() as session:
            candidate_service.persist_candidates(
                session,
                source_version=session.get(ChapterVersion, version_data["id"]),
                proposals=[proposal],
            )
    assert caught.value.detail["code"] == "INVALID_MEMORY_CANDIDATE"
    with database.session_scope() as session:
        assert session.scalar(select(func.count()).select_from(generated_model)) == 0


def test_source_version_project_and_chapter_must_match_before_candidate_insert(
    client, project
):
    from novel_harness.db.models import Project, StoryNode
    from novel_harness.services.memory_candidates import persist_candidates

    database = client.app.state.vault_registry.require(project["id"]).database
    with pytest.raises(HTTPException) as caught:
        with database.session_scope() as session:
            foreign = Project(title="同库外部项目")
            session.add(foreign)
            session.flush()
            foreign_chapter = StoryNode(
                project_id=foreign.id,
                kind="chapter",
                title="外部章",
                order_index=1,
            )
            session.add(foreign_chapter)
            session.flush()
            corrupt = ChapterVersion(
                project_id=project["id"],
                chapter_id=foreign_chapter.id,
                content="她收起铜钥匙",
            )
            session.add(corrupt)
            session.flush()
            persist_candidates(
                session,
                source_version=corrupt,
                proposals=[
                    {
                        "kind": "timeline",
                        "payload": {
                            "chapter_id": None,
                            "title": "得到钥匙",
                            "story_time": "",
                            "sort_key": 0,
                            "description": "",
                        },
                        "evidence": {
                            "quote": "收起铜钥匙",
                            "start": 1,
                            "end": 6,
                        },
                    }
                ],
            )
    assert caught.value.detail["code"] == "INVALID_MEMORY_CANDIDATE"
    with database.session_scope() as session:
        assert session.scalar(select(func.count()).select_from(GeneratedMemoryCandidate)) == 0


def test_persist_uses_stored_version_content_instead_of_caller_object(
    client, project, seeded_chapter
):
    from novel_harness.services.memory_candidates import persist_candidates

    version_data = _make_version(client, project["id"], seeded_chapter)
    database = client.app.state.vault_registry.require(project["id"]).database
    with pytest.raises(HTTPException) as caught:
        with database.session_scope() as session:
            fake = ChapterVersion(
                id=version_data["id"],
                project_id=project["id"],
                chapter_id=seeded_chapter,
                content="伪证据",
            )
            proposal = _canon_proposal(quote="伪证据", start=0, end=3)
            persist_candidates(session, source_version=fake, proposals=[proposal])
    assert caught.value.detail["code"] == "INVALID_MEMORY_EVIDENCE"
    with database.session_scope() as session:
        assert session.scalar(select(func.count()).select_from(GeneratedMemoryCandidate)) == 0


def test_persist_refreshes_a_dirty_identity_mapped_version_before_validation(
    client, project, seeded_chapter
):
    from novel_harness.services.memory_candidates import persist_candidates

    version_data = _make_version(client, project["id"], seeded_chapter)
    database = client.app.state.vault_registry.require(project["id"]).database
    with pytest.raises(HTTPException) as caught:
        with database.session_scope() as session:
            dirty = session.get(ChapterVersion, version_data["id"])
            dirty.content = "伪证据"
            persist_candidates(
                session,
                source_version=dirty,
                proposals=[_canon_proposal(quote="伪证据", start=0, end=3)],
            )
    assert caught.value.detail["code"] == "INVALID_MEMORY_EVIDENCE"
    with database.session_scope() as session:
        assert session.scalar(select(func.count()).select_from(GeneratedMemoryCandidate)) == 0
        assert session.get(ChapterVersion, version_data["id"]).content == "她收起铜钥匙"


def test_persist_uses_stored_version_chapter_for_candidate_row(
    client, project, seeded_chapter
):
    from novel_harness.services.memory_candidates import persist_candidates

    version_data = _make_version(client, project["id"], seeded_chapter)
    later = client.post(
        f"/api/v1/projects/{project['id']}/nodes",
        json={"kind": "chapter", "title": "后续章", "order_index": 2},
    ).json()
    database = client.app.state.vault_registry.require(project["id"]).database
    with database.session_scope() as session:
        fake = ChapterVersion(
            id=version_data["id"],
            project_id=project["id"],
            chapter_id=later["id"],
            content="她收起铜钥匙",
        )
        row = persist_candidates(
            session, source_version=fake, proposals=[_canon_proposal()]
        )[0]
        assert row.chapter_id == seeded_chapter


def test_persist_loads_stored_summary_and_job_instead_of_trusting_caller_objects(
    client, project, seeded_chapter
):
    from novel_harness.services.memory_candidates import persist_candidates

    version_data = _make_version(client, project["id"], seeded_chapter)
    database = client.app.state.vault_registry.require(project["id"]).database
    with database.session_scope() as session:
        summary = ChapterSummary(
            project_id=project["id"],
            chapter_id=seeded_chapter,
            version_id=version_data["id"],
            title="第一章",
            content_hash="a" * 64,
            recap="她收起铜钥匙",
            details={},
        )
        job = AIJob(
            project_id=project["id"],
            chapter_id=seeded_chapter,
            task_type="full_chapter",
            prompt_version="test",
            status="succeeded",
        )
        session.add_all([summary, job])
        session.flush()
        summary_id, job_id = summary.id, job.id
    with database.session_scope() as session:
        fake_summary = ChapterSummary(
            id=summary_id,
            project_id="caller-lie",
            chapter_id="caller-lie",
            version_id="caller-lie",
            title="caller-lie",
            content_hash="b" * 64,
            recap="caller-lie",
            details={},
        )
        fake_job = AIJob(
            id=job_id,
            project_id="caller-lie",
            chapter_id="caller-lie",
            task_type="full_chapter",
            prompt_version="test",
        )
        row = persist_candidates(
            session,
            source_version=session.get(ChapterVersion, version_data["id"]),
            proposals=[_canon_proposal()],
            source_summary=fake_summary,
            source_job=fake_job,
        )[0]
        assert row.source_summary_id == summary_id
        assert row.source_job_id == job_id


@pytest.mark.parametrize("kind", ["timeline", "plot"])
def test_timeline_and_plot_candidates_cannot_date_source_into_another_chapter(
    client, project, seeded_chapter, kind
):
    from novel_harness.services.memory_candidates import persist_candidates

    version_data = _make_version(client, project["id"], seeded_chapter)
    later = client.post(
        f"/api/v1/projects/{project['id']}/nodes",
        json={"kind": "chapter", "title": "后续章", "order_index": 2},
    ).json()
    proposal = (
        {
            "kind": "timeline",
            "payload": {
                "chapter_id": later["id"],
                "title": "得到钥匙",
                "story_time": "",
                "sort_key": 0,
                "description": "",
            },
            "evidence": {"quote": "收起铜钥匙", "start": 1, "end": 6},
        }
        if kind == "timeline"
        else {
            "kind": "plot",
            "payload": {
                "kind": "mystery",
                "title": "钥匙伏笔",
                "promise": "",
                "start_node_id": later["id"],
                "due_node_id": later["id"],
                "payoff": "",
            },
            "evidence": {"quote": "收起铜钥匙", "start": 1, "end": 6},
        }
    )
    database = client.app.state.vault_registry.require(project["id"]).database
    with pytest.raises(HTTPException) as caught:
        with database.session_scope() as session:
            persist_candidates(
                session,
                source_version=session.get(ChapterVersion, version_data["id"]),
                proposals=[proposal],
            )
    assert caught.value.detail["code"] == "INVALID_MEMORY_CANDIDATE"
    with database.session_scope() as session:
        assert session.scalar(select(func.count()).select_from(GeneratedMemoryCandidate)) == 0


@pytest.mark.parametrize(
    "proposal",
    [
        {**_canon_proposal(), "evidence": {"quote": "收起铜钥匙", "start": "1", "end": 6}},
        {
            "kind": "timeline",
            "payload": {
                "chapter_id": None,
                "title": "得到钥匙",
                "story_time": "",
                "sort_key": "1",
                "description": "",
            },
            "evidence": {"quote": "收起铜钥匙", "start": 1, "end": 6},
        },
    ],
)
def test_candidate_schema_does_not_coerce_provider_scalar_types(proposal):
    memory = import_module("novel_harness.schemas.memory")
    with pytest.raises(ValidationError):
        TypeAdapter(memory.MemoryCandidateProposal).validate_python(proposal)


def _deep_json(depth):
    value = "leaf"
    for _ in range(depth):
        value = [value]
    return value


@pytest.mark.parametrize(
    "proposals",
    [
        [_canon_proposal() for _ in range(101)],
        [
            {
                **_canon_proposal(),
                "payload": {**_canon_proposal()["payload"], "value": "x" * 16_001},
            }
        ],
        [
            {
                **_canon_proposal(),
                "payload": {
                    **_canon_proposal()["payload"],
                    "value": {f"key-{index}": index for index in range(65)},
                },
            }
        ],
        [
            {
                **_canon_proposal(),
                "payload": {
                    **_canon_proposal()["payload"],
                    "value": _deep_json(300),
                },
            }
        ],
    ],
)
def test_invalid_candidate_resource_shapes_are_rejected_before_any_sql(
    client, project, seeded_chapter, proposals
):
    from novel_harness.services.memory_candidates import persist_candidates

    version_data = _make_version(client, project["id"], seeded_chapter)
    database = client.app.state.vault_registry.require(project["id"]).database
    session = Session(database.engine, expire_on_commit=False)
    try:
        version = session.get(ChapterVersion, version_data["id"])
        statements = []

        def record(_connection, _cursor, statement, *_args):
            statements.append(statement)

        event.listen(database.engine, "before_cursor_execute", record)
        try:
            with pytest.raises(HTTPException) as caught:
                persist_candidates(
                    session, source_version=version, proposals=proposals
                )
        finally:
            event.remove(database.engine, "before_cursor_execute", record)
        assert caught.value.detail["code"] == "INVALID_MEMORY_CANDIDATE"
        assert statements == []
    finally:
        session.rollback()
        session.close()


def test_database_snapshot_reads_do_not_refresh_or_flush_tracked_dirty_sources(
    client, project, seeded_chapter
):
    from novel_harness.services.memory_candidates import persist_candidates

    version_data = _make_version(client, project["id"], seeded_chapter)
    database = client.app.state.vault_registry.require(project["id"]).database
    session = Session(database.engine, expire_on_commit=False)
    try:
        version = session.get(ChapterVersion, version_data["id"])
        summary = ChapterSummary(
            project_id=project["id"],
            chapter_id=seeded_chapter,
            version_id=version.id,
            title="第一章",
            content_hash="a" * 64,
            recap="原总结",
            details={},
        )
        job = AIJob(
            project_id=project["id"],
            chapter_id=seeded_chapter,
            task_type="full_chapter",
            prompt_version="test",
            status="succeeded",
            instructions="原指令",
        )
        session.add_all([summary, job])
        session.flush()
        version.content = "caller dirty content"
        summary.recap = "caller dirty recap"
        job.instructions = "caller dirty instructions"

        row = persist_candidates(
            session,
            source_version=version,
            source_summary=summary,
            source_job=job,
            proposals=[_canon_proposal()],
        )[0]

        assert row.source_version_id == version_data["id"]
        assert version.content == "caller dirty content"
        assert summary.recap == "caller dirty recap"
        assert job.instructions == "caller dirty instructions"
        assert all(session.is_modified(item) for item in (version, summary, job))
    finally:
        session.rollback()
        session.close()


def test_memory_evidence_offsets_use_python_unicode_code_points(
    client, project, seeded_chapter
):
    from novel_harness.services.memory_candidates import persist_candidates

    text = "她握住🔑离开"
    version_data = _make_version(client, project["id"], seeded_chapter, text)
    proposal = _canon_proposal(quote="🔑", start=3, end=4)
    database = client.app.state.vault_registry.require(project["id"]).database
    with database.session_scope() as session:
        row = persist_candidates(
            session,
            source_version=session.get(ChapterVersion, version_data["id"]),
            proposals=[proposal],
        )[0]
        assert row.evidence == {"quote": "🔑", "start": 3, "end": 4}


def test_summary_validation_uses_candidate_offsets_not_summary_evidence_loop():
    source = "她收起铜钥匙"
    request = AITextRequest(
        task="chapter_summary", developer_instruction="", user_prompt=source
    )
    value = DemoProvider().generate_structured(request, {}).model_dump()
    value["data"]["memory_candidates"] = [_canon_proposal()]

    result = summary_generation.validate_result(value, source)
    assert result["data"]["memory_candidates"][0]["evidence"] == {
        "quote": "收起铜钥匙",
        "start": 1,
        "end": 6,
    }
    value["data"]["memory_candidates"][0]["evidence"]["start"] = 0
    with pytest.raises(ProviderExecutionError) as caught:
        summary_generation.validate_result(value, source)
    assert caught.value.code == "INVALID_MEMORY_EVIDENCE"


def test_final_review_candidate_evidence_is_checked_against_candidate_text():
    from novel_harness.services.review_validation import validate_review

    review = {
        "issues": [],
        "observations": [],
        "memory_candidates": [_canon_proposal()],
    }
    validate_review(review, "她收起铜钥匙", [], set())
    review["memory_candidates"][0]["evidence"]["end"] = 5
    with pytest.raises(ProviderExecutionError) as caught:
        validate_review(review, "她收起铜钥匙", [], set())
    assert caught.value.code == "INVALID_MEMORY_EVIDENCE"


def test_summary_publication_persists_once_and_links_summary(
    client, project, seeded_chapter
):
    generated_model = GeneratedMemoryCandidate

    class CandidateProvider(DemoProvider):
        def generate_structured(self, request, schema):
            result = super().generate_structured(request, schema)
            if request.task == "chapter_summary":
                quote = "收起铜钥匙"
                start = request.user_prompt.index(quote)
                result.data["memory_candidates"] = [
                    _canon_proposal(quote=quote, start=start, end=start + len(quote))
                ]
            return result

    chapter_url = f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}"
    assert save_chapter(client, chapter_url, json={"content": "她收起铜钥匙"}).status_code == 200
    client.app.state.ai_provider = CandidateProvider()
    summary = complete_summary(client, chapter_url).json()
    replayed = complete_summary(client, chapter_url).json()

    database = client.app.state.vault_registry.require(project["id"]).database
    with database.session_scope() as session:
        rows = list(session.scalars(select(generated_model)).all())
        assert len(rows) == 1
        assert rows[0].source_summary_id == summary["id"] == replayed["id"]


def test_writing_candidates_persist_only_after_accept_and_accept_replay_is_idempotent(
    client, project, seeded_chapter
):
    generated_model = GeneratedMemoryCandidate

    class CandidateProvider(DemoProvider):
        def generate_structured(self, request, schema):
            result = super().generate_structured(request, schema)
            if request.task == "continuity_review":
                quote = request.user_prompt[:6]
                result.data["memory_candidates"] = [
                    _canon_proposal(quote=quote, start=0, end=len(quote))
                ]
            return result

    client.app.state.ai_provider = CandidateProvider()
    job = run_job(
        client,
        f"/api/v1/projects/{project['id']}/ai/jobs",
        json={
            "project_id": project["id"],
            "chapter_id": seeded_chapter,
            "task_type": "full_chapter",
        },
    ).json()
    database = client.app.state.vault_registry.require(project["id"]).database
    with database.session_scope() as session:
        assert session.scalar(select(func.count()).select_from(generated_model)) == 0

    accept_url = f"/api/v1/projects/{project['id']}/ai/jobs/{job['id']}/accept"
    first = client.post(accept_url)
    second = client.post(accept_url)
    assert first.status_code == second.status_code == 201
    with database.session_scope() as session:
        rows = list(session.scalars(select(generated_model)).all())
        assert len(rows) == 1
        assert rows[0].source_job_id == job["id"]
        stored_job = session.get(import_module("novel_harness.db.models").AIJob, job["id"])
        assert stored_job.result["pending_canon_changes"][0]["id"] == rows[0].id


def test_candidate_api_lists_generated_rows_with_project_chapter_and_status_filters(
    client, project, seeded_chapter
):
    first_id = _persist_candidate(client, project["id"], seeded_chapter)
    second_proposal = _canon_proposal()
    second_proposal["payload"]["predicate"] = "carries"
    second_id = _persist_candidate(
        client, project["id"], seeded_chapter, second_proposal
    )
    rejected = client.post(
        _candidate_url(project["id"], second_id) + "/reject", json={"revision": 1}
    )
    assert rejected.status_code == 200, rejected.text
    later = client.post(
        f"/api/v1/projects/{project['id']}/nodes",
        json={"kind": "chapter", "title": "第二章", "order_index": 2},
    ).json()
    later_id = _persist_candidate(client, project["id"], later["id"])

    pending = client.get(
        _candidate_url(project["id"]),
        params={"chapter_id": seeded_chapter, "status": "pending"},
    )
    assert pending.status_code == 200, pending.text
    assert [row["id"] for row in pending.json()["items"]] == [first_id]
    assert pending.json()["next_cursor"] is None

    all_rows = client.get(_candidate_url(project["id"]))
    assert all_rows.status_code == 200, all_rows.text
    assert [row["id"] for row in all_rows.json()["items"]] == [
        first_id,
        second_id,
        later_id,
    ]

    another = client.post(
        "/api/v1/projects",
        json={"title": "隔离项目", "premise": "", "genre": "", "target_words": 1},
    ).json()
    cross_project = client.patch(
        _candidate_url(another["id"], first_id),
        json={"revision": 1, "payload": _canon_proposal()["payload"]},
    )
    assert cross_project.status_code == 404


def test_candidate_pages_are_bounded_complete_and_bind_cursor_to_filter_scope(
    client, project, seeded_chapter
):
    proposals = []
    for index in range(105):
        proposal = _canon_proposal()
        proposal["payload"]["predicate"] = f"owns-{index:03d}"
        proposals.append(proposal)
    chapter_ids = _persist_same_version(
        client, project["id"], seeded_chapter, proposals
    )
    database = client.app.state.vault_registry.require(project["id"]).database
    with database.session_scope() as session:
        session.execute(
            update(GeneratedMemoryCandidate)
            .where(GeneratedMemoryCandidate.id.in_(chapter_ids[-5:]))
            .values(status="rejected")
        )
    later = client.post(
        f"/api/v1/projects/{project['id']}/nodes",
        json={"kind": "chapter", "title": "分页外章节", "order_index": 3},
    ).json()
    foreign_chapter_ids = []
    for index in range(3):
        proposal = _canon_proposal()
        proposal["payload"]["predicate"] = f"later-{index}"
        foreign_chapter_ids.append(proposal)
    _persist_same_version(client, project["id"], later["id"], foreign_chapter_ids)

    too_large = client.get(_candidate_url(project["id"]), params={"limit": 101})
    assert too_large.status_code == 422

    params = {
        "chapter_id": seeded_chapter,
        "status": "pending",
        "limit": 40,
    }
    first = client.get(_candidate_url(project["id"]), params=params)
    assert first.status_code == 200, first.text
    assert len(first.json()["items"]) == 40
    cursor = first.json()["next_cursor"]
    assert cursor and all(row["status"] == "pending" for row in first.json()["items"])

    other = client.post("/api/v1/projects", json={"title": "游标外项目"}).json()
    deeply_nested = base64.urlsafe_b64encode(
        (("[" * 1100) + "0" + ("]" * 1100)).encode()
    ).decode()
    invalid_uses = [
        client.get(
            _candidate_url(project["id"]),
            params={**params, "status": "rejected", "cursor": cursor},
        ),
        client.get(
            _candidate_url(project["id"]),
            params={**params, "chapter_id": later["id"], "cursor": cursor},
        ),
        client.get(
            _candidate_url(other["id"]),
            params={"cursor": cursor},
        ),
        client.get(_candidate_url(project["id"]), params={"cursor": "invalid"}),
        client.get(_candidate_url(project["id"]), params={"cursor": "x" * 4097}),
        client.get(_candidate_url(project["id"]), params={"cursor": deeply_nested}),
    ]
    assert [response.status_code for response in invalid_uses] == [422] * 6
    assert {
        response.json()["detail"]["code"] for response in invalid_uses
    } == {"INVALID_CURSOR"}

    seen = []
    while cursor:
        response = client.get(
            _candidate_url(project["id"]), params={**params, "cursor": cursor}
        )
        assert response.status_code == 200, response.text
        seen.extend(row["id"] for row in response.json()["items"])
        cursor = response.json()["next_cursor"]
    first_ids = [row["id"] for row in first.json()["items"]]
    seen = first_ids + seen
    assert len(seen) == len(set(seen)) == 100
    assert set(seen) == set(chapter_ids[:-5])

    missing_page = client.get(
        _candidate_url(project["id"]), params={**params, "limit": 1}
    ).json()
    with database.session_scope() as session:
        session.delete(
            session.get(GeneratedMemoryCandidate, missing_page["items"][0]["id"])
        )
    missing = client.get(
        _candidate_url(project["id"]),
        params={**params, "cursor": missing_page["next_cursor"]},
    )
    assert missing.status_code == 422
    assert missing.json()["detail"]["code"] == "INVALID_CURSOR"


def test_candidate_cursor_survives_anchor_status_change(
    client, project, seeded_chapter
):
    proposals = []
    for index in range(41):
        proposal = _canon_proposal()
        proposal["payload"]["predicate"] = f"cursor-status-{index:03d}"
        proposals.append(proposal)
    _persist_same_version(client, project["id"], seeded_chapter, proposals)
    params = {
        "chapter_id": seeded_chapter,
        "status": "pending",
        "limit": 40,
    }

    first = client.get(_candidate_url(project["id"]), params=params)
    assert first.status_code == 200, first.text
    anchor = first.json()["items"][-1]
    cursor = first.json()["next_cursor"]
    assert cursor
    rejected = client.post(
        _candidate_url(project["id"], anchor["id"]) + "/reject",
        json={"revision": anchor["revision"]},
    )
    assert rejected.status_code == 200, rejected.text

    second = client.get(
        _candidate_url(project["id"]), params={**params, "cursor": cursor}
    )

    assert second.status_code == 200, second.text
    assert len(second.json()["items"]) == 1
    assert second.json()["next_cursor"] is None


def test_candidate_actions_and_chapter_filter_do_not_cross_project_boundaries(
    client, project, seeded_chapter
):
    candidate_id = _persist_candidate(client, project["id"], seeded_chapter)
    other = client.post("/api/v1/projects", json={"title": "边界外项目"}).json()
    other_chapter = client.post(
        f"/api/v1/projects/{other['id']}/nodes",
        json={"kind": "chapter", "title": "外部章", "order_index": 1},
    ).json()

    for action in ("confirm", "reject"):
        response = client.post(
            _candidate_url(other["id"], candidate_id) + f"/{action}",
            json={"revision": 1},
        )
        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "MEMORY_CANDIDATE_NOT_FOUND"

    filtered = client.get(
        _candidate_url(project["id"]),
        params={"chapter_id": other_chapter["id"]},
    )
    assert filtered.status_code == 422
    assert filtered.json()["detail"]["code"] == "INVALID_MEMORY_CANDIDATE"


def test_pending_candidate_edit_is_typed_revalidates_evidence_and_uses_revision_cas(
    client, project, seeded_chapter
):
    candidate_id = _persist_candidate(client, project["id"], seeded_chapter)
    url = _candidate_url(project["id"], candidate_id)

    invalid = client.patch(
        url,
        json={
            "revision": 1,
            "evidence": {"quote": "铜钥匙", "start": 0, "end": 3},
        },
    )
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == "INVALID_MEMORY_EVIDENCE"

    edited = client.patch(
        url,
        json={
            "revision": 1,
            "payload": {
                **_canon_proposal()["payload"],
                "predicate": "carries",
            },
        },
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["revision"] == 2
    assert edited.json()["payload"]["predicate"] == "carries"

    stale = client.patch(
        url,
        json={"revision": 1, "payload": _canon_proposal()["payload"]},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "revision_conflict"
    assert stale.json()["detail"]["current"]["revision"] == 2

    immutable_shape = client.patch(url, json={"revision": 2, "kind": "timeline"})
    assert immutable_shape.status_code == 422


def test_generated_candidate_patch_rejects_null_and_canonical_noop(
    client, project, seeded_chapter
):
    candidate_id = _persist_candidate(client, project["id"], seeded_chapter)
    url = _candidate_url(project["id"], candidate_id)
    for body in (
        {"revision": 1, "payload": None},
        {"revision": 1, "evidence": None},
        {"revision": 1, "payload": _canon_proposal()["payload"]},
    ):
        response = client.patch(url, json=body)
        assert response.status_code == 422
    database = client.app.state.vault_registry.require(project["id"]).database
    with database.session_scope() as session:
        candidate = session.get(GeneratedMemoryCandidate, candidate_id)
        assert candidate.revision == 1
        assert candidate.status == "pending"


def test_candidate_edit_duplicate_is_stable_and_does_not_overwrite_either_row(
    client, project, seeded_chapter
):
    from novel_harness.services.memory_candidates import persist_candidates

    second_proposal = _canon_proposal()
    second_proposal["payload"]["predicate"] = "carries"
    version = _make_version(client, project["id"], seeded_chapter)
    database = client.app.state.vault_registry.require(project["id"]).database
    with database.session_scope() as session:
        rows = persist_candidates(
            session,
            source_version=session.get(ChapterVersion, version["id"]),
            proposals=[_canon_proposal(), second_proposal],
        )
        first_id, second_id = (row.id for row in rows)

    duplicate = client.patch(
        _candidate_url(project["id"], second_id),
        json={"revision": 1, "payload": _canon_proposal()["payload"]},
    )
    assert duplicate.status_code == 409
    detail = duplicate.json()["detail"]
    assert detail["code"] == "MEMORY_CANDIDATE_DUPLICATE"
    assert detail["existing"]["id"] == first_id
    assert detail["current"]["id"] == second_id
    assert detail["current"]["revision"] == 1


def test_concurrent_candidate_edits_serialize_identity_conflict_without_500(
    client, project, seeded_chapter, monkeypatch
):
    from novel_harness.api.routes import memory as memory_routes

    proposals = []
    for predicate in ("owns-a", "owns-b"):
        proposal = _canon_proposal()
        proposal["payload"]["predicate"] = predicate
        proposals.append(proposal)
    first_id, second_id = _persist_same_version(
        client, project["id"], seeded_chapter, proposals
    )
    target = {**_canon_proposal()["payload"], "predicate": "shared-target"}
    winner_updated = threading.Event()
    release_winner = threading.Event()
    loser_lock_attempted = threading.Event()
    loser_worker = {}
    original = memory_routes.edit_candidate

    def coordinated_edit(session, project_id, candidate_id, revision, changes):
        if candidate_id == second_id:
            loser_worker["ident"] = threading.get_ident()
        result = original(session, project_id, candidate_id, revision, changes)
        if candidate_id == first_id:
            winner_updated.set()
            assert release_winner.wait(timeout=5)
        return result

    monkeypatch.setattr(memory_routes, "edit_candidate", coordinated_edit)
    database = client.app.state.vault_registry.require(project["id"]).database

    def observe(_connection, _cursor, statement, *_args):
        if (
            threading.get_ident() == loser_worker.get("ident")
            and statement.strip().upper().startswith("BEGIN IMMEDIATE")
        ):
            loser_lock_attempted.set()

    event.listen(database.engine, "before_cursor_execute", observe)
    try:
        def patch_in_thread(name, candidate_id):
            threading.current_thread().name = name
            return client.patch(
                _candidate_url(project["id"], candidate_id),
                json={"revision": 1, "payload": target},
            )

        with ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="candidate"
        ) as executor:
            winner = executor.submit(patch_in_thread, "candidate-winner", first_id)
            assert winner_updated.wait(timeout=5)
            loser = executor.submit(patch_in_thread, "candidate-loser", second_id)
            assert loser_lock_attempted.wait(timeout=5)
            release_winner.set()
            responses = [winner.result(timeout=5), loser.result(timeout=5)]
    finally:
        release_winner.set()
        event.remove(database.engine, "before_cursor_execute", observe)

    assert [response.status_code for response in responses] == [200, 409]
    assert responses[1].json()["detail"]["code"] == "MEMORY_CANDIDATE_DUPLICATE"
    with database.session_scope() as session:
        rows = list(
            session.scalars(
                select(GeneratedMemoryCandidate).where(
                    GeneratedMemoryCandidate.id.in_([first_id, second_id])
                )
            ).all()
        )
        assert sum(row.payload["predicate"] == "shared-target" for row in rows) == 1


@pytest.mark.parametrize(
    ("kind", "proposal", "model"),
    [
        ("canon", _canon_proposal(), CanonFact),
        (
            "timeline",
            {
                "kind": "timeline",
                "payload": {
                    "chapter_id": None,
                    "title": "得到钥匙",
                    "story_time": "雨夜",
                    "sort_key": 3,
                    "description": "她收起铜钥匙",
                },
                "evidence": {"quote": "收起铜钥匙", "start": 1, "end": 6},
            },
            TimelineEvent,
        ),
        (
            "plot",
            {
                "kind": "plot",
                "payload": {
                    "kind": "foreshadowing",
                    "title": "钥匙伏笔",
                    "promise": "钥匙会打开旧门",
                    "start_node_id": None,
                    "due_node_id": None,
                    "payoff": "",
                },
                "evidence": {"quote": "收起铜钥匙", "start": 1, "end": 6},
            },
            PlotThread,
        ),
    ],
)
def test_confirm_promotes_each_non_state_kind_once_and_replay_is_idempotent(
    client, project, seeded_chapter, kind, proposal, model
):
    candidate_id = _persist_candidate(
        client, project["id"], seeded_chapter, proposal
    )
    url = _candidate_url(project["id"], candidate_id) + "/confirm"
    confirmed = client.post(url, json={"revision": 1})
    assert confirmed.status_code == 200, confirmed.text
    body = confirmed.json()
    assert body["kind"] == kind
    assert body["status"] == "confirmed"
    assert body["revision"] == 2
    assert body["promoted_record_id"]

    replayed = client.post(url, json={"revision": 2})
    assert replayed.status_code == 200, replayed.text
    assert replayed.json() == body

    database = client.app.state.vault_registry.require(project["id"]).database
    with database.session_scope() as session:
        assert session.scalar(select(func.count()).select_from(model)) == 1
        promoted = session.get(model, body["promoted_record_id"])
        candidate = session.get(GeneratedMemoryCandidate, candidate_id)
        if kind == "canon":
            assert promoted.status == "confirmed"
            assert promoted.source_version_id == body["source_version_id"]
        elif kind == "plot":
            assert promoted.status == "active"
        if kind in {"timeline", "plot"}:
            assert candidate.source_version_id == body["source_version_id"]
            assert candidate.evidence == proposal["evidence"]
            assert candidate.promoted_record_id == promoted.id
            assert not hasattr(candidate, "deleted_at")
            assert not hasattr(candidate, "purge_after")


def test_generated_confirmed_replay_requires_existing_promotion(
    client, project, seeded_chapter
):
    candidate_id = _persist_candidate(client, project["id"], seeded_chapter)
    url = _candidate_url(project["id"], candidate_id) + "/confirm"
    confirmed = client.post(url, json={"revision": 1})
    assert confirmed.status_code == 200
    database = client.app.state.vault_registry.require(project["id"]).database
    with database.session_scope() as session:
        session.delete(session.get(CanonFact, confirmed.json()["promoted_record_id"]))
    replay = client.post(url, json={"revision": 2})
    assert replay.status_code == 409
    assert replay.json()["detail"]["code"] == "CANDIDATE_PROMOTION_MISSING"


def test_generated_replay_rejects_same_type_pointer_swap(
    client, project, seeded_chapter
):
    proposal = {
        "kind": "timeline",
        "payload": {
            "chapter_id": seeded_chapter,
            "title": "原时间线",
            "story_time": "雨夜",
            "sort_key": 1,
            "description": "原记录",
        },
        "evidence": {"quote": "收起铜钥匙", "start": 1, "end": 6},
    }
    candidate_id = _persist_candidate(
        client, project["id"], seeded_chapter, proposal
    )
    url = _candidate_url(project["id"], candidate_id) + "/confirm"
    confirmed = client.post(url, json={"revision": 1})
    assert confirmed.status_code == 200, confirmed.text
    replacement = client.post(
        f"/api/v1/projects/{project['id']}/timeline",
        json={"title": "替换时间线", "story_time": "晴天"},
    )
    assert replacement.status_code == 201
    database = client.app.state.vault_registry.require(project["id"]).database
    with database.session_scope() as session:
        candidate = session.get(GeneratedMemoryCandidate, candidate_id)
        candidate.promoted_record_id = replacement.json()["id"]
        candidate.promotion_fingerprint = None
    replay = client.post(url, json={"revision": 2})
    assert replay.status_code == 409
    assert replay.json()["detail"]["code"] == "CANDIDATE_PROMOTION_MISSING"


@pytest.mark.parametrize("kind", ["canon", "entity_state", "timeline", "plot"])
def test_generated_confirmed_replay_revalidates_stored_source_provenance(
    client, project, seeded_chapter, kind
):
    if kind == "canon":
        proposal = _canon_proposal()
    elif kind == "timeline":
        proposal = {
            "kind": "timeline",
            "payload": {
                "chapter_id": seeded_chapter,
                "title": "得到钥匙",
                "story_time": "雨夜",
                "sort_key": 3,
                "description": "她收起铜钥匙",
            },
            "evidence": {"quote": "收起铜钥匙", "start": 1, "end": 6},
        }
    elif kind == "plot":
        proposal = {
            "kind": "plot",
            "payload": {
                "kind": "foreshadowing",
                "title": "钥匙伏笔",
                "promise": "钥匙会打开旧门",
                "start_node_id": None,
                "due_node_id": None,
                "payoff": "",
            },
            "evidence": {"quote": "收起铜钥匙", "start": 1, "end": 6},
        }
    else:
        entity = client.post(
            f"/api/v1/projects/{project['id']}/entities",
            json={
                "kind": "character",
                "name": "阿雾",
                "state": {"owns": "key"},
            },
        ).json()
        proposal = {
            "kind": "entity_state",
            "payload": {
                "entity_id": entity["id"],
                "data": {"owns": "key"},
                "valid_from_node_id": seeded_chapter,
                "valid_to_node_id": None,
                "legacy_transition": "baseline",
            },
            "evidence": {"quote": "收起铜钥匙", "start": 1, "end": 6},
        }
    candidate_id = _persist_candidate(
        client, project["id"], seeded_chapter, proposal
    )
    url = _candidate_url(project["id"], candidate_id) + "/confirm"
    confirmed = client.post(url, json={"revision": 1})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["promotion_fingerprint"]
    client.app.state.vault_registry.require(project["id"]).database.dispose()
    lost_response_replay = client.post(url, json={"revision": 1})
    assert lost_response_replay.status_code == 200, lost_response_replay.text
    assert lost_response_replay.json() == confirmed.json()
    database = client.app.state.vault_registry.require(project["id"]).database
    with database.session_scope() as session:
        session.get(GeneratedMemoryCandidate, candidate_id).promotion_fingerprint = None
    legacy_replay = client.post(url, json={"revision": 2})
    assert legacy_replay.status_code == 200, legacy_replay.text
    assert legacy_replay.json()["promotion_fingerprint"]
    promoted_models = {
        "canon": CanonFact,
        "entity_state": EntityState,
        "timeline": TimelineEvent,
        "plot": PlotThread,
    }
    with database.session_scope() as session:
        candidate = session.get(GeneratedMemoryCandidate, candidate_id)
        model = promoted_models[kind]
        promoted = session.get(model, candidate.promoted_record_id)
        duplicate = _duplicate_promoted_record(session, model, promoted)
        duplicate_id = duplicate.id
        candidate.promotion_fingerprint = None
    ambiguous_original = client.post(url, json={"revision": 2})
    assert ambiguous_original.status_code == 409
    assert (
        ambiguous_original.json()["detail"]["code"]
        == "CANDIDATE_PROMOTION_MISSING"
    )
    with database.session_scope() as session:
        candidate = session.get(GeneratedMemoryCandidate, candidate_id)
        assert candidate.promotion_fingerprint is None
        candidate.promoted_record_id = duplicate_id
    ambiguous_swapped = client.post(url, json={"revision": 2})
    assert ambiguous_swapped.status_code == 409
    assert ambiguous_swapped.json()["detail"]["code"] == "CANDIDATE_PROMOTION_MISSING"
    with database.session_scope() as session:
        version = session.get(ChapterVersion, confirmed.json()["source_version_id"])
        version.content = "来源已被篡改"
    replay = client.post(url, json={"revision": 2})
    assert replay.status_code == 422
    assert replay.json()["detail"]["code"] == "INVALID_MEMORY_EVIDENCE"


def test_entity_state_confirmation_requires_author_supplied_legacy_transition(
    client, project, seeded_chapter
):
    entity = client.post(
        f"/api/v1/projects/{project['id']}/entities",
        json={
            "kind": "character",
            "name": "阿雾",
            "state": {"alive": True},
        },
    ).json()
    proposal = {
        "kind": "entity_state",
        "payload": {
            "entity_id": entity["id"],
            "data": {"alive": True},
            "valid_from_node_id": seeded_chapter,
            "valid_to_node_id": None,
        },
        "evidence": {"quote": "收起铜钥匙", "start": 1, "end": 6},
    }
    candidate_id = _persist_candidate(
        client, project["id"], seeded_chapter, proposal
    )
    base = _candidate_url(project["id"], candidate_id)

    blocked = client.post(base + "/confirm", json={"revision": 1})
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "LEGACY_STATE_TRANSITION_REQUIRED"

    payload = {**proposal["payload"], "legacy_transition": "baseline"}
    edited = client.patch(base, json={"revision": 1, "payload": payload})
    assert edited.status_code == 200, edited.text
    confirmed = client.post(base + "/confirm", json={"revision": 2})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "confirmed"
    replayed = client.post(base + "/confirm", json={"revision": 3})
    assert replayed.status_code == 200, replayed.text
    assert replayed.json() == confirmed.json()

    database = client.app.state.vault_registry.require(project["id"]).database
    with database.session_scope() as session:
        state = session.get(EntityState, confirmed.json()["promoted_record_id"])
        assert state.status == "confirmed"
        assert state.legacy_transition == "baseline"
        assert state.source_version_id == confirmed.json()["source_version_id"]
        assert session.scalar(select(func.count()).select_from(EntityState)) == 1


def test_reject_is_idempotent_at_current_revision_and_stale_revision_wins_first(
    client, project, seeded_chapter
):
    candidate_id = _persist_candidate(client, project["id"], seeded_chapter)
    base = _candidate_url(project["id"], candidate_id)
    rejected = client.post(base + "/reject", json={"revision": 1})
    assert rejected.status_code == 200, rejected.text
    body = rejected.json()
    assert body["status"] == "rejected"
    assert body["revision"] == 2

    stale = client.post(base + "/reject", json={"revision": 1})
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "revision_conflict"
    replayed = client.post(base + "/reject", json={"revision": 2})
    assert replayed.status_code == 200
    assert replayed.json() == body

    confirm_after_reject = client.post(base + "/confirm", json={"revision": 2})
    assert confirm_after_reject.status_code == 409
    assert confirm_after_reject.json()["detail"]["code"] == "MEMORY_CANDIDATE_REJECTED"


def test_reject_after_confirm_is_rejected_and_stale_revision_has_priority(
    client, project, seeded_chapter
):
    candidate_id = _persist_candidate(client, project["id"], seeded_chapter)
    base = _candidate_url(project["id"], candidate_id)
    assert client.post(base + "/confirm", json={"revision": 1}).status_code == 200

    stale = client.post(base + "/reject", json={"revision": 1})
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "revision_conflict"
    current = client.post(base + "/reject", json={"revision": 2})
    assert current.status_code == 409
    assert current.json()["detail"]["code"] == "MEMORY_CANDIDATE_CONFIRMED"


def test_confirmation_failure_rolls_back_authoritative_insert_and_candidate_state(
    client, project, seeded_chapter, monkeypatch
):
    from novel_harness.services import memory_candidates

    candidate_id = _persist_candidate(client, project["id"], seeded_chapter)
    original = memory_candidates.add_canon

    def insert_then_fail(session, project_id, values):
        original(session, project_id, values)
        raise HTTPException(422, detail={"code": "PROMOTION_FAILED"})

    monkeypatch.setattr(memory_candidates, "add_canon", insert_then_fail)
    response = client.post(
        _candidate_url(project["id"], candidate_id) + "/confirm",
        json={"revision": 1},
    )
    assert response.status_code == 422

    database = client.app.state.vault_registry.require(project["id"]).database
    with database.session_scope() as session:
        candidate = session.get(GeneratedMemoryCandidate, candidate_id)
        assert candidate.status == "pending"
        assert candidate.revision == 1
        assert candidate.promoted_record_id is None
        assert session.scalar(select(func.count()).select_from(CanonFact)) == 0


def test_old_vault_adds_generated_candidates_without_rewriting_import_candidates(tmp_path):
    database = Database(tmp_path / "old-vault.db")
    database.create_schema()
    generated_table = "generated_memory_candidates"
    with database.session_scope() as session:
        from novel_harness.db.models import ImportBatch, Project, SourceDocument, StoryNode

        project = Project(id="legacy-project", title="旧稿")
        session.add(project)
        session.flush()
        chapter = StoryNode(
            id="legacy-chapter",
            project_id=project.id,
            kind="chapter",
            title="旧章",
        )
        batch = ImportBatch(
            id="legacy-batch", project_id=project.id, source_kind="folder"
        )
        session.add_all([chapter, batch])
        session.flush()
        source = SourceDocument(
            id="legacy-source",
            project_id=project.id,
            import_batch_id=batch.id,
            chapter_id=chapter.id,
            relative_path="旧章.txt",
            stored_path="imports/旧章.txt",
            category="manuscript",
            title="旧章",
            content="旧正文",
            encoding="utf-8",
            byte_hash="a" * 64,
            content_hash=hashlib.sha256("旧正文".encode()).hexdigest(),
        )
        session.add(source)
        session.flush()
        session.add(
            MemoryCandidate(
                id="legacy-candidate",
                project_id=project.id,
                import_batch_id=batch.id,
                source_document_id=source.id,
                chapter_id=chapter.id,
                kind="chapter_summary",
                payload={"recap": "旧总结"},
                evidence=[{"quote": "旧正文"}],
                source_hash="c" * 64,
                dedupe_key="d" * 64,
            )
        )

    with database.engine.begin() as connection:
        assert generated_table in inspect(connection).get_table_names()
        connection.exec_driver_sql(
            "ALTER TABLE memory_candidates DROP COLUMN promotion_fingerprint"
        )
        connection.exec_driver_sql(f'DROP TABLE "{generated_table}"')
    database.create_schema()
    with database.engine.connect() as connection:
        for table in ("memory_candidates", "generated_memory_candidates"):
            columns = {
                row[1]
                for row in connection.exec_driver_sql(f'PRAGMA table_info("{table}")')
            }
            assert "promotion_fingerprint" in columns
    with database.session_scope() as session:
        legacy = session.get(MemoryCandidate, "legacy-candidate")
        assert legacy.payload == {"recap": "旧总结"}
    with closing(sqlite3.connect(database.path)) as connection, connection:
        assert generated_table in {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    database.dispose()
