"""Validate and persist generated memory proposals without promoting them."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator
from sqlalchemy import and_, func, or_, select, text, tuple_, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from novel_harness.db.models import (
    AIJob,
    CanonFact,
    ChapterDocument,
    ChapterSummary,
    ChapterVersion,
    Entity,
    EntityRelation,
    EntityState,
    GeneratedMemoryCandidate,
    Idea,
    ImportBatch,
    MemoryCandidate,
    PlotThread,
    SourceDocument,
    StoryNode,
    StyleRule,
    TimelineEvent,
    new_id,
)
from novel_harness.schemas import story
from novel_harness.schemas.imports import BoundedJson, Evidence, Payload
from novel_harness.schemas.memory import MemoryCandidateList, MemoryCandidateProposal
from novel_harness.services.entity_states import create_entity_state
from novel_harness.services.import_drafts import normalize_relative_path
from novel_harness.services.job_state import command_hash
from novel_harness.services.projects import require_project
from novel_harness.services.retrieval import ordered_nodes
from novel_harness.services.serialization import serialize
from novel_harness.services.source_identity import source_document_identity_hash
from novel_harness.services.story import (
    add_canon,
    add_node,
    add_plot,
    add_record,
    add_relation,
    add_timeline,
)

_PROPOSALS = TypeAdapter(MemoryCandidateList)


@dataclass(frozen=True, slots=True)
class _VersionSnapshot:
    id: str
    project_id: str
    chapter_id: str
    content: str


@dataclass(frozen=True, slots=True)
class _SummarySnapshot:
    id: str
    project_id: str
    chapter_id: str
    version_id: str


@dataclass(frozen=True, slots=True)
class _JobSnapshot:
    id: str
    project_id: str
    chapter_id: str | None


def _invalid(code: str) -> HTTPException:
    return HTTPException(422, detail={"code": code})


def _active_node(session: Session, project_id: str, node_id: str | None) -> StoryNode | None:
    if node_id is None:
        return None
    node = session.get(StoryNode, node_id)
    if node is None or node.project_id != project_id or node.deleted_at is not None:
        raise _invalid("INVALID_MEMORY_CANDIDATE")
    return node


def _entity(session: Session, project_id: str, entity_id: str | None) -> Entity | None:
    if entity_id is None:
        return None
    entity = session.get(Entity, entity_id)
    if entity is None or entity.project_id != project_id or entity.deleted_at is not None:
        raise _invalid("INVALID_MEMORY_CANDIDATE")
    return entity


def _validate_range(
    session: Session,
    project_id: str,
    start_id: str | None,
    end_id: str | None,
) -> None:
    start = _active_node(session, project_id, start_id)
    end = _active_node(session, project_id, end_id)
    if start is None or end is None:
        return
    positions = {
        node.id: index
        for index, node in enumerate(ordered_nodes(session))
        if node.project_id == project_id and node.deleted_at is None
    }
    if positions[start.id] > positions[end.id]:
        raise _invalid("INVALID_MEMORY_CANDIDATE")


def _validate_source_in_range(
    session: Session,
    source_version: _VersionSnapshot,
    start_id: str | None,
    end_id: str | None,
) -> None:
    source = _active_node(session, source_version.project_id, source_version.chapter_id)
    start = _active_node(session, source_version.project_id, start_id)
    end = _active_node(session, source_version.project_id, end_id)
    positions = {
        node.id: index
        for index, node in enumerate(ordered_nodes(session))
        if node.project_id == source_version.project_id and node.deleted_at is None
    }
    if (start is not None and positions[source.id] < positions[start.id]) or (
        end is not None and positions[source.id] > positions[end.id]
    ):
        raise _invalid("INVALID_MEMORY_CANDIDATE")


def _validate_payload(
    session: Session, source_version: _VersionSnapshot, proposal: dict[str, Any]
) -> None:
    project_id = source_version.project_id
    payload = proposal["payload"]
    kind = proposal["kind"]
    if kind == "canon":
        _entity(session, project_id, payload["subject_entity_id"])
        _active_node(session, project_id, payload["valid_from_node_id"])
        _active_node(session, project_id, payload["valid_to_node_id"])
        _validate_range(
            session,
            project_id,
            payload["valid_from_node_id"],
            payload["valid_to_node_id"],
        )
        _validate_source_in_range(
            session,
            source_version,
            payload["valid_from_node_id"],
            payload["valid_to_node_id"],
        )
    elif kind == "entity_state":
        _entity(session, project_id, payload["entity_id"])
        _active_node(session, project_id, payload["valid_from_node_id"])
        _active_node(session, project_id, payload["valid_to_node_id"])
        _validate_range(
            session,
            project_id,
            payload["valid_from_node_id"],
            payload["valid_to_node_id"],
        )
        _validate_source_in_range(
            session,
            source_version,
            payload["valid_from_node_id"],
            payload["valid_to_node_id"],
        )
    elif kind == "timeline":
        chapter = _active_node(session, project_id, payload["chapter_id"])
        if chapter is not None and chapter.id != source_version.chapter_id:
            raise _invalid("INVALID_MEMORY_CANDIDATE")
    elif kind == "plot":
        _active_node(session, project_id, payload["start_node_id"])
        _active_node(session, project_id, payload["due_node_id"])
        _validate_range(
            session,
            project_id,
            payload["start_node_id"],
            payload["due_node_id"],
        )
        _validate_source_in_range(
            session,
            source_version,
            payload["start_node_id"],
            payload["due_node_id"],
        )


def _proposal_shapes(
    proposals: list[MemoryCandidateProposal | dict[str, Any]],
) -> list[dict[str, Any]]:
    try:
        parsed = _PROPOSALS.validate_python(proposals)
        return [proposal.model_dump(mode="json") for proposal in parsed]
    except (RecursionError, ValidationError, ValueError) as exc:
        raise _invalid("INVALID_MEMORY_CANDIDATE") from exc


def _validated_proposals(
    session: Session,
    source_version: _VersionSnapshot,
    proposals: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    for proposal in proposals:
        evidence = proposal["evidence"]
        if source_version.content[evidence["start"] : evidence["end"]] != evidence["quote"]:
            raise _invalid("INVALID_MEMORY_EVIDENCE")
        _validate_payload(session, source_version, proposal)
    return proposals


def _validate_sources(
    session: Session,
    source_version: ChapterVersion,
    source_summary: ChapterSummary | None,
    source_job: AIJob | None,
) -> tuple[_VersionSnapshot, _SummarySnapshot | None, _JobSnapshot | None]:
    version_table = ChapterVersion.__table__
    with session.no_autoflush:
        version_row = session.execute(
            select(
                version_table.c.id,
                version_table.c.project_id,
                version_table.c.chapter_id,
                version_table.c.content,
            ).where(version_table.c.id == source_version.id)
        ).mappings().one_or_none()
    if version_row is None:
        raise _invalid("INVALID_MEMORY_CANDIDATE")
    stored_version = _VersionSnapshot(**version_row)
    _active_node(session, stored_version.project_id, stored_version.chapter_id)
    summary_table = ChapterSummary.__table__
    with session.no_autoflush:
        summary_row = (
            session.execute(
                select(
                    summary_table.c.id,
                    summary_table.c.project_id,
                    summary_table.c.chapter_id,
                    summary_table.c.version_id,
                ).where(summary_table.c.id == source_summary.id)
            ).mappings().one_or_none()
            if source_summary is not None
            else None
        )
    stored_summary = _SummarySnapshot(**summary_row) if summary_row else None
    if source_summary is not None and (
        stored_summary is None
        or stored_summary.project_id != stored_version.project_id
        or stored_summary.chapter_id != stored_version.chapter_id
        or stored_summary.version_id != stored_version.id
    ):
        raise _invalid("INVALID_MEMORY_CANDIDATE")
    job_table = AIJob.__table__
    with session.no_autoflush:
        job_row = (
            session.execute(
                select(
                    job_table.c.id,
                    job_table.c.project_id,
                    job_table.c.chapter_id,
                ).where(job_table.c.id == source_job.id)
            ).mappings().one_or_none()
            if source_job is not None
            else None
        )
    stored_job = _JobSnapshot(**job_row) if job_row else None
    if source_job is not None and (
        stored_job is None
        or stored_job.project_id != stored_version.project_id
        or stored_job.chapter_id != stored_version.chapter_id
    ):
        raise _invalid("INVALID_MEMORY_CANDIDATE")
    return stored_version, stored_summary, stored_job


def persist_candidates(
    session: Session,
    *,
    source_version: ChapterVersion,
    proposals: list[MemoryCandidateProposal | dict[str, Any]],
    source_summary: ChapterSummary | None = None,
    source_job: AIJob | None = None,
) -> list[GeneratedMemoryCandidate]:
    """Persist validated pending proposals idempotently at an accepted boundary."""

    checked = _proposal_shapes(proposals)
    with session.no_autoflush:
        source_version, source_summary, source_job = _validate_sources(
            session, source_version, source_summary, source_job
        )
        checked = _validated_proposals(session, source_version, checked)
        rows = []
        table = GeneratedMemoryCandidate.__table__
        for proposal in checked:
            identity_hash = command_hash(
                {
                    "source_version_id": source_version.id,
                    "kind": proposal["kind"],
                    "payload": proposal["payload"],
                    "evidence": proposal["evidence"],
                }
            )
            candidate_id = new_id()
            session.execute(
                sqlite_insert(table)
                .values(
                    id=candidate_id,
                    project_id=source_version.project_id,
                    chapter_id=source_version.chapter_id,
                    source_job_id=source_job.id if source_job else None,
                    source_summary_id=source_summary.id if source_summary else None,
                    source_version_id=source_version.id,
                    kind=proposal["kind"],
                    payload=proposal["payload"],
                    evidence=proposal["evidence"],
                    identity_hash=identity_hash,
                    status="pending",
                    revision=1,
                )
                .on_conflict_do_nothing(
                    index_elements=[table.c.project_id, table.c.identity_hash]
                )
            )
            stored_id = session.scalar(
                select(table.c.id).where(
                    table.c.project_id == source_version.project_id,
                    table.c.identity_hash == identity_hash,
                )
            )
            candidate = session.get(GeneratedMemoryCandidate, stored_id)
            rows.append(candidate)
        return rows


def serialize_candidate(candidate: GeneratedMemoryCandidate) -> dict[str, Any]:
    return jsonable_encoder(serialize(candidate))


def _begin_write(session: Session) -> None:
    connection = session.connection().connection.driver_connection
    if not connection.in_transaction:
        session.execute(text("BEGIN IMMEDIATE"))
        session.expire_all()


def _require_candidate(
    session: Session, project_id: str, candidate_id: str
) -> GeneratedMemoryCandidate:
    require_project(session, project_id)
    candidate = session.get(GeneratedMemoryCandidate, candidate_id)
    if candidate is None or candidate.project_id != project_id:
        raise HTTPException(404, detail={"code": "MEMORY_CANDIDATE_NOT_FOUND"})
    return candidate


def _raise_revision_conflict(candidate: GeneratedMemoryCandidate) -> None:
    raise HTTPException(
        409,
        detail={
            "code": "revision_conflict",
            "current": serialize_candidate(candidate),
        },
    )


def _candidate_identity(
    source_version_id: str,
    kind: str,
    payload: dict[str, Any],
    evidence: dict[str, Any],
) -> str:
    return command_hash(
        {
            "source_version_id": source_version_id,
            "kind": kind,
            "payload": payload,
            "evidence": evidence,
        }
    )


def _validate_stored_candidate(
    session: Session,
    candidate: GeneratedMemoryCandidate,
    *,
    payload: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
) -> tuple[_VersionSnapshot, dict[str, Any], dict[str, Any]]:
    source_version = session.get(ChapterVersion, candidate.source_version_id)
    source_summary = (
        session.get(ChapterSummary, candidate.source_summary_id)
        if candidate.source_summary_id
        else None
    )
    source_job = (
        session.get(AIJob, candidate.source_job_id) if candidate.source_job_id else None
    )
    if (
        source_version is None
        or (candidate.source_summary_id and source_summary is None)
        or (candidate.source_job_id and source_job is None)
    ):
        raise _invalid("INVALID_MEMORY_CANDIDATE")
    stored_version, _, _ = _validate_sources(
        session, source_version, source_summary, source_job
    )
    if (
        stored_version.project_id != candidate.project_id
        or stored_version.chapter_id != candidate.chapter_id
    ):
        raise _invalid("INVALID_MEMORY_CANDIDATE")
    proposal = {
        "kind": candidate.kind,
        "payload": payload if payload is not None else candidate.payload,
        "evidence": evidence if evidence is not None else candidate.evidence,
    }
    shaped = _proposal_shapes([proposal])[0]
    _validated_proposals(session, stored_version, [shaped])
    return stored_version, shaped["payload"], shaped["evidence"]


def list_candidates(
    session: Session,
    project_id: str,
    *,
    chapter_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> dict[str, Any]:
    require_project(session, project_id)
    limit = min(100, max(1, limit))
    scope = [project_id, chapter_id, status]
    statement = select(GeneratedMemoryCandidate).where(
        GeneratedMemoryCandidate.project_id == project_id
    )
    if chapter_id is not None:
        _active_node(session, project_id, chapter_id)
        statement = statement.where(GeneratedMemoryCandidate.chapter_id == chapter_id)
    if status is not None:
        statement = statement.where(GeneratedMemoryCandidate.status == status)
    anchor = _decode_cursor(session, cursor, scope) if cursor else None
    if anchor is not None:
        statement = statement.where(
            tuple_(
                GeneratedMemoryCandidate.created_at,
                GeneratedMemoryCandidate.id,
            )
            > tuple_(anchor.created_at, anchor.id)
        )
    rows = list(
        session.scalars(
            statement.order_by(
                GeneratedMemoryCandidate.created_at,
                GeneratedMemoryCandidate.id,
            ).limit(limit + 1)
        ).all()
    )
    items = rows[:limit]
    next_cursor = _encode_cursor(scope, items[-1].id) if len(rows) > limit else None
    return {"items": items, "next_cursor": next_cursor}


def _invalid_cursor() -> HTTPException:
    return HTTPException(422, detail={"code": "INVALID_CURSOR"})


def _decode_cursor(
    session: Session, cursor: str, scope: list[Any]
) -> GeneratedMemoryCandidate:
    try:
        if len(cursor) > 4096:
            raise ValueError
        value = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        candidate_id = value["after"]
        if (
            value.get("v") != 1
            or value.get("scope") != scope
            or not isinstance(candidate_id, str)
            or not candidate_id
            or len(candidate_id) > 64
        ):
            raise ValueError
    except (
        ValueError,
        KeyError,
        TypeError,
        RecursionError,
        binascii.Error,
        UnicodeDecodeError,
    ) as exc:
        raise _invalid_cursor() from exc
    anchor = session.get(GeneratedMemoryCandidate, candidate_id)
    if (
        anchor is None
        or anchor.project_id != scope[0]
        or (scope[1] is not None and anchor.chapter_id != scope[1])
    ):
        raise _invalid_cursor()
    return anchor


def _encode_cursor(scope: list[Any], candidate_id: str) -> str:
    value = {"v": 1, "scope": scope, "after": candidate_id}
    return base64.urlsafe_b64encode(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    ).decode()


def edit_candidate(
    session: Session,
    project_id: str,
    candidate_id: str,
    revision: int,
    changes: dict[str, Any],
) -> GeneratedMemoryCandidate:
    _begin_write(session)
    candidate = _require_candidate(session, project_id, candidate_id)
    if candidate.revision != revision:
        _raise_revision_conflict(candidate)
    if candidate.status != "pending":
        raise HTTPException(409, detail={"code": "MEMORY_CANDIDATE_IMMUTABLE"})
    _, payload, evidence = _validate_stored_candidate(
        session,
        candidate,
        payload=changes.get("payload"),
        evidence=changes.get("evidence"),
    )
    if payload == candidate.payload and evidence == candidate.evidence:
        raise HTTPException(422, detail={"code": "NO_CANDIDATE_CHANGES"})
    identity_hash = _candidate_identity(
        candidate.source_version_id, candidate.kind, payload, evidence
    )
    duplicate = session.scalar(
        select(GeneratedMemoryCandidate)
        .where(
            GeneratedMemoryCandidate.project_id == project_id,
            GeneratedMemoryCandidate.identity_hash == identity_hash,
            GeneratedMemoryCandidate.id != candidate.id,
        )
        .order_by(
            GeneratedMemoryCandidate.created_at,
            GeneratedMemoryCandidate.id,
        )
        .limit(1)
    )
    if duplicate is not None:
        raise HTTPException(
            409,
            detail={
                "code": "MEMORY_CANDIDATE_DUPLICATE",
                "existing": serialize_candidate(duplicate),
                "current": serialize_candidate(candidate),
            },
        )
    result = session.execute(
        update(GeneratedMemoryCandidate)
        .where(
            GeneratedMemoryCandidate.id == candidate.id,
            GeneratedMemoryCandidate.project_id == project_id,
            GeneratedMemoryCandidate.revision == revision,
        )
        .values(
            payload=payload,
            evidence=evidence,
            identity_hash=identity_hash,
            revision=revision + 1,
        )
        .execution_options(synchronize_session=False)
    )
    session.expire_all()
    current = _require_candidate(session, project_id, candidate_id)
    if result.rowcount != 1:
        _raise_revision_conflict(current)
    return current


def reject_candidate(
    session: Session,
    project_id: str,
    candidate_id: str,
    revision: int,
) -> GeneratedMemoryCandidate:
    _begin_write(session)
    candidate = _require_candidate(session, project_id, candidate_id)
    if candidate.revision != revision:
        _raise_revision_conflict(candidate)
    if candidate.status == "rejected":
        return candidate
    if candidate.status == "confirmed":
        raise HTTPException(409, detail={"code": "MEMORY_CANDIDATE_CONFIRMED"})
    result = session.execute(
        update(GeneratedMemoryCandidate)
        .where(
            GeneratedMemoryCandidate.id == candidate.id,
            GeneratedMemoryCandidate.project_id == project_id,
            GeneratedMemoryCandidate.revision == revision,
        )
        .values(status="rejected", revision=revision + 1)
        .execution_options(synchronize_session=False)
    )
    session.expire_all()
    current = _require_candidate(session, project_id, candidate_id)
    if result.rowcount != 1:
        _raise_revision_conflict(current)
    return current


def _promote_candidate(
    session: Session,
    candidate: GeneratedMemoryCandidate,
    source_version: _VersionSnapshot,
    payload: dict[str, Any],
    evidence: dict[str, Any],
):
    if candidate.kind == "canon":
        return add_canon(
            session,
            candidate.project_id,
            {
                **payload,
                "source_note": evidence["quote"],
                "source_version_id": source_version.id,
                "status": "confirmed",
            },
        )
    if candidate.kind == "entity_state":
        values = {key: value for key, value in payload.items() if key != "entity_id"}
        return create_entity_state(
            session,
            candidate.project_id,
            payload["entity_id"],
            {
                **values,
                "source_version_id": source_version.id,
                "status": "confirmed",
            },
        )
    if candidate.kind == "timeline":
        return add_timeline(session, candidate.project_id, payload)
    if candidate.kind == "plot":
        return add_plot(
            session,
            candidate.project_id,
            {**payload, "status": "active"},
        )
    raise _invalid("INVALID_MEMORY_CANDIDATE")


def confirm_candidate(
    session: Session,
    project_id: str,
    candidate_id: str,
    revision: int,
) -> GeneratedMemoryCandidate:
    _begin_write(session)
    candidate = _require_candidate(session, project_id, candidate_id)
    if candidate.status == "confirmed" and revision in {
        candidate.revision,
        candidate.revision - 1,
    }:
        _validate_stored_candidate(session, candidate)
        _validate_generated_promotion(session, candidate)
        return candidate
    if candidate.revision != revision:
        _raise_revision_conflict(candidate)
    if candidate.status == "rejected":
        raise HTTPException(409, detail={"code": "MEMORY_CANDIDATE_REJECTED"})
    source_version, payload, evidence = _validate_stored_candidate(session, candidate)
    promoted = _promote_candidate(
        session, candidate, source_version, payload, evidence
    )
    promotion_fingerprint = _promotion_fingerprint(
        candidate,
        origin="generated",
        promoted_type=candidate.kind,
        promoted_record_id=promoted.id,
    )
    result = session.execute(
        update(GeneratedMemoryCandidate)
        .where(
            GeneratedMemoryCandidate.id == candidate.id,
            GeneratedMemoryCandidate.project_id == project_id,
            GeneratedMemoryCandidate.revision == revision,
            GeneratedMemoryCandidate.status == "pending",
        )
        .values(
            status="confirmed",
            revision=revision + 1,
            promoted_record_id=promoted.id,
            promotion_fingerprint=promotion_fingerprint,
        )
        .execution_options(synchronize_session=False)
    )
    session.expire_all()
    current = _require_candidate(session, project_id, candidate_id)
    if result.rowcount != 1:
        _raise_revision_conflict(current)
    return current


# Imported-source candidates deliberately share the author-facing endpoint with
# generated chapter candidates.  Their source/evidence contract is different,
# so keep the write path separate and merge only at the HTTP listing boundary.


class _ImportPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _EntityPayload(_ImportPayload):
    kind: str = Field(pattern="^(character|location|organization|item)$")
    name: str = Field(min_length=1, max_length=240)
    summary: str = Field(default="", max_length=16_000)
    profile: Payload = Field(default_factory=dict)
    state: Payload = Field(default_factory=dict)


class _RelationPayload(_ImportPayload):
    source_entity_name: str = Field(min_length=1, max_length=240)
    target_entity_name: str = Field(min_length=1, max_length=240)
    relation_type: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=16_000)


class _CanonPayload(_ImportPayload):
    subject_entity_id: str | None = Field(default=None, min_length=1, max_length=64)
    predicate: str = Field(min_length=1, max_length=120)
    value: BoundedJson
    source_note: str = Field(default="", max_length=16_000)
    valid_from_node_id: str | None = Field(default=None, min_length=1, max_length=64)
    valid_to_node_id: str | None = Field(default=None, min_length=1, max_length=64)
    status: str = Field(default="confirmed", pattern="^confirmed$")


class _TimelinePayload(_ImportPayload):
    title: str = Field(min_length=1, max_length=240)
    story_time: str = Field(default="", max_length=160)
    sort_key: int = Field(default=0, ge=-(2**63), le=2**63 - 1)
    chapter_id: str | None = Field(default=None, min_length=1, max_length=64)
    description: str = Field(default="", max_length=16_000)


class _PlotPayload(_ImportPayload):
    kind: str = Field(pattern="^(main|subplot|character|foreshadowing)$")
    title: str = Field(min_length=1, max_length=240)
    promise: str = Field(default="", max_length=16_000)
    status: str = Field(default="active", min_length=1, max_length=32)
    start_node_id: str | None = Field(default=None, min_length=1, max_length=64)
    due_node_id: str | None = Field(default=None, min_length=1, max_length=64)
    payoff: str = Field(default="", max_length=16_000)


class _NodePayload(_ImportPayload):
    kind: str = Field(pattern="^(volume|chapter|scene)$")
    parent_id: str | None = Field(default=None, min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=240)
    summary: str = Field(default="", max_length=16_000)
    order_index: int = Field(default=0, ge=0, le=2**31 - 1)
    target_words: int = Field(default=0, ge=0, le=2**31 - 1)
    status: str = Field(default="planned", min_length=1, max_length=32)
    pov_entity_id: str | None = Field(default=None, min_length=1, max_length=64)


class _NodeUpdatePayload(_ImportPayload):
    node_id: str = Field(min_length=1, max_length=64)
    expected_revision: int = Field(strict=True, ge=1)
    summary: str | None = Field(default=None, max_length=16_000)
    target_words: int | None = Field(default=None, ge=0, le=2**31 - 1)
    pov_entity_id: str | None = Field(default=None, min_length=1, max_length=64)
    parent_id: str | None = Field(default=None, min_length=1, max_length=64)
    order_index: int | None = Field(default=None, ge=0, le=2**31 - 1)

    @model_validator(mode="after")
    def has_changes(self):
        if not self.model_fields_set.intersection(
            {"summary", "target_words", "pov_entity_id", "parent_id", "order_index"}
        ):
            raise ValueError("node update requires an allowlisted change")
        return self


class _StyleRulePayload(_ImportPayload):
    style_profile_id: str | None = Field(default=None, min_length=1, max_length=64)
    rule_type: str = Field(default="preference", min_length=1, max_length=40)
    instruction: str = Field(min_length=1, max_length=16_000)
    weight: float = Field(default=1.0, ge=-1e308, le=1e308, allow_inf_nan=False)
    status: str = Field(default="confirmed", min_length=1, max_length=32)


class _IdeaPayload(_ImportPayload):
    title: str = Field(min_length=1, max_length=240)
    content: str = Field(default="", max_length=200_000)
    tags: list[str] = Field(default_factory=list, max_length=100)
    source: str = Field(default="import", max_length=240)
    status: str = Field(default="captured", max_length=32)


class _EntityStatePayload(_ImportPayload):
    entity_name: str = Field(min_length=1, max_length=240)
    data: Payload = Field(default_factory=dict)
    valid_from_node_id: str = Field(min_length=1, max_length=64)
    valid_to_node_id: str | None = Field(default=None, min_length=1, max_length=64)
    status: str = Field(default="confirmed", pattern="^confirmed$")
    legacy_transition: str | None = Field(default=None, pattern="^(baseline|retire)$")


class _ChapterSummaryPayload(_ImportPayload):
    chapter_id: str = Field(min_length=1, max_length=64)
    version_id: str = Field(min_length=1, max_length=64)
    content_hash: str = Field(min_length=64, max_length=64)
    title: str = Field(min_length=1, max_length=240)
    recap: str = Field(min_length=1, max_length=200_000)
    details: Payload = Field(default_factory=dict)


_IMPORT_PAYLOADS: dict[str, type[BaseModel]] = {
    "entity": _EntityPayload,
    "relation": _RelationPayload,
    "canon": _CanonPayload,
    "timeline": _TimelinePayload,
    "plot": _PlotPayload,
    "node": _NodePayload,
    "node_update": _NodeUpdatePayload,
    "style_rule": _StyleRulePayload,
    "idea": _IdeaPayload,
    "entity_state": _EntityStatePayload,
    "chapter_summary": _ChapterSummaryPayload,
}
_CREATE_SEPARATE_KINDS = frozenset(
    {"entity", "relation", "canon", "timeline", "plot", "node", "style_rule", "idea"}
)
_PROMOTED_MODELS: dict[str, type[Any]] = {
    "entity": Entity,
    "relation": EntityRelation,
    "canon": CanonFact,
    "timeline": TimelineEvent,
    "plot": PlotThread,
    "node": StoryNode,
    "node_update": StoryNode,
    "style_rule": StyleRule,
    "idea": Idea,
    "entity_state": EntityState,
    "chapter_summary": ChapterSummary,
}


class ImportCandidateConflict(RuntimeError):
    def __init__(self, current: MemoryCandidate):
        self.current = current


def _promotion_missing() -> None:
    raise HTTPException(409, detail={"code": "CANDIDATE_PROMOTION_MISSING"})


def _source_version_changed() -> None:
    raise HTTPException(409, detail={"code": "CANDIDATE_SOURCE_VERSION_CHANGED"})


def _promotion_fingerprint(
    candidate: GeneratedMemoryCandidate | MemoryCandidate,
    *,
    origin: str,
    promoted_type: str,
    promoted_record_id: str,
    candidate_identity: str | None = None,
) -> str:
    identity = (
        {"identity_hash": candidate_identity or candidate.identity_hash}
        if origin == "generated"
        else {"dedupe_key": candidate_identity or candidate.dedupe_key}
    )
    return command_hash(
        {
            "origin": origin,
            "candidate_id": candidate.id,
            "project_id": candidate.project_id,
            **identity,
            "promoted_type": promoted_type,
            "promoted_id": promoted_record_id,
        }
    )


def _validate_or_backfill_promotion_fingerprint(
    session: Session,
    candidate: GeneratedMemoryCandidate | MemoryCandidate,
    *,
    origin: str,
    promoted_type: str,
    legacy_binding_proven: bool,
) -> None:
    if not candidate.promoted_record_id:
        _promotion_missing()
    expected = _promotion_fingerprint(
        candidate,
        origin=origin,
        promoted_type=promoted_type,
        promoted_record_id=candidate.promoted_record_id,
    )
    if candidate.promotion_fingerprint is None:
        if not legacy_binding_proven:
            _promotion_missing()
        candidate.promotion_fingerprint = expected
        session.flush()
    elif candidate.promotion_fingerprint != expected:
        _promotion_missing()


def _validate_promoted_record(
    session: Session,
    *,
    project_id: str,
    kind: str,
    promoted_record_id: str | None,
):
    model = _PROMOTED_MODELS.get(kind)
    if model is None or not promoted_record_id:
        _promotion_missing()
    record = session.get(model, promoted_record_id)
    if (
        record is None
        or record.project_id != project_id
        or getattr(record, "deleted_at", None) is not None
    ):
        _promotion_missing()
    return record


def _record_fields_match(record, values: dict[str, Any]) -> bool:
    return all(getattr(record, key, object()) == value for key, value in values.items())


def _legacy_import_binding_matches(
    session: Session,
    candidate: MemoryCandidate,
    record,
    *,
    require_explicit_target: bool = True,
) -> bool:
    payload = candidate.payload
    kind = candidate.kind
    if kind == "entity":
        return _record_fields_match(record, payload)
    if kind == "relation":
        source = _exact_entity(session, candidate.project_id, payload["source_entity_name"])
        target = _exact_entity(session, candidate.project_id, payload["target_entity_name"])
        return _record_fields_match(
            record,
            {
                "source_entity_id": source.id,
                "target_entity_id": target.id,
                "relation_type": payload["relation_type"],
                "description": payload["description"],
            },
        )
    if kind == "canon":
        return _record_fields_match(
            record,
            {
                **payload,
                "source_version_id": candidate.source_version_id,
            },
        )
    if kind == "timeline":
        return _record_fields_match(record, payload)
    if kind == "plot":
        return _record_fields_match(record, payload)
    if kind == "node":
        return _record_fields_match(record, payload)
    if kind == "node_update":
        return (
            (not require_explicit_target or record.id == payload["node_id"])
            and record.revision == (payload["expected_revision"] + 1)
            and _record_fields_match(
                record,
                {
                    key: value
                    for key, value in payload.items()
                    if key not in {"node_id", "expected_revision"}
                    and value is not None
                },
            )
        )
    if kind == "style_rule":
        return _record_fields_match(record, payload)
    if kind == "idea":
        return _record_fields_match(record, payload)
    if kind == "entity_state":
        entity = _exact_entity(session, candidate.project_id, payload["entity_name"])
        return _record_fields_match(
            record,
            {
                **{key: value for key, value in payload.items() if key != "entity_name"},
                "entity_id": entity.id,
                "source_version_id": candidate.source_version_id,
            },
        )
    if kind == "chapter_summary":
        return _record_fields_match(
            record,
            {
                "chapter_id": payload["chapter_id"],
                "version_id": payload["version_id"],
                "content_hash": payload["content_hash"],
                "origin": "import_confirmed",
                "status": "valid",
            },
        )
    return False


def _legacy_generated_binding_matches(
    candidate: GeneratedMemoryCandidate, record
) -> bool:
    payload = candidate.payload
    if candidate.kind == "canon":
        return _record_fields_match(
            record,
            {
                **payload,
                "source_note": candidate.evidence["quote"],
                "source_version_id": candidate.source_version_id,
                "status": "confirmed",
            },
        )
    if candidate.kind == "entity_state":
        return _record_fields_match(
            record,
            {
                **payload,
                "source_version_id": candidate.source_version_id,
                "status": "confirmed",
            },
        )
    if candidate.kind == "timeline":
        return _record_fields_match(record, payload)
    if candidate.kind == "plot":
        return _record_fields_match(record, {**payload, "status": "active"})
    return False


def _legacy_promotion_binding_is_unique(
    session: Session,
    candidate: GeneratedMemoryCandidate | MemoryCandidate,
    *,
    origin: str,
) -> bool:
    model = _PROMOTED_MODELS.get(candidate.kind)
    if model is None or not candidate.promoted_record_id:
        return False
    statement = select(model).where(model.project_id == candidate.project_id)
    if hasattr(model, "deleted_at"):
        statement = statement.where(model.deleted_at.is_(None))
    records = session.scalars(statement).all()
    if origin == "import":
        matches = [
            record
            for record in records
            if _legacy_import_binding_matches(
                session,
                candidate,
                record,
                require_explicit_target=False,
            )
        ]
    else:
        matches = [
            record
            for record in records
            if _legacy_generated_binding_matches(candidate, record)
        ]
    return (
        len(matches) == 1
        and matches[0].id == candidate.promoted_record_id
    )


def _validate_import_promotion(
    session: Session, candidate: MemoryCandidate
) -> None:
    if candidate.promoted_type != candidate.kind:
        _promotion_missing()
    record = _validate_promoted_record(
        session,
        project_id=candidate.project_id,
        kind=candidate.kind,
        promoted_record_id=candidate.promoted_record_id,
    )
    payload = candidate.payload
    if candidate.kind == "node_update" and record.id != payload.get("node_id"):
        _promotion_missing()
    if candidate.kind == "canon" and (
        record.source_version_id != candidate.source_version_id
    ):
        _promotion_missing()
    if candidate.kind == "entity_state" and (
        record.source_version_id != candidate.source_version_id
    ):
        _promotion_missing()
    if candidate.kind == "chapter_summary" and (
        record.chapter_id != payload.get("chapter_id")
        or record.version_id != candidate.source_version_id
        or record.version_id != payload.get("version_id")
        or record.origin != "import_confirmed"
    ):
        _promotion_missing()
    _validate_or_backfill_promotion_fingerprint(
        session,
        candidate,
        origin="import",
        promoted_type=candidate.kind,
        legacy_binding_proven=(
            candidate.promotion_fingerprint is not None
            or _legacy_promotion_binding_is_unique(
                session, candidate, origin="import"
            )
        ),
    )


def _validate_generated_promotion(
    session: Session, candidate: GeneratedMemoryCandidate
) -> None:
    record = _validate_promoted_record(
        session,
        project_id=candidate.project_id,
        kind=candidate.kind,
        promoted_record_id=candidate.promoted_record_id,
    )
    if candidate.kind in {"canon", "entity_state"} and (
        record.source_version_id != candidate.source_version_id
    ):
        _promotion_missing()
    _validate_or_backfill_promotion_fingerprint(
        session,
        candidate,
        origin="generated",
        promoted_type=candidate.kind,
        legacy_binding_proven=(
            candidate.promotion_fingerprint is not None
            or _legacy_promotion_binding_is_unique(
                session, candidate, origin="generated"
            )
        ),
    )


def serialize_import_candidate(candidate: MemoryCandidate) -> dict[str, Any]:
    return {**jsonable_encoder(serialize(candidate)), "origin": "import"}


def _normalized_name(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def _import_revision_conflict(candidate: MemoryCandidate) -> None:
    raise HTTPException(
        409,
        detail={"code": "revision_conflict", "current": serialize_import_candidate(candidate)},
    )


def _require_import_candidate(
    session: Session, project_id: str, candidate_id: str
) -> MemoryCandidate:
    require_project(session, project_id)
    candidate = session.get(MemoryCandidate, candidate_id)
    if candidate is None or candidate.project_id != project_id:
        raise HTTPException(404, detail={"code": "MEMORY_CANDIDATE_NOT_FOUND"})
    return candidate


def candidate_origins(session: Session, project_id: str, candidate_id: str) -> set[str]:
    origins: set[str] = set()
    imported = session.get(MemoryCandidate, candidate_id)
    generated = session.get(GeneratedMemoryCandidate, candidate_id)
    if imported is not None and imported.project_id == project_id:
        origins.add("import")
    if generated is not None and generated.project_id == project_id:
        origins.add("generated")
    return origins


def _validate_import_payload(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    schema = _IMPORT_PAYLOADS.get(kind)
    if schema is None:
        raise _invalid("INVALID_MEMORY_CANDIDATE")
    try:
        checked = schema.model_validate(payload)
        return checked.model_dump(mode="json", exclude_unset=kind == "node_update")
    except (RecursionError, ValidationError, ValueError) as exc:
        raise _invalid("INVALID_MEMORY_CANDIDATE") from exc


def _validate_import_evidence(evidence: Any) -> list[dict[str, Any]]:
    try:
        checked = TypeAdapter(Evidence).validate_python(evidence)
    except (RecursionError, ValidationError, ValueError) as exc:
        raise _invalid("INVALID_MEMORY_EVIDENCE") from exc
    result: list[dict[str, Any]] = []
    for item in checked:
        if not {"quote", "start", "end"}.issubset(item) or set(item) - {
            "quote",
            "start",
            "end",
            "relative_path",
        }:
            raise _invalid("INVALID_MEMORY_EVIDENCE")
        quote, start, end = item["quote"], item["start"], item["end"]
        if (
            not isinstance(quote, str)
            or not quote
            or type(start) is not int
            or type(end) is not int
            or start < 0
            or end <= start
        ):
            raise _invalid("INVALID_MEMORY_EVIDENCE")
        normalized = {"quote": quote, "start": start, "end": end}
        if "relative_path" in item:
            path = item["relative_path"]
            if not isinstance(path, str):
                raise _invalid("INVALID_MEMORY_EVIDENCE")
            try:
                normalized["relative_path"] = normalize_relative_path(path)
            except HTTPException as exc:
                raise _invalid("INVALID_MEMORY_EVIDENCE") from exc
        result.append(normalized)
    if not result:
        raise _invalid("INVALID_MEMORY_EVIDENCE")
    return result


def import_candidate_dedupe_key(
    *,
    source_document_id: str,
    source_hash: str,
    kind: str,
    payload: dict[str, Any],
    evidence: list[dict[str, Any]],
) -> str:
    return command_hash(
        {
            "source_document_id": source_document_id,
            "source_hash": source_hash,
            "kind": kind,
            "payload": payload,
            "evidence": evidence,
        }
    )


def import_candidate_analysis_identity(kind: str, payload: dict[str, Any]) -> str:
    fields = {
        "entity": ("kind", "name"),
        "relation": ("source_entity_name", "target_entity_name", "relation_type"),
        "canon": ("subject_entity_id", "predicate"),
        "timeline": ("title", "story_time"),
        "plot": ("kind", "title"),
        "node": ("kind", "title"),
        "node_update": ("node_id",),
        "style_rule": ("instruction",),
        "idea": ("title",),
        "entity_state": ("entity_name", "valid_from_node_id"),
    }.get(kind, tuple(sorted(payload)))
    identity = [
        unicodedata.normalize("NFKC", str(payload.get(field, ""))).strip().casefold()
        for field in fields
    ]
    return command_hash({"kind": kind, "identity": identity})


def _validate_import_source_version(
    session: Session, candidate: MemoryCandidate, source: SourceDocument
) -> ChapterVersion | None:
    if source.deleted_at is not None or (
        source.chapter_id is not None and source.chapter_id != candidate.chapter_id
    ):
        _source_version_changed()
    if candidate.source_version_id is None:
        return None
    version = session.get(ChapterVersion, candidate.source_version_id)
    chapter = (
        session.get(StoryNode, candidate.chapter_id)
        if candidate.chapter_id is not None
        else None
    )
    if (
        version is None
        or version.project_id != candidate.project_id
        or version.chapter_id != candidate.chapter_id
        or version.source != "import"
        or chapter is None
        or chapter.project_id != candidate.project_id
        or chapter.deleted_at is not None
    ):
        _source_version_changed()
    return version


def _validated_import_candidate(
    session: Session,
    candidate: MemoryCandidate,
    *,
    kind: str | None = None,
    payload: dict[str, Any] | None = None,
    evidence: Any = None,
) -> tuple[SourceDocument, str, dict[str, Any], list[dict[str, Any]], str]:
    source = session.get(SourceDocument, candidate.source_document_id)
    batch = session.get(ImportBatch, candidate.import_batch_id)
    if (
        source is None
        or batch is None
        or source.project_id != candidate.project_id
        or source.import_batch_id != candidate.import_batch_id
        or batch.project_id != candidate.project_id
    ):
        raise HTTPException(409, detail={"code": "CANDIDATE_EVIDENCE_CHANGED"})
    _validate_import_source_version(session, candidate, source)
    try:
        current_source_hash = source_document_identity_hash(source)
    except ValueError as exc:
        raise HTTPException(
            409, detail={"code": "CANDIDATE_EVIDENCE_CHANGED"}
        ) from exc
    if candidate.source_hash != current_source_hash:
        raise HTTPException(409, detail={"code": "CANDIDATE_EVIDENCE_CHANGED"})
    checked_kind = kind if kind is not None else candidate.kind
    checked_payload = _validate_import_payload(
        checked_kind, payload if payload is not None else candidate.payload
    )
    try:
        checked_evidence = _validate_import_evidence(
            evidence if evidence is not None else candidate.evidence
        )
        source_path = normalize_relative_path(source.relative_path)
    except HTTPException as exc:
        if evidence is not None:
            raise
        raise HTTPException(
            409, detail={"code": "CANDIDATE_EVIDENCE_CHANGED"}
        ) from exc
    for item in checked_evidence:
        start, end = item["start"], item["end"]
        if (
            end > len(source.content)
            or source.content[start:end] != item["quote"]
            or item.get("relative_path", source_path) != source_path
        ):
            raise HTTPException(409, detail={"code": "CANDIDATE_EVIDENCE_CHANGED"})
        item["relative_path"] = source_path
    dedupe = import_candidate_dedupe_key(
        source_document_id=source.id,
        source_hash=current_source_hash,
        kind=checked_kind,
        payload=checked_payload,
        evidence=checked_evidence,
    )
    duplicate = session.scalar(
        select(MemoryCandidate).where(
            MemoryCandidate.project_id == candidate.project_id,
            MemoryCandidate.dedupe_key == dedupe,
            MemoryCandidate.id != candidate.id,
        )
    )
    if duplicate is not None:
        raise HTTPException(
            409,
            detail={
                "code": "MEMORY_CANDIDATE_DUPLICATE",
                "existing": serialize_import_candidate(duplicate),
            },
        )
    return source, checked_kind, checked_payload, checked_evidence, dedupe


def edit_import_candidate(
    session: Session,
    project_id: str,
    candidate_id: str,
    revision: int,
    changes: dict[str, Any],
) -> MemoryCandidate:
    _begin_write(session)
    candidate = _require_import_candidate(session, project_id, candidate_id)
    if candidate.revision != revision:
        _import_revision_conflict(candidate)
    if candidate.status not in {"pending", "conflict"}:
        raise HTTPException(409, detail={"code": "MEMORY_CANDIDATE_IMMUTABLE"})
    editable = {"kind", "payload", "evidence"}
    if not changes or set(changes) - editable:
        raise _invalid("INVALID_MEMORY_CANDIDATE_EDIT")
    _, current_kind, current_payload, current_evidence, _ = (
        _validated_import_candidate(session, candidate)
    )
    _, kind, payload, evidence, dedupe = _validated_import_candidate(
        session,
        candidate,
        kind=changes.get("kind"),
        payload=changes.get("payload"),
        evidence=changes.get("evidence") if "evidence" in changes else None,
    )
    if (kind, payload, evidence) == (
        current_kind,
        current_payload,
        current_evidence,
    ):
        raise HTTPException(422, detail={"code": "NO_CANDIDATE_CHANGES"})
    result = session.execute(
        update(MemoryCandidate)
        .where(
            MemoryCandidate.id == candidate.id,
            MemoryCandidate.project_id == project_id,
            MemoryCandidate.revision == revision,
            MemoryCandidate.status.in_(("pending", "conflict")),
        )
        .values(
            kind=kind,
            payload=payload,
            evidence=evidence,
            dedupe_key=dedupe,
            analysis_identity=import_candidate_analysis_identity(kind, payload),
            conflict={},
            status="pending",
            revision=revision + 1,
        )
        .execution_options(synchronize_session=False)
    )
    session.expire_all()
    current = _require_import_candidate(session, project_id, candidate_id)
    if result.rowcount != 1:
        _import_revision_conflict(current)
    return current


def reject_import_candidate(
    session: Session, project_id: str, candidate_id: str, revision: int
) -> MemoryCandidate:
    _begin_write(session)
    candidate = _require_import_candidate(session, project_id, candidate_id)
    if candidate.status == "rejected" and revision in {
        candidate.revision,
        candidate.revision - 1,
    }:
        return candidate
    if candidate.revision != revision:
        _import_revision_conflict(candidate)
    if candidate.status == "confirmed":
        raise HTTPException(409, detail={"code": "MEMORY_CANDIDATE_CONFIRMED"})
    result = session.execute(
        update(MemoryCandidate)
        .where(
            MemoryCandidate.id == candidate.id,
            MemoryCandidate.revision == revision,
            MemoryCandidate.status.in_(("pending", "conflict")),
        )
        .values(status="rejected", conflict={}, revision=revision + 1)
        .execution_options(synchronize_session=False)
    )
    session.expire_all()
    current = _require_import_candidate(session, project_id, candidate_id)
    if result.rowcount != 1:
        _import_revision_conflict(current)
    return current


def _exact_entity(session: Session, project_id: str, name: str) -> Entity:
    wanted = _normalized_name(name)
    matches = [
        entity
        for entity in session.scalars(
            select(Entity).where(
                Entity.project_id == project_id, Entity.deleted_at.is_(None)
            )
        )
        if _normalized_name(entity.name) == wanted
    ]
    if len(matches) != 1:
        raise HTTPException(
            409, detail={"code": "CANDIDATE_REFERENCE_AMBIGUOUS"}
        )
    return matches[0]


def _business_conflict(
    session: Session, candidate: MemoryCandidate, payload: dict[str, Any]
) -> dict[str, Any] | None:
    project_id = candidate.project_id
    kind = candidate.kind
    statement = None
    if kind == "entity":
        matches = [
            row.id
            for row in session.scalars(
                select(Entity).where(
                    Entity.project_id == project_id, Entity.deleted_at.is_(None)
                )
            )
            if _normalized_name(row.name) == _normalized_name(payload["name"])
        ]
        if matches:
            return {"code": "CANDIDATE_RECORD_CONFLICT", "record_ids": sorted(matches)}
    elif kind == "relation":
        source = _exact_entity(session, project_id, payload["source_entity_name"])
        target = _exact_entity(session, project_id, payload["target_entity_name"])
        statement = select(EntityRelation.id).where(
            EntityRelation.project_id == project_id,
            EntityRelation.source_entity_id == source.id,
            EntityRelation.target_entity_id == target.id,
            EntityRelation.relation_type == payload["relation_type"],
            EntityRelation.deleted_at.is_(None),
        )
    elif kind == "canon":
        statement = select(CanonFact.id).where(
            CanonFact.project_id == project_id,
            CanonFact.subject_entity_id == payload.get("subject_entity_id"),
            CanonFact.predicate == payload["predicate"],
            CanonFact.deleted_at.is_(None),
        )
    elif kind == "timeline":
        statement = select(TimelineEvent.id).where(
            TimelineEvent.project_id == project_id,
            TimelineEvent.title == payload["title"],
            TimelineEvent.story_time == payload["story_time"],
            TimelineEvent.deleted_at.is_(None),
        )
    elif kind == "plot":
        statement = select(PlotThread.id).where(
            PlotThread.project_id == project_id,
            PlotThread.kind == payload["kind"],
            PlotThread.title == payload["title"],
            PlotThread.deleted_at.is_(None),
        )
    elif kind == "node":
        statement = select(StoryNode.id).where(
            StoryNode.project_id == project_id,
            StoryNode.kind == payload["kind"],
            StoryNode.parent_id == payload.get("parent_id"),
            StoryNode.title == payload["title"],
            StoryNode.deleted_at.is_(None),
        )
    elif kind == "style_rule":
        statement = select(StyleRule.id).where(
            StyleRule.project_id == project_id,
            StyleRule.style_profile_id == payload.get("style_profile_id"),
            StyleRule.instruction == payload["instruction"],
            StyleRule.deleted_at.is_(None),
        )
    elif kind == "idea":
        statement = select(Idea.id).where(
            Idea.project_id == project_id,
            Idea.title == payload["title"],
            Idea.deleted_at.is_(None),
        )
    elif kind == "entity_state":
        entity = _exact_entity(session, project_id, payload["entity_name"])
        from novel_harness.services.entity_states import (
            candidate_entity_state_conflict,
        )

        state_values = {
            key: value for key, value in payload.items() if key != "entity_name"
        }
        conflict = candidate_entity_state_conflict(
            session, project_id, entity, state_values
        )
        if conflict is not None:
            return conflict
        statement = select(EntityState.id).where(
            EntityState.project_id == project_id,
            EntityState.entity_id == entity.id,
            EntityState.data == payload["data"],
            EntityState.valid_from_node_id == payload["valid_from_node_id"],
            EntityState.valid_to_node_id == payload.get("valid_to_node_id"),
            EntityState.status == "confirmed",
        )
    elif kind == "chapter_summary":
        statement = select(ChapterSummary.id).where(
            ChapterSummary.project_id == project_id,
            ChapterSummary.chapter_id == payload["chapter_id"],
            ChapterSummary.version_id == payload["version_id"],
            ChapterSummary.status == "valid",
            ChapterSummary.deleted_at.is_(None),
        )
    if statement is not None:
        matches = sorted(session.scalars(statement).all())
        if matches:
            return {"code": "CANDIDATE_RECORD_CONFLICT", "record_ids": matches}
    return None


def _promote_import_candidate(
    session: Session,
    candidate: MemoryCandidate,
    payload: dict[str, Any],
):
    project_id = candidate.project_id
    if candidate.kind == "entity":
        checked = story.EntityCreate.model_validate(payload).model_dump()
        return add_record(session, project_id, Entity, checked)
    if candidate.kind == "relation":
        source = _exact_entity(session, project_id, payload["source_entity_name"])
        target = _exact_entity(session, project_id, payload["target_entity_name"])
        checked = story.RelationCreate.model_validate(
            {
                "source_entity_id": source.id,
                "target_entity_id": target.id,
                "relation_type": payload["relation_type"],
                "description": payload["description"],
            }
        ).model_dump()
        return add_relation(session, project_id, checked)
    if candidate.kind == "canon":
        checked = story.CanonFactCreate.model_validate(
            {**payload, "source_version_id": candidate.source_version_id}
        ).model_dump()
        return add_canon(session, project_id, checked)
    if candidate.kind == "timeline":
        checked = story.TimelineEventCreate.model_validate(payload).model_dump()
        return add_timeline(session, project_id, checked)
    if candidate.kind == "plot":
        checked = story.PlotThreadCreate.model_validate(payload).model_dump()
        return add_plot(session, project_id, checked)
    if candidate.kind == "node":
        checked = story.NodeCreate.model_validate(payload).model_dump()
        return add_node(session, project_id, checked)
    if candidate.kind == "style_rule":
        if payload.get("style_profile_id"):
            from novel_harness.db.models import StyleProfile
            from novel_harness.services.story import _require_same_project

            _require_same_project(
                session, StyleProfile, payload["style_profile_id"], project_id
            )
        return add_record(session, project_id, StyleRule, payload)
    if candidate.kind == "idea":
        checked = story.IdeaCreate.model_validate(payload).model_dump()
        return add_record(session, project_id, Idea, checked)
    if candidate.kind == "entity_state":
        entity = _exact_entity(session, project_id, payload["entity_name"])
        values = {key: value for key, value in payload.items() if key != "entity_name"}
        values["source_version_id"] = candidate.source_version_id
        return create_entity_state(session, project_id, entity.id, values)
    if candidate.kind == "node_update":
        return _promote_node_update(session, candidate, payload)
    if candidate.kind == "chapter_summary":
        return _promote_chapter_summary(session, candidate, payload)
    raise _invalid("INVALID_MEMORY_CANDIDATE")


def _promote_node_update(
    session: Session, candidate: MemoryCandidate, payload: dict[str, Any]
) -> StoryNode:
    from novel_harness.services.material_fields import validate_fields
    from novel_harness.services.versions import begin_version_write

    node = session.get(StoryNode, payload["node_id"])
    imported = session.scalar(
        select(ChapterVersion.id).where(
            ChapterVersion.project_id == candidate.project_id,
            ChapterVersion.chapter_id == payload["node_id"],
            ChapterVersion.source == "import",
        )
    )
    if (
        node is None
        or node.project_id != candidate.project_id
        or node.deleted_at is not None
        or imported is None
    ):
        raise _invalid("CANDIDATE_NODE_NOT_IMPORTED")
    if node.revision != payload["expected_revision"]:
        raise HTTPException(
            409,
            detail={"code": "revision_conflict", "current": jsonable_encoder(serialize(node))},
        )
    fields = {
        key: value
        for key, value in payload.items()
        if key not in {"node_id", "expected_revision"}
    }
    checked = validate_fields(session, "node", node, fields, {})
    begin_version_write(session)
    result = session.execute(
        update(StoryNode)
        .where(
            StoryNode.id == node.id,
            StoryNode.project_id == candidate.project_id,
            StoryNode.revision == payload["expected_revision"],
            StoryNode.deleted_at.is_(None),
        )
        .values(**checked, revision=node.revision + 1)
        .execution_options(synchronize_session=False)
    )
    session.expire_all()
    node = session.get(StoryNode, payload["node_id"])
    if result.rowcount != 1:
        raise HTTPException(
            409,
            detail={"code": "revision_conflict", "current": jsonable_encoder(serialize(node))},
        )
    from novel_harness.services.search_index import sync_record

    sync_record(session, node)
    return node


def _promote_chapter_summary(
    session: Session, candidate: MemoryCandidate, payload: dict[str, Any]
) -> ChapterSummary:
    version = session.get(ChapterVersion, payload["version_id"])
    chapter = session.get(StoryNode, payload["chapter_id"])
    if (
        version is None
        or chapter is None
        or version.project_id != candidate.project_id
        or chapter.project_id != candidate.project_id
        or version.chapter_id != chapter.id
        or candidate.chapter_id != chapter.id
        or chapter.deleted_at is not None
        or candidate.source_version_id != version.id
        or hashlib.sha256(version.content.encode()).hexdigest() != payload["content_hash"]
    ):
        raise HTTPException(409, detail={"code": "CANDIDATE_SOURCE_VERSION_CHANGED"})
    document = session.get(ChapterDocument, chapter.id)
    if document is None or document.project_id != candidate.project_id:
        raise HTTPException(409, detail={"code": "CANDIDATE_SOURCE_VERSION_CHANGED"})
    summary = ChapterSummary(
        project_id=candidate.project_id,
        chapter_id=chapter.id,
        version_id=version.id,
        title=payload["title"],
        content_hash=payload["content_hash"],
        recap=payload["recap"],
        details=payload["details"],
        origin="import_confirmed",
        status="valid",
        provider="",
    )
    session.add(summary)
    session.flush()
    from novel_harness.services.chapter_summaries import write_ledger

    write_ledger(session)
    return summary


def confirm_import_candidate(
    session: Session,
    project_id: str,
    candidate_id: str,
    revision: int,
    *,
    resolution: str | None = None,
    persist_conflict: bool = True,
) -> MemoryCandidate:
    _begin_write(session)
    candidate = _require_import_candidate(session, project_id, candidate_id)
    if candidate.status == "confirmed" and revision in {
        candidate.revision,
        candidate.revision - 1,
    }:
        _validated_import_candidate(session, candidate)
        _validate_import_promotion(session, candidate)
        return candidate
    if candidate.revision != revision:
        _import_revision_conflict(candidate)
    if candidate.status == "rejected":
        raise HTTPException(409, detail={"code": "MEMORY_CANDIDATE_REJECTED"})
    if resolution is not None and (
        resolution != "create_separate" or candidate.kind not in _CREATE_SEPARATE_KINDS
    ):
        raise _invalid("INVALID_CANDIDATE_RESOLUTION")
    if candidate.status == "conflict" and resolution != "create_separate":
        raise HTTPException(
            409,
            detail={
                "code": "CANDIDATE_RECORD_CONFLICT",
                "current": serialize_import_candidate(candidate),
            },
        )
    _, kind, payload, evidence, dedupe = _validated_import_candidate(session, candidate)
    candidate.kind = kind
    conflict = _business_conflict(session, candidate, payload)
    if conflict is not None and resolution != "create_separate":
        if not persist_conflict:
            raise HTTPException(409, detail=conflict)
        result = session.execute(
            update(MemoryCandidate)
            .where(
                MemoryCandidate.id == candidate.id,
                MemoryCandidate.revision == revision,
                MemoryCandidate.status.in_(("pending", "conflict")),
            )
            .values(
                payload=payload,
                evidence=evidence,
                dedupe_key=dedupe,
                conflict=conflict,
                status="conflict",
                revision=revision + 1,
            )
            .execution_options(synchronize_session=False)
        )
        session.expire_all()
        current = _require_import_candidate(session, project_id, candidate_id)
        if result.rowcount != 1:
            _import_revision_conflict(current)
        raise ImportCandidateConflict(current)
    promoted = _promote_import_candidate(session, candidate, payload)
    promotion_fingerprint = _promotion_fingerprint(
        candidate,
        origin="import",
        promoted_type=kind,
        promoted_record_id=promoted.id,
        candidate_identity=dedupe,
    )
    result = session.execute(
        update(MemoryCandidate)
        .where(
            MemoryCandidate.id == candidate.id,
            MemoryCandidate.project_id == project_id,
            MemoryCandidate.revision == revision,
            MemoryCandidate.status.in_(("pending", "conflict")),
        )
        .values(
            kind=kind,
            payload=payload,
            evidence=evidence,
            dedupe_key=dedupe,
            conflict={},
            status="confirmed",
            revision=revision + 1,
            promoted_type=kind,
            promoted_record_id=promoted.id,
            promotion_fingerprint=promotion_fingerprint,
        )
        .execution_options(synchronize_session=False)
    )
    session.expire_all()
    current = _require_import_candidate(session, project_id, candidate_id)
    if result.rowcount != 1:
        _import_revision_conflict(current)
    return current


def bulk_confirm_import_candidates(
    session: Session, project_id: str, entries: list[dict[str, Any]]
) -> list[MemoryCandidate]:
    _begin_write(session)
    ids = [entry["candidate_id"] for entry in entries]
    if len(ids) != len(set(ids)):
        raise _invalid("DUPLICATE_MEMORY_CANDIDATE")
    if len(entries) > 100:
        raise _invalid("MEMORY_CANDIDATE_BULK_LIMIT")
    for candidate_id in ids:
        origins = candidate_origins(session, project_id, candidate_id)
        if not origins:
            raise HTTPException(404, detail={"code": "MEMORY_CANDIDATE_NOT_FOUND"})
        if len(origins) != 1:
            raise HTTPException(
                409, detail={"code": "MEMORY_CANDIDATE_AMBIGUOUS"}
            )
        if origins != {"import"}:
            raise _invalid("BULK_IMPORT_CANDIDATE_REQUIRED")
    candidates = [_require_import_candidate(session, project_id, item) for item in ids]
    for candidate, entry in zip(candidates, entries, strict=True):
        if candidate.status == "confirmed" and entry["revision"] in {
            candidate.revision,
            candidate.revision - 1,
        }:
            _validated_import_candidate(session, candidate)
            _validate_import_promotion(session, candidate)
            continue
        if candidate.status == "conflict":
            raise HTTPException(409, detail={"code": "CANDIDATE_RECORD_CONFLICT"})
        if candidate.status != "pending" or candidate.revision != entry["revision"]:
            _import_revision_conflict(candidate)
        _, _, payload, _, _ = _validated_import_candidate(session, candidate)
        conflict = _business_conflict(session, candidate, payload)
        if conflict is not None:
            raise HTTPException(409, detail=conflict)
    return [
        confirm_import_candidate(
            session,
            project_id,
            entry["candidate_id"],
            entry["revision"],
            persist_conflict=False,
        )
        for entry in entries
    ]


def list_import_candidates(
    session: Session,
    project_id: str,
    *,
    chapter_id: str | None = None,
    status: str | None = None,
) -> list[MemoryCandidate]:
    require_project(session, project_id)
    statement = select(MemoryCandidate).where(MemoryCandidate.project_id == project_id)
    if chapter_id is not None:
        _active_node(session, project_id, chapter_id)
        statement = statement.where(MemoryCandidate.chapter_id == chapter_id)
    if status is not None:
        statement = statement.where(MemoryCandidate.status == status)
    return list(session.scalars(statement).all())


def import_candidate_counts(session: Session, project_id: str) -> dict[str, int]:
    return {
        status: count
        for status, count in session.execute(
            select(MemoryCandidate.status, func.count())
            .where(MemoryCandidate.project_id == project_id)
            .group_by(MemoryCandidate.status)
        )
    }


def _unified_cursor_scope(
    project_id: str,
    chapter_id: str | None,
    status: str | None,
    origin: str | None = None,
) -> list[Any]:
    scope = [project_id, chapter_id, status]
    if origin is not None:
        scope.append(origin)
    return scope


def _encode_unified_cursor(
    scope: list[Any], origin: str, candidate_id: str
) -> str:
    value = {"v": 2, "scope": scope, "origin": origin, "after": candidate_id}
    return base64.urlsafe_b64encode(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    ).decode()


def _decode_unified_cursor(
    session: Session, cursor: str, scope: list[Any]
) -> tuple[datetime, str, str]:
    try:
        if len(cursor) > 4096:
            raise ValueError
        value = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        candidate_id = value["after"]
        version = value.get("v")
        origin = "generated" if version == 1 else value["origin"]
        if (
            version not in {1, 2}
            or value.get("scope") != scope
            or origin not in {"generated", "import"}
            or not isinstance(candidate_id, str)
            or not candidate_id
            or len(candidate_id) > 64
        ):
            raise ValueError
        model = GeneratedMemoryCandidate if origin == "generated" else MemoryCandidate
        anchor = session.get(model, candidate_id)
        if (
            anchor is None
            or anchor.project_id != scope[0]
            or (scope[1] is not None and anchor.chapter_id != scope[1])
        ):
            raise ValueError
        return anchor.created_at, anchor.id, origin
    except (
        ValueError,
        KeyError,
        TypeError,
        RecursionError,
        binascii.Error,
        UnicodeDecodeError,
    ) as exc:
        raise _invalid_cursor() from exc


def list_unified_candidates(
    session: Session,
    project_id: str,
    *,
    chapter_id: str | None = None,
    status: str | None = None,
    origin: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> dict[str, Any]:
    require_project(session, project_id)
    if chapter_id is not None:
        _active_node(session, project_id, chapter_id)
    limit = min(100, max(1, limit))
    scope = _unified_cursor_scope(project_id, chapter_id, status, origin)
    anchor = _decode_unified_cursor(session, cursor, scope) if cursor else None

    generated_filters = [GeneratedMemoryCandidate.project_id == project_id]
    imported_filters = [MemoryCandidate.project_id == project_id]
    if chapter_id is not None:
        generated_filters.append(GeneratedMemoryCandidate.chapter_id == chapter_id)
        imported_filters.append(MemoryCandidate.chapter_id == chapter_id)
    if status is not None:
        generated_filters.append(GeneratedMemoryCandidate.status == status)
        imported_filters.append(MemoryCandidate.status == status)

    def origin_page(model, origin: str, filters):
        statement = select(model).where(*filters)
        if anchor is not None:
            created_at, candidate_id, anchor_origin = anchor
            statement = statement.where(
                or_(
                    model.created_at > created_at,
                    and_(model.created_at == created_at, model.id > candidate_id),
                    and_(
                        model.created_at == created_at,
                        model.id == candidate_id,
                        origin > anchor_origin,
                    ),
                )
            )
        return list(
            session.scalars(
                statement.order_by(model.created_at, model.id).limit(limit + 1)
            ).all()
        )

    models_and_filters = []
    if origin in (None, "generated"):
        models_and_filters.append(
            (GeneratedMemoryCandidate, "generated", generated_filters)
        )
    if origin in (None, "import"):
        models_and_filters.append((MemoryCandidate, "import", imported_filters))
    rows: list[tuple[str, GeneratedMemoryCandidate | MemoryCandidate]] = [
        (row_origin, item)
        for model, row_origin, filters in models_and_filters
        for item in origin_page(model, row_origin, filters)
    ]
    rows.sort(key=lambda pair: (pair[1].created_at, pair[1].id, pair[0]))
    page = rows[: limit + 1]
    items = page[:limit]
    next_cursor = (
        _encode_unified_cursor(scope, items[-1][0], items[-1][1].id)
        if len(page) > limit
        else None
    )
    counts: dict[str, int] = {}
    filtered_total = sum(
        session.scalar(select(func.count()).select_from(model).where(*filters)) or 0
        for model, _row_origin, filters in models_and_filters
    )
    count_statements = [
        select(model.status, func.count())
        .where(model.project_id == project_id)
        .group_by(model.status)
        for model, _row_origin, _filters in models_and_filters
    ]
    for statement in count_statements:
        for row_status, count in session.execute(statement):
            counts[row_status] = counts.get(row_status, 0) + count
    return {
        "items": items,
        "next_cursor": next_cursor,
        "total": filtered_total,
        "counts": counts,
    }
