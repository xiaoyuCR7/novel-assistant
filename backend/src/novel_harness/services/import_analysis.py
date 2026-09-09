"""Durable, fenced analysis of bounded imported source chunks."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import OperationalError

from novel_harness.ai.base import ContextBudgetError, ExecutionLimits, ProviderExecutionError
from novel_harness.ai.import_prompts import (
    ImportExtractionResult,
    ImportSummaryMerge,
    ImportSummaryPartial,
    build_source_memory_request,
    build_summary_map_request,
    build_summary_merge_request,
)
from novel_harness.db.models import (
    ChapterDocument,
    ChapterVersion,
    ImportAnalysisUnit,
    ImportBatch,
    MemoryCandidate,
    SourceDocument,
    StoryNode,
)
from novel_harness.services.chunks import project_chunks
from novel_harness.services.import_drafts import normalize_relative_path
from novel_harness.services.job_state import command_hash
from novel_harness.services.memory_candidates import (
    _validate_import_evidence,
    _validate_import_payload,
    import_candidate_analysis_identity,
    import_candidate_dedupe_key,
)
from novel_harness.services.serialization import serialize
from novel_harness.services.source_identity import source_document_identity_hash

logger = logging.getLogger(__name__)
_ERROR_MESSAGE_LIMIT = 1000
_MERGE_GROUP_SIZE = 2
_MERGE_MAX_DEPTH = 64
_MERGE_NODE_BUDGET = 10_000


class _StoredEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    quote: str = Field(min_length=1, max_length=1200)
    start: int = Field(strict=True, ge=0)
    end: int = Field(strict=True, gt=0)


class _StoredSummaryMap(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    recap: str = Field(min_length=1, max_length=2000)
    evidence: list[_StoredEvidence] = Field(min_length=1, max_length=20)
    chapter_id: str = Field(min_length=1, max_length=36)
    version_id: str = Field(min_length=1, max_length=36)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_document_id: str = Field(min_length=1, max_length=36)
    source_start: int | None = Field(default=None, strict=True, ge=0)
    source_end: int | None = Field(default=None, strict=True, gt=0)


class _SummaryMapManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source_version_id: str = Field(min_length=1, max_length=36)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_start: int | None = Field(default=None, strict=True, ge=0)
    source_end: int | None = Field(default=None, strict=True, gt=0)


class _StoredSummaryMerge(_StoredSummaryMap):
    recap: str = Field(min_length=1, max_length=4000)
    merge_level: int = Field(strict=True, ge=0, le=64)
    final: bool
    input_unit_ids: list[str] = Field(min_length=1, max_length=_MERGE_GROUP_SIZE)


class _SummaryMergeManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    input_unit_ids: list[str] = Field(min_length=1, max_length=_MERGE_GROUP_SIZE)
    merge_level: int = Field(strict=True, ge=0, le=64)
    final: bool


class _ConsolidationManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    kind: str = Field(min_length=1, max_length=32)
    candidate_count: int = Field(strict=True, ge=1)
    candidate_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class ImportUnitFence:
    unit_id: str
    project_id: str
    import_batch_id: str
    worker_epoch: str
    attempt_count: int
    kind: str
    unit_key: str
    chunk_key: str | None
    source_hash: str
    source_document_id: str | None
    chapter_id: str | None
    provider_identity_hash: str


@dataclass(slots=True)
class _MergeValidationState:
    memo: dict[
        str,
        tuple[list[ImportAnalysisUnit], list[dict[str, str]], _StoredSummaryMap],
    ]
    parsed: dict[str, _StoredSummaryMap]
    visiting: set[str]
    visited: set[str]

    def visit(self, unit_id: str) -> None:
        if unit_id in self.visited:
            return
        if len(self.visited) >= _MERGE_NODE_BUDGET:
            raise ValueError("IMPORT_ANALYSIS_MERGE_INPUT_INVALID")
        self.visited.add(unit_id)


def _begin_immediate(session) -> None:
    if not session.in_transaction():
        session.execute(text("BEGIN IMMEDIATE"))


def _require_batch(session, project_id: str, batch_id: str) -> ImportBatch:
    batch = session.get(ImportBatch, batch_id)
    if batch is None or batch.project_id != project_id:
        raise HTTPException(404, detail={"code": "IMPORT_BATCH_NOT_FOUND"})
    return batch


def _safe_error(code: str, message: str) -> tuple[str, str]:
    safe_code = code[:80] if code and code.replace("_", "").isalnum() else "ANALYSIS_FAILED"
    return safe_code, message[:_ERROR_MESSAGE_LIMIT]


def _revision_conflict(session, batch: ImportBatch) -> None:
    raise HTTPException(
        409,
        detail={
            "code": "IMPORT_ANALYSIS_REVISION_CONFLICT",
            "current": jsonable_encoder(
                analysis_status(session, batch.project_id, batch.id)
            ),
        },
    )


def _counts(session, batch_id: str) -> dict[str, int]:
    rows = session.execute(
        select(ImportAnalysisUnit.status, func.count(ImportAnalysisUnit.id))
        .where(ImportAnalysisUnit.import_batch_id == batch_id)
        .group_by(ImportAnalysisUnit.status)
    )
    counts = {status: int(count) for status, count in rows}
    return {
        "total": sum(counts.values()),
        "completed": counts.get("succeeded", 0),
        "failed": counts.get("failed", 0),
        "queued": counts.get("queued", 0),
        "running": counts.get("running", 0),
    }


def analysis_status_payload_from_batch(
    batch: ImportBatch,
    current_unit: ImportAnalysisUnit | None,
    counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    progress = counts or {
        "total": batch.total_units,
        "completed": batch.completed_units,
        "failed": 0,
        "queued": max(0, batch.total_units - batch.completed_units),
        "running": 0,
    }
    return {
        "id": batch.id,
        "project_id": batch.project_id,
        "status": batch.status,
        "revision": batch.revision,
        "progress": progress,
        "current_unit": serialize(current_unit) if current_unit is not None else None,
        "last_error": batch.last_error[:4000],
        "created_at": batch.created_at,
        "updated_at": batch.updated_at,
    }


def analysis_status(session, project_id: str, batch_id: str) -> dict[str, Any]:
    batch = _require_batch(session, project_id, batch_id)
    counts = _counts(session, batch.id)
    current = session.scalar(
        select(ImportAnalysisUnit)
        .where(
            ImportAnalysisUnit.import_batch_id == batch.id,
            ImportAnalysisUnit.status == "running",
        )
        .order_by(ImportAnalysisUnit.created_at, ImportAnalysisUnit.id)
        .limit(1)
    )
    return analysis_status_payload_from_batch(batch, current, counts)


def latest_analysis_status(session, project_id: str) -> dict[str, Any]:
    batch = session.scalar(
        select(ImportBatch)
        .where(ImportBatch.project_id == project_id)
        .order_by(ImportBatch.created_at.desc(), ImportBatch.id.desc())
        .limit(1)
    )
    if batch is None:
        raise HTTPException(404, detail={"code": "IMPORT_BATCH_NOT_FOUND"})
    return analysis_status(session, project_id, batch.id)


def _action(
    session,
    project_id: str,
    batch_id: str,
    revision: int,
    *,
    target: str,
    allowed: set[str],
    provider_identity: dict[str, Any] | None = None,
    adopt_current_provider: bool = False,
) -> ImportBatch:
    _begin_immediate(session)
    batch = _require_batch(session, project_id, batch_id)
    if batch.status == target and revision in {batch.revision, batch.revision - 1}:
        return batch
    if batch.revision != revision:
        _revision_conflict(session, batch)
    if batch.status not in allowed:
        raise HTTPException(409, detail={"code": "IMPORT_ANALYSIS_ACTION_INVALID"})
    if provider_identity is not None:
        if (
            batch.analysis_provider_identity
            and batch.analysis_provider_identity != provider_identity
            and not adopt_current_provider
        ):
            raise HTTPException(409, detail={"code": "PROVIDER_CHANGED"})
        batch.analysis_provider_identity = provider_identity
    batch.status = target
    batch.last_error = ""
    batch.revision += 1
    return batch


def continue_analysis(
    session,
    project_id: str,
    batch_id: str,
    revision: int,
    provider_identity: dict[str, Any],
    adopt_current_provider: bool = False,
) -> ImportBatch:
    if adopt_current_provider:
        raise HTTPException(
            409,
            detail={"code": "IMPORT_ANALYSIS_ADOPT_REQUIRES_RETRY"},
        )
    return _action(
        session,
        project_id,
        batch_id,
        revision,
        target="analyzing",
        allowed={"imported", "paused", "partially_analyzed", "analysis_failed"},
        provider_identity=provider_identity,
    )


def pause_analysis(session, project_id: str, batch_id: str, revision: int) -> ImportBatch:
    return _action(
        session,
        project_id,
        batch_id,
        revision,
        target="paused",
        allowed={"analyzing", "partially_analyzed", "analysis_failed", "imported"},
    )


def retry_analysis(
    session,
    project_id: str,
    batch_id: str,
    revision: int,
    provider_identity: dict[str, Any] | None = None,
    adopt_current_provider: bool = False,
) -> ImportBatch:
    _begin_immediate(session)
    batch = _require_batch(session, project_id, batch_id)
    failed_count = session.scalar(
        select(func.count(ImportAnalysisUnit.id)).where(
            ImportAnalysisUnit.import_batch_id == batch.id,
            ImportAnalysisUnit.status == "failed",
        )
    )
    running_count = session.scalar(
        select(func.count(ImportAnalysisUnit.id)).where(
            ImportAnalysisUnit.import_batch_id == batch.id,
            ImportAnalysisUnit.status == "running",
        )
    )
    if adopt_current_provider and (
        provider_identity is None or running_count or not failed_count
    ):
        raise HTTPException(
            409,
            detail={"code": "IMPORT_ANALYSIS_ADOPT_NOT_SAFE"},
        )
    if (
        batch.status == "analyzing"
        and not failed_count
        and revision
        in {
            batch.revision,
            batch.revision - 1,
        }
    ):
        return batch
    if batch.revision != revision:
        _revision_conflict(session, batch)
    if batch.status not in {"paused", "partially_analyzed", "analysis_failed", "analyzing"}:
        raise HTTPException(409, detail={"code": "IMPORT_ANALYSIS_ACTION_INVALID"})
    if adopt_current_provider:
        batch.analysis_provider_identity = provider_identity
    session.execute(
        update(ImportAnalysisUnit)
        .where(
            ImportAnalysisUnit.import_batch_id == batch.id,
            ImportAnalysisUnit.status == "failed",
        )
        .values(
            status="queued",
            worker_epoch=None,
            error_code=None,
            error_message="",
        )
    )
    batch.status = "analyzing"
    batch.last_error = ""
    batch.revision += 1
    return batch


def peek_import_unit(database, project_id: str) -> tuple[datetime, str] | None:
    with database.job_session_scope() as session:
        row = session.execute(
            select(ImportAnalysisUnit.created_at, ImportAnalysisUnit.id)
            .join(ImportBatch, ImportBatch.id == ImportAnalysisUnit.import_batch_id)
            .where(
                ImportAnalysisUnit.project_id == project_id,
                ImportBatch.project_id == project_id,
                ImportAnalysisUnit.project_id == ImportBatch.project_id,
                ImportAnalysisUnit.status == "queued",
                ImportBatch.status == "analyzing",
            )
            .order_by(ImportAnalysisUnit.created_at, ImportAnalysisUnit.id)
            .limit(1)
        ).first()
        return (row[0], row[1]) if row else None


def claim_import_unit(
    database, project_id: str, worker_epoch: str, unit_id: str | None = None
) -> ImportUnitFence | None:
    with database.job_session_scope() as session:
        _begin_immediate(session)
        query = (
            select(ImportAnalysisUnit)
            .join(ImportBatch, ImportBatch.id == ImportAnalysisUnit.import_batch_id)
            .where(
                ImportAnalysisUnit.project_id == project_id,
                ImportBatch.project_id == project_id,
                ImportAnalysisUnit.project_id == ImportBatch.project_id,
                ImportAnalysisUnit.status == "queued",
                ImportBatch.status == "analyzing",
            )
        )
        if unit_id is not None:
            query = query.where(ImportAnalysisUnit.id == unit_id)
        unit = session.scalar(
            query.order_by(ImportAnalysisUnit.created_at, ImportAnalysisUnit.id).limit(1)
        )
        if unit is None:
            return None
        next_attempt = unit.attempt_count + 1
        result = session.execute(
            update(ImportAnalysisUnit)
            .where(ImportAnalysisUnit.id == unit.id, ImportAnalysisUnit.status == "queued")
            .values(
                status="running",
                worker_epoch=worker_epoch,
                attempt_count=next_attempt,
                error_code=None,
                error_message="",
            )
        )
        if result.rowcount != 1:
            return None
        batch = session.get(ImportBatch, unit.import_batch_id)
        batch.revision += 1
        return ImportUnitFence(
            unit_id=unit.id,
            project_id=unit.project_id,
            import_batch_id=unit.import_batch_id,
            worker_epoch=worker_epoch,
            attempt_count=next_attempt,
            kind=unit.kind,
            unit_key=unit.unit_key,
            chunk_key=unit.chunk_key,
            source_hash=unit.source_hash,
            source_document_id=unit.source_document_id,
            chapter_id=unit.chapter_id,
            provider_identity_hash=command_hash(batch.analysis_provider_identity),
        )


def recover_import_units(database, project_id: str, worker_epoch: str) -> int:
    with database.job_session_scope() as session:
        _begin_immediate(session)
        units = list(
            session.scalars(
                select(ImportAnalysisUnit).where(
                    ImportAnalysisUnit.project_id == project_id,
                    ImportAnalysisUnit.status == "running",
                    (ImportAnalysisUnit.worker_epoch.is_(None))
                    | (ImportAnalysisUnit.worker_epoch != worker_epoch),
                )
            )
        )
        if not units:
            return 0
        batch_ids = {unit.import_batch_id for unit in units}
        for unit in units:
            unit.status = "failed"
            unit.error_code = "ANALYSIS_RESULT_UNKNOWN"
            unit.error_message = "The previous analysis result is unknown; retry explicitly."
        for batch_id in batch_ids:
            batch = session.get(ImportBatch, batch_id)
            if batch is not None:
                session.flush()
                counts = _counts(session, batch.id)
                batch.completed_units = counts["completed"]
                batch.total_units = counts["total"]
                if batch.status != "paused":
                    batch.status = (
                        "partially_analyzed"
                        if counts["completed"] or counts["queued"] or counts["running"]
                        else "analysis_failed"
                    )
                batch.last_error = "ANALYSIS_RESULT_UNKNOWN"
                batch.revision += 1
        return len(units)


def recover_current_epoch_orphans(database, project_id: str, worker_epoch: str) -> int:
    """Fence results left uncertain by this executor's prior completed tick."""
    with database.job_session_scope() as session:
        _begin_immediate(session)
        units = list(
            session.scalars(
                select(ImportAnalysisUnit).where(
                    ImportAnalysisUnit.project_id == project_id,
                    ImportAnalysisUnit.status == "running",
                    ImportAnalysisUnit.worker_epoch == worker_epoch,
                )
            )
        )
        if not units:
            return 0
        batch_ids = {unit.import_batch_id for unit in units}
        for unit in units:
            unit.status = "failed"
            unit.error_code = "ANALYSIS_RESULT_UNKNOWN"
            unit.error_message = "The previous analysis result is unknown; retry explicitly."
        session.flush()
        for batch_id in batch_ids:
            batch = session.get(ImportBatch, batch_id)
            if batch is None or batch.project_id != project_id:
                continue
            counts = _counts(session, batch.id)
            batch.completed_units = counts["completed"]
            batch.total_units = counts["total"]
            batch.last_error = "ANALYSIS_RESULT_UNKNOWN"
            if batch.status != "paused":
                batch.status = (
                    "partially_analyzed"
                    if counts["completed"] or counts["queued"] or counts["running"]
                    else "analysis_failed"
                )
            batch.revision += 1
        return len(units)


def _load_search_chunk(session, unit: ImportAnalysisUnit) -> dict[str, Any]:
    if not unit.chunk_key:
        raise ValueError("IMPORT_ANALYSIS_CHUNK_MISSING")
    row = (
        session.execute(
            text(
                "SELECT chunk_key,document_key,strategy,ordinal,start_offset,end_offset,"
                "source_hash,chunk_hash,body FROM search_chunks WHERE chunk_key=:key"
            ),
            {"key": unit.chunk_key},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise ValueError("IMPORT_ANALYSIS_CHUNK_MISSING")
    result = dict(row)
    if (
        result["chunk_key"] != unit.chunk_key
        or result["chunk_hash"] != unit.source_hash
        or len(result["body"]) > 1200
        or result["end_offset"] - result["start_offset"] != len(result["body"])
    ):
        raise ValueError("IMPORT_ANALYSIS_SOURCE_CHANGED")
    return result


def _fence_matches(
    unit: ImportAnalysisUnit | None,
    batch: ImportBatch | None,
    fence: ImportUnitFence,
) -> bool:
    return bool(
        unit is not None
        and batch is not None
        and unit.id == fence.unit_id
        and unit.project_id == fence.project_id == batch.project_id
        and unit.import_batch_id == fence.import_batch_id == batch.id
        and unit.status == "running"
        and unit.worker_epoch == fence.worker_epoch
        and unit.attempt_count == fence.attempt_count
        and unit.kind == fence.kind
        and unit.unit_key == fence.unit_key
        and unit.chunk_key == fence.chunk_key
        and unit.source_hash == fence.source_hash
        and unit.source_document_id == fence.source_document_id
        and unit.chapter_id == fence.chapter_id
        and command_hash(batch.analysis_provider_identity) == fence.provider_identity_hash
        and batch.status in {"analyzing", "paused"}
    )


def _source_snapshot(session, unit: ImportAnalysisUnit, chunk: dict[str, Any]):
    source = session.get(SourceDocument, unit.source_document_id)
    if (
        source is None
        or source.project_id != unit.project_id
        or source.import_batch_id != unit.import_batch_id
        or source.deleted_at is not None
        or chunk["document_key"] != f"source_document:{source.id}"
        or hashlib.sha256(source.content.encode()).hexdigest() != source.content_hash
    ):
        raise ValueError("IMPORT_ANALYSIS_SOURCE_CHANGED")
    identity = source_document_identity_hash(source)
    if chunk["source_hash"] != identity:
        raise ValueError("IMPORT_ANALYSIS_SOURCE_CHANGED")
    start, end = chunk["start_offset"], chunk["end_offset"]
    if source.content[start:end] != chunk["body"]:
        raise ValueError("IMPORT_ANALYSIS_SOURCE_CHANGED")
    return source, identity


def _chapter_snapshot(session, unit: ImportAnalysisUnit):
    chapter = session.get(StoryNode, unit.chapter_id)
    document = session.get(ChapterDocument, unit.chapter_id)
    source = session.get(SourceDocument, unit.source_document_id)
    if (
        chapter is None
        or chapter.project_id != unit.project_id
        or chapter.status != "completed"
        or document is None
        or document.project_id != unit.project_id
        or source is None
        or source.project_id != unit.project_id
        or source.import_batch_id != unit.import_batch_id
        or source.chapter_id not in {None, chapter.id}
        or hashlib.sha256(source.content.encode()).hexdigest() != source.content_hash
    ):
        raise ValueError("IMPORT_ANALYSIS_SOURCE_CHANGED")
    matches: list[tuple[ChapterVersion, dict[str, Any]]] = []
    stored_version_id = None
    stored_content_hash = None
    stored_source_start = None
    stored_source_end = None
    if unit.status == "succeeded" and unit.kind == "summary_map":
        stored = _StoredSummaryMap.model_validate(unit.result)
        stored_version_id = stored.version_id
        stored_content_hash = stored.content_hash
        stored_source_start = stored.source_start
        stored_source_end = stored.source_end
    elif unit.result:
        stored = _SummaryMapManifest.model_validate(unit.result)
        stored_version_id = stored.source_version_id
        stored_content_hash = stored.content_hash
        stored_source_start = stored.source_start
        stored_source_end = stored.source_end
    if (stored_source_start is None) != (stored_source_end is None):
        raise ValueError("IMPORT_ANALYSIS_SOURCE_CHANGED")
    if stored_version_id is not None:
        selected = session.get(ChapterVersion, stored_version_id)
        versions = [selected] if selected is not None else []
    else:
        versions = session.scalars(
            select(ChapterVersion)
            .where(
                ChapterVersion.project_id == unit.project_id,
                ChapterVersion.chapter_id == chapter.id,
                ChapterVersion.source == "import",
            )
            .order_by(ChapterVersion.id)
        )
    for version in versions:
        content_hash = hashlib.sha256(version.content.encode()).hexdigest()
        if (
            version.project_id != unit.project_id
            or version.chapter_id != chapter.id
            or version.source != "import"
            or (stored_content_hash is not None and stored_content_hash != content_hash)
            or (stored_version_id is None and version.content not in source.content)
        ):
            continue
        item = {
            "type": "manuscript",
            "id": chapter.id,
            "title": chapter.title + " · 正文版本",
            "content": version.content,
            "record": {"version_id": version.id},
        }
        for candidate_chunk in project_chunks(item, version.content):
            if (
                candidate_chunk["chunk_key"] == unit.chunk_key
                and candidate_chunk["chunk_hash"] == unit.source_hash
            ):
                matches.append((version, candidate_chunk))
    if len(matches) != 1:
        raise ValueError("IMPORT_ANALYSIS_SOURCE_CHANGED")
    version, chunk = matches[0]
    start, end = chunk["start_offset"], chunk["end_offset"]
    if version.content[start:end] != chunk["body"]:
        raise ValueError("IMPORT_ANALYSIS_SOURCE_CHANGED")
    if stored_source_start is not None and stored_source_end is not None:
        first = stored_source_start
        if (
            stored_source_end - stored_source_start != len(version.content)
            or stored_source_end > len(source.content)
            or source.content[stored_source_start:stored_source_end] != version.content
        ):
            raise ValueError("IMPORT_ANALYSIS_SOURCE_CHANGED")
    else:
        first = source.content.find(version.content)
        if first < 0 or source.content.find(version.content, first + 1) >= 0:
            raise ValueError("IMPORT_ANALYSIS_SOURCE_CHANGED")
    return chapter, version, source, first, chunk


def _candidate_id(project_id: str, dedupe: str) -> str:
    try:
        namespace = UUID(project_id)
    except ValueError:
        namespace = NAMESPACE_URL
    return str(uuid5(namespace, f"import-candidate:{dedupe}"))


def _insert_candidate(
    session,
    *,
    unit: ImportAnalysisUnit,
    source: SourceDocument,
    source_identity: str,
    kind: str,
    payload: dict[str, Any],
    evidence: list[dict[str, Any]],
    chapter_id: str | None = None,
    source_version_id: str | None = None,
) -> str:
    checked_payload = _validate_import_payload(kind, payload)
    relative_path = normalize_relative_path(source.relative_path)
    checked_evidence = [
        {**item, "relative_path": relative_path}
        for item in _validate_import_evidence(evidence)
    ]
    for item in checked_evidence:
        if (
            item["end"] > len(source.content)
            or source.content[item["start"] : item["end"]] != item["quote"]
        ):
            raise ValueError("IMPORT_ANALYSIS_EVIDENCE_CHANGED")
    dedupe = import_candidate_dedupe_key(
        source_document_id=source.id,
        source_hash=source_identity,
        kind=kind,
        payload=checked_payload,
        evidence=checked_evidence,
    )
    candidate_id = _candidate_id(unit.project_id, dedupe)
    session.execute(
        sqlite_insert(MemoryCandidate)
        .values(
            id=candidate_id,
            project_id=unit.project_id,
            import_batch_id=unit.import_batch_id,
            source_document_id=source.id,
            chapter_id=chapter_id if chapter_id is not None else unit.chapter_id,
            source_version_id=source_version_id,
            kind=kind,
            payload=checked_payload,
            evidence=checked_evidence,
            source_hash=source_identity,
            dedupe_key=dedupe,
            analysis_identity=import_candidate_analysis_identity(kind, checked_payload),
            status="pending",
            revision=1,
            conflict={},
        )
        .on_conflict_do_nothing(index_elements=["project_id", "dedupe_key"])
    )
    return candidate_id


def _validate_relative_evidence(body: str, evidence) -> list[dict[str, Any]]:
    result = []
    for item in evidence:
        value = item.model_dump(mode="json") if hasattr(item, "model_dump") else item
        start, end, quote = value["start"], value["end"], value["quote"]
        if end <= start or end > len(body) or body[start:end] != quote:
            raise ValueError("IMPORT_ANALYSIS_EVIDENCE_CHANGED")
        result.append({"quote": quote, "start": start, "end": end})
    return result


def _validate_stored_summary_evidence(source: SourceDocument, evidence) -> None:
    for item in evidence:
        if item.end > len(source.content) or source.content[item.start : item.end] != item.quote:
            raise ValueError("IMPORT_ANALYSIS_MERGE_INPUT_INVALID")


def _merge_manifest_hash(unit: ImportAnalysisUnit, inputs: list[ImportAnalysisUnit]) -> str:
    return command_hash(
        {
            "project_id": unit.project_id,
            "import_batch_id": unit.import_batch_id,
            "chapter_id": unit.chapter_id,
            "merge_level": unit.result["merge_level"],
            "final": unit.result["final"],
            "inputs": [
                {
                    "id": item.id,
                    "kind": item.kind,
                    "source_hash": item.source_hash,
                    "result": item.result,
                }
                for item in inputs
            ],
        }
    )


def _validate_merge_inputs(
    session,
    unit: ImportAnalysisUnit,
    *,
    state: _MergeValidationState | None = None,
    depth: int = 0,
) -> tuple[list[ImportAnalysisUnit], list[dict[str, str]], _StoredSummaryMap]:
    if state is None:
        state = _MergeValidationState(memo={}, parsed={}, visiting=set(), visited=set())
    if unit.id in state.memo:
        return state.memo[unit.id]
    if depth > _MERGE_MAX_DEPTH or unit.id in state.visiting:
        raise ValueError("IMPORT_ANALYSIS_MERGE_INPUT_INVALID")
    state.visit(unit.id)
    state.visiting.add(unit.id)
    try:
        return _validate_merge_inputs_inner(session, unit, state=state, depth=depth)
    finally:
        state.visiting.discard(unit.id)


def _validate_merge_inputs_inner(
    session,
    unit: ImportAnalysisUnit,
    *,
    state: _MergeValidationState,
    depth: int,
) -> tuple[list[ImportAnalysisUnit], list[dict[str, str]], _StoredSummaryMap]:
    if unit.status == "succeeded":
        stored_unit = _StoredSummaryMerge.model_validate(unit.result)
        manifest = _SummaryMergeManifest(
            input_unit_ids=stored_unit.input_unit_ids,
            merge_level=stored_unit.merge_level,
            final=stored_unit.final,
        )
    else:
        manifest = _SummaryMergeManifest.model_validate(unit.result)
    input_ids = manifest.input_unit_ids
    inputs = [session.get(ImportAnalysisUnit, item_id) for item_id in input_ids]
    by_id = {item.id: item for item in inputs if item is not None}
    if (
        len(by_id) != len(input_ids)
        or set(by_id) != set(input_ids)
        or input_ids != [item.id for item in sorted(inputs, key=lambda item: item.unit_key)]
    ):
        raise ValueError("IMPORT_ANALYSIS_MERGE_INPUT_INVALID")
    ordered = [by_id[item_id] for item_id in input_ids]
    expected_kind = "summary_map" if manifest.merge_level == 0 else "summary_merge"
    parsed_results: list[_StoredSummaryMap] = []
    for child in ordered:
        if (
            child.id in state.visiting
            or child.project_id != unit.project_id
            or child.import_batch_id != unit.import_batch_id
            or child.chapter_id != unit.chapter_id
            or child.status != "succeeded"
            or child.kind != expected_kind
        ):
            raise ValueError("IMPORT_ANALYSIS_MERGE_INPUT_INVALID")
        state.visit(child.id)
        if child.kind == "summary_map":
            parsed = state.parsed.get(child.id)
            if parsed is None:
                parsed = _StoredSummaryMap.model_validate(child.result)
                chapter, version, source, source_base, _ = _chapter_snapshot(session, child)
                if (
                    parsed.chapter_id != chapter.id
                    or parsed.version_id != version.id
                    or parsed.content_hash
                    != hashlib.sha256(version.content.encode()).hexdigest()
                    or parsed.source_document_id != source.id
                    or (
                        parsed.source_start is not None
                        and (
                            parsed.source_start != source_base
                            or parsed.source_end != source_base + len(version.content)
                        )
                    )
                ):
                    raise ValueError("IMPORT_ANALYSIS_MERGE_INPUT_INVALID")
                _validate_stored_summary_evidence(source, parsed.evidence)
                state.parsed[child.id] = parsed
            else:
                source = session.get(SourceDocument, parsed.source_document_id)
        else:
            parsed = _StoredSummaryMerge.model_validate(child.result)
            if parsed.merge_level != manifest.merge_level - 1 or parsed.final:
                raise ValueError("IMPORT_ANALYSIS_MERGE_INPUT_INVALID")
            _, _, nested_provenance = _validate_merge_inputs(
                session,
                child,
                state=state,
                depth=depth + 1,
            )
            source = session.get(SourceDocument, parsed.source_document_id)
            if (
                parsed.chapter_id != nested_provenance.chapter_id
                or parsed.version_id != nested_provenance.version_id
                or parsed.content_hash != nested_provenance.content_hash
                or parsed.source_document_id != nested_provenance.source_document_id
                or parsed.source_start != nested_provenance.source_start
                or parsed.source_end != nested_provenance.source_end
            ):
                raise ValueError("IMPORT_ANALYSIS_MERGE_INPUT_INVALID")
        if (
            source is None
            or source.project_id != unit.project_id
            or source.import_batch_id != unit.import_batch_id
        ):
            raise ValueError("IMPORT_ANALYSIS_MERGE_INPUT_INVALID")
        if child.kind != "summary_map" or child.id not in state.parsed:
            _validate_stored_summary_evidence(source, parsed.evidence)
        parsed_results.append(parsed)
    first = parsed_results[0]
    if any(
        (
            parsed.chapter_id,
            parsed.version_id,
            parsed.content_hash,
            parsed.source_document_id,
            parsed.source_start,
            parsed.source_end,
        )
        != (
            first.chapter_id,
            first.version_id,
            first.content_hash,
            first.source_document_id,
            first.source_start,
            first.source_end,
        )
        for parsed in parsed_results[1:]
    ):
        raise ValueError("IMPORT_ANALYSIS_MERGE_INPUT_INVALID")
    if _merge_manifest_hash(unit, ordered) != unit.source_hash:
        raise ValueError("IMPORT_ANALYSIS_MERGE_INPUT_INVALID")
    partials = [{"recap": parsed.recap} for parsed in parsed_results]
    validated = (ordered, partials, first)
    state.memo[unit.id] = validated
    return validated


def _snapshot_for_dispatch(vault, fence: ImportUnitFence) -> dict[str, Any]:
    with vault.database.job_session_scope() as session:
        unit = session.get(ImportAnalysisUnit, fence.unit_id)
        batch = session.get(ImportBatch, fence.import_batch_id)
        if not _fence_matches(unit, batch, fence):
            raise ValueError("IMPORT_ANALYSIS_FENCE_LOST")
        if unit.kind == "source_memory":
            chunk = _load_search_chunk(session, unit)
            source, identity = _source_snapshot(session, unit, chunk)
            request, schema = build_source_memory_request(
                body=chunk["body"],
                relative_path=source.relative_path,
                category=source.category,
                chapter_id=unit.chapter_id,
                chunk_start=chunk["start_offset"],
                chunk_end=chunk["end_offset"],
            )
            request = _apply_execution_limits(request, batch)
            return {"kind": unit.kind, "request": request, "schema": schema}
        if unit.kind == "summary_map":
            chapter, _, _, _, chunk = _chapter_snapshot(session, unit)
            request, schema = build_summary_map_request(
                body=chunk["body"],
                chapter_id=chapter.id,
                chunk_start=chunk["start_offset"],
                chunk_end=chunk["end_offset"],
            )
            request = _apply_execution_limits(request, batch)
            return {"kind": unit.kind, "request": request, "schema": schema}
        if unit.kind == "summary_merge":
            _, partials, _ = _validate_merge_inputs(session, unit)
            request, schema = build_summary_merge_request(
                chapter_id=unit.chapter_id, partials=partials
            )
            request = _apply_execution_limits(request, batch)
            return {"kind": unit.kind, "request": request, "schema": schema}
        if unit.kind == "consolidation":
            _validate_consolidation_unit(session, unit)
            return {"kind": unit.kind}
        raise ValueError("IMPORT_ANALYSIS_KIND_INVALID")


def _apply_execution_limits(request, batch: ImportBatch):
    limits = ExecutionLimits.model_validate(batch.analysis_provider_identity)
    return request.model_copy(
        update={
            "output_token_budget": min(request.output_token_budget, limits.output_token_budget),
            "context_capacity": limits.context_capacity,
            "deadline_seconds": limits.deadline_seconds,
            "output_parameter": limits.output_parameter,
        }
    )


def _publish_source(session, unit: ImportAnalysisUnit, data: Any) -> dict[str, Any]:
    parsed = ImportExtractionResult.model_validate(data)
    chunk = _load_search_chunk(session, unit)
    source, identity = _source_snapshot(session, unit, chunk)
    candidate_ids = []
    for candidate in parsed.candidates:
        relative = _validate_relative_evidence(chunk["body"], candidate.evidence)
        evidence = [
            {
                "quote": item["quote"],
                "start": chunk["start_offset"] + item["start"],
                "end": chunk["start_offset"] + item["end"],
            }
            for item in relative
        ]
        candidate_ids.append(
            _insert_candidate(
                session,
                unit=unit,
                source=source,
                source_identity=identity,
                kind=candidate.kind,
                payload=candidate.payload,
                evidence=evidence,
            )
        )
    return {"candidate_count": len(candidate_ids), "candidate_ids": candidate_ids}


def _publish_map(session, unit: ImportAnalysisUnit, data: Any) -> dict[str, Any]:
    parsed = ImportSummaryPartial.model_validate(data)
    chapter, version, source, source_base, chunk = _chapter_snapshot(session, unit)
    relative = _validate_relative_evidence(chunk["body"], parsed.evidence)
    evidence = [
        {
            "quote": item["quote"],
            "start": source_base + chunk["start_offset"] + item["start"],
            "end": source_base + chunk["start_offset"] + item["end"],
        }
        for item in relative
    ]
    return {
        "recap": parsed.recap,
        "evidence": evidence,
        "chapter_id": chapter.id,
        "version_id": version.id,
        "content_hash": hashlib.sha256(version.content.encode()).hexdigest(),
        "source_document_id": source.id,
        "source_start": source_base,
        "source_end": source_base + len(version.content),
    }


def _publish_merge(session, unit: ImportAnalysisUnit, data: Any) -> dict[str, Any]:
    parsed = ImportSummaryMerge.model_validate(data)
    input_ids = list(unit.result["input_unit_ids"])
    ordered, _, provenance = _validate_merge_inputs(session, unit)
    evidence: list[dict[str, Any]] = []
    for child in ordered:
        for item in child.result.get("evidence", []):
            if item not in evidence:
                evidence.append(item)
            if len(evidence) == 20:
                break
        if len(evidence) == 20:
            break
    if not evidence:
        raise ValueError("IMPORT_ANALYSIS_EVIDENCE_CHANGED")
    return {
        "recap": parsed.recap,
        "evidence": evidence,
        "chapter_id": unit.chapter_id,
        "version_id": provenance.version_id,
        "content_hash": provenance.content_hash,
        "source_document_id": provenance.source_document_id,
        "source_start": provenance.source_start,
        "source_end": provenance.source_end,
        "merge_level": unit.result["merge_level"],
        "final": bool(unit.result["final"]),
        "input_unit_ids": input_ids,
    }


def _normalized_candidate_identity(candidate: MemoryCandidate) -> str:
    return import_candidate_analysis_identity(candidate.kind, candidate.payload)


def _validate_consolidation_candidates(
    session,
    *,
    project_id: str,
    batch_id: str,
    kind: str,
    identity: str,
) -> tuple[list[MemoryCandidate], str, str]:
    candidates = list(
        session.scalars(
            select(MemoryCandidate)
            .where(
                MemoryCandidate.project_id == project_id,
                MemoryCandidate.import_batch_id == batch_id,
                MemoryCandidate.kind == kind,
                MemoryCandidate.analysis_identity == identity,
            )
            .order_by(MemoryCandidate.id)
        )
    )
    if not candidates or any(item.kind != kind for item in candidates):
        raise ValueError("IMPORT_ANALYSIS_CONSOLIDATION_INVALID")
    for candidate in candidates:
        if _normalized_candidate_identity(candidate) != identity:
            raise ValueError("IMPORT_ANALYSIS_CONSOLIDATION_INVALID")
        _validate_consolidation_candidate(
            session, candidate, project_id=project_id, batch_id=batch_id
        )
    digest = _consolidation_digest(
        project_id, batch_id, identity, candidates[0].kind, candidates
    )
    return candidates, candidates[0].kind, digest


def _validate_consolidation_candidate(
    session,
    candidate: MemoryCandidate,
    *,
    project_id: str,
    batch_id: str,
) -> None:
    source = session.get(SourceDocument, candidate.source_document_id)
    payload = _validate_import_payload(candidate.kind, candidate.payload)
    evidence = _validate_import_evidence(candidate.evidence)
    if (
        source is None
        or source.project_id != project_id
        or source.import_batch_id != batch_id
        or source.deleted_at is not None
        or candidate.source_hash != source_document_identity_hash(source)
        or candidate.dedupe_key
        != import_candidate_dedupe_key(
            source_document_id=source.id,
            source_hash=candidate.source_hash,
            kind=candidate.kind,
            payload=payload,
            evidence=evidence,
        )
        or any(
            item["end"] > len(source.content)
            or source.content[item["start"] : item["end"]] != item["quote"]
            for item in evidence
        )
    ):
        raise ValueError("IMPORT_ANALYSIS_CONSOLIDATION_INVALID")


def _consolidation_digest(
    project_id: str,
    batch_id: str,
    identity: str,
    kind: str,
    candidates: list[MemoryCandidate],
) -> str:
    return command_hash(
        {
            "project_id": project_id,
            "import_batch_id": batch_id,
            "identity": identity,
            "kind": kind,
            "candidates": [
                {
                    "id": item.id,
                    "dedupe_key": item.dedupe_key,
                    "source_hash": item.source_hash,
                }
                for item in candidates
            ],
        }
    )


def _consolidation_identity(unit: ImportAnalysisUnit) -> str:
    parts = unit.unit_key.split(":")
    if len(parts) < 2 or parts[0] != "consolidate" or len(parts[1]) != 64:
        raise ValueError("IMPORT_ANALYSIS_CONSOLIDATION_INVALID")
    return parts[1]


def _validate_consolidation_unit(
    session, unit: ImportAnalysisUnit
) -> tuple[list[MemoryCandidate], _ConsolidationManifest]:
    manifest = _ConsolidationManifest.model_validate(unit.result)
    identity = _consolidation_identity(unit)
    candidates, kind, digest = _validate_consolidation_candidates(
        session,
        project_id=unit.project_id,
        batch_id=unit.import_batch_id,
        kind=manifest.kind,
        identity=identity,
    )
    if (
        manifest.identity != identity
        or manifest.kind != kind
        or manifest.candidate_count != len(candidates)
        or manifest.candidate_digest != digest
        or unit.source_hash != digest
    ):
        raise ValueError("IMPORT_ANALYSIS_CONSOLIDATION_INVALID")
    return candidates, manifest


def _seed_consolidations(session, batch: ImportBatch) -> int:
    unfinished = session.scalar(
        select(func.count(ImportAnalysisUnit.id)).where(
            ImportAnalysisUnit.import_batch_id == batch.id,
            ImportAnalysisUnit.kind == "source_memory",
            ImportAnalysisUnit.status != "succeeded",
        )
    )
    if unfinished:
        return 0
    candidates = list(
        session.scalars(
            select(MemoryCandidate)
            .where(
                MemoryCandidate.import_batch_id == batch.id,
                MemoryCandidate.kind != "chapter_summary",
            )
            .order_by(MemoryCandidate.id)
        )
    )
    groups: dict[str, list[MemoryCandidate]] = {}
    for candidate in candidates:
        identity = _normalized_candidate_identity(candidate)
        if candidate.analysis_identity != identity:
            candidate.analysis_identity = identity
        _validate_consolidation_candidate(
            session,
            candidate,
            project_id=batch.project_id,
            batch_id=batch.id,
        )
        groups.setdefault(identity, []).append(candidate)
    session.flush()
    created = 0
    for identity, group in sorted(groups.items()):
        kind = group[0].kind
        digest = _consolidation_digest(
            batch.project_id, batch.id, identity, kind, group
        )
        unit_key = f"consolidate:{identity}"
        manifest = {
            "identity": identity,
            "kind": kind,
            "candidate_count": len(group),
            "candidate_digest": digest,
        }
        result = session.execute(
            sqlite_insert(ImportAnalysisUnit)
            .values(
                id=str(uuid5(NAMESPACE_URL, f"{batch.id}:{unit_key}")),
                project_id=batch.project_id,
                import_batch_id=batch.id,
                source_document_id=group[0].source_document_id,
                chapter_id=None,
                chunk_key=None,
                unit_key=unit_key,
                kind="consolidation",
                source_hash=digest,
                status="queued",
                attempt_count=0,
                result=manifest,
            )
            .on_conflict_do_nothing(index_elements=["import_batch_id", "unit_key"])
        )
        created += max(0, result.rowcount or 0)
        canonical = session.scalar(
            select(ImportAnalysisUnit).where(
                ImportAnalysisUnit.import_batch_id == batch.id,
                ImportAnalysisUnit.unit_key == unit_key,
            )
        )
        if canonical is not None and canonical.status == "queued" and not canonical.attempt_count:
            canonical.source_hash = digest
            canonical.result = manifest
        legacy_units = session.scalars(
            select(ImportAnalysisUnit).where(
                ImportAnalysisUnit.import_batch_id == batch.id,
                ImportAnalysisUnit.kind == "consolidation",
                ImportAnalysisUnit.unit_key.like(f"{unit_key}:%"),
                ImportAnalysisUnit.status == "queued",
            )
        )
        for legacy in legacy_units:
            legacy.status = "succeeded"
            legacy.result = {
                "classification": "superseded",
                "candidate_count": len(group),
                "candidate_digest": digest,
            }
    return created


def _seed_summary_merges(session, batch: ImportBatch) -> int:
    created = 0
    chapter_ids = list(
        session.scalars(
            select(ImportAnalysisUnit.chapter_id)
            .where(
                ImportAnalysisUnit.import_batch_id == batch.id,
                ImportAnalysisUnit.kind == "summary_map",
            )
            .distinct()
        )
    )
    for chapter_id in (value for value in chapter_ids if value is not None):
        maps = list(
            session.scalars(
                select(ImportAnalysisUnit)
                .where(
                    ImportAnalysisUnit.import_batch_id == batch.id,
                    ImportAnalysisUnit.chapter_id == chapter_id,
                    ImportAnalysisUnit.kind == "summary_map",
                )
                .order_by(ImportAnalysisUnit.unit_key)
            )
        )
        if not maps or any(item.status != "succeeded" for item in maps):
            continue
        merges = list(
            session.scalars(
                select(ImportAnalysisUnit)
                .where(
                    ImportAnalysisUnit.import_batch_id == batch.id,
                    ImportAnalysisUnit.chapter_id == chapter_id,
                    ImportAnalysisUnit.kind == "summary_merge",
                )
                .order_by(ImportAnalysisUnit.unit_key)
            )
        )
        if not merges:
            inputs, level = maps, 0
        else:
            max_level = max(int(item.result.get("merge_level", 0)) for item in merges)
            inputs = [
                item for item in merges if int(item.result.get("merge_level", 0)) == max_level
            ]
            if any(item.status != "succeeded" for item in inputs) or len(inputs) == 1:
                continue
            level = max_level + 1
        groups = [
            inputs[index : index + _MERGE_GROUP_SIZE]
            for index in range(0, len(inputs), _MERGE_GROUP_SIZE)
        ]
        for ordinal, group in enumerate(groups):
            input_ids = [item.id for item in group]
            manifest = {
                "input_unit_ids": input_ids,
                "merge_level": level,
                "final": len(groups) == 1,
            }
            digest = command_hash(
                {
                    "project_id": batch.project_id,
                    "import_batch_id": batch.id,
                    "chapter_id": chapter_id,
                    "merge_level": level,
                    "final": manifest["final"],
                    "inputs": [
                        {
                            "id": item.id,
                            "kind": item.kind,
                            "source_hash": item.source_hash,
                            "result": item.result,
                        }
                        for item in group
                    ],
                }
            )
            unit_key = f"summary-merge:{chapter_id}:{level}:{ordinal}:{digest[:24]}"
            result = session.execute(
                sqlite_insert(ImportAnalysisUnit)
                .values(
                    id=str(uuid5(NAMESPACE_URL, f"{batch.id}:{unit_key}")),
                    project_id=batch.project_id,
                    import_batch_id=batch.id,
                    source_document_id=group[0].source_document_id,
                    chapter_id=chapter_id,
                    chunk_key=None,
                    unit_key=unit_key,
                    kind="summary_merge",
                    source_hash=digest,
                    status="queued",
                    attempt_count=0,
                    result=manifest,
                )
                .on_conflict_do_nothing(index_elements=["import_batch_id", "unit_key"])
            )
            created += max(0, result.rowcount or 0)
    return created


def _publish_final_summary(session, unit: ImportAnalysisUnit) -> None:
    if not unit.result.get("final"):
        return
    chapter = session.get(StoryNode, unit.chapter_id)
    document = session.get(ChapterDocument, unit.chapter_id)
    version = session.get(ChapterVersion, unit.result.get("version_id"))
    source = session.get(SourceDocument, unit.result.get("source_document_id"))
    if (
        chapter is None
        or chapter.project_id != unit.project_id
        or chapter.status != "completed"
        or document is None
        or document.project_id != unit.project_id
        or version is None
        or version.project_id != unit.project_id
        or version.chapter_id != chapter.id
        or version.source != "import"
        or hashlib.sha256(version.content.encode()).hexdigest() != unit.result.get("content_hash")
        or source is None
        or source.project_id != unit.project_id
        or source.import_batch_id != unit.import_batch_id
        or source.chapter_id not in {None, chapter.id}
        or unit.source_document_id != source.id
        or hashlib.sha256(source.content.encode()).hexdigest() != source.content_hash
        or version.content not in source.content
    ):
        raise ValueError("IMPORT_ANALYSIS_SOURCE_CHANGED")
    _insert_candidate(
        session,
        unit=unit,
        source=source,
        source_identity=source_document_identity_hash(source),
        kind="chapter_summary",
        payload={
            "chapter_id": chapter.id,
            "version_id": version.id,
            "content_hash": unit.result["content_hash"],
            "title": chapter.title,
            "recap": unit.result["recap"],
            "details": {},
        },
        evidence=unit.result["evidence"],
        chapter_id=chapter.id,
        source_version_id=version.id,
    )


def _publish_consolidation(session, unit: ImportAnalysisUnit) -> dict[str, Any]:
    candidates, manifest = _validate_consolidation_unit(session, unit)
    payloads = {command_hash(item.payload) for item in candidates}
    classification = (
        "unique" if len(candidates) == 1 else "duplicate" if len(payloads) == 1 else "conflict"
    )
    for candidate in candidates:
        if classification == "unique":
            continue
        conflict = {
            "type": classification,
            "candidate_count": manifest.candidate_count,
            "candidate_digest": manifest.candidate_digest,
        }
        changed = candidate.conflict != conflict
        if changed:
            candidate.conflict = conflict
        if candidate.status == "pending":
            candidate.status = "conflict"
            changed = True
        if changed:
            candidate.revision += 1
    return {
        "classification": classification,
        "candidate_count": manifest.candidate_count,
        "candidate_digest": manifest.candidate_digest,
    }


def _recompute_after_publication(session, batch: ImportBatch) -> None:
    seeded = _seed_summary_merges(session, batch) + _seed_consolidations(session, batch)
    session.flush()
    counts = _counts(session, batch.id)
    batch.completed_units = counts["completed"]
    batch.total_units = counts["total"]
    if batch.status == "paused":
        pass
    elif counts["failed"]:
        batch.status = (
            "partially_analyzed"
            if counts["completed"] or counts["queued"] or counts["running"]
            else "analysis_failed"
        )
    elif counts["queued"] or counts["running"] or seeded:
        batch.status = "analyzing"
    else:
        batch.status = "analyzed"
        batch.last_error = ""
    batch.revision += 1


def _publish(vault, fence: ImportUnitFence, data: Any) -> bool:
    with vault.database.job_session_scope() as session:
        _begin_immediate(session)
        unit = session.get(ImportAnalysisUnit, fence.unit_id)
        batch = session.get(ImportBatch, fence.import_batch_id)
        if not _fence_matches(unit, batch, fence):
            return False
        if unit.kind == "source_memory":
            result = _publish_source(session, unit, data)
        elif unit.kind == "summary_map":
            result = _publish_map(session, unit, data)
        elif unit.kind == "summary_merge":
            result = _publish_merge(session, unit, data)
        elif unit.kind == "consolidation":
            result = _publish_consolidation(session, unit)
        else:
            raise ValueError("IMPORT_ANALYSIS_KIND_INVALID")
        unit.result = result
        unit.status = "succeeded"
        unit.error_code = None
        unit.error_message = ""
        session.flush()
        if unit.kind == "summary_merge":
            _publish_final_summary(session, unit)
        _recompute_after_publication(session, batch)
        return True


def fail_import_unit(vault, fence: ImportUnitFence, code: str, message: str) -> bool:
    code, message = _safe_error(code, message)
    with vault.database.job_session_scope() as session:
        _begin_immediate(session)
        unit = session.get(ImportAnalysisUnit, fence.unit_id)
        batch = session.get(ImportBatch, fence.import_batch_id)
        if not _fence_matches(unit, batch, fence):
            return False
        unit.status = "failed"
        unit.error_code = code
        unit.error_message = message
        session.flush()
        counts = _counts(session, batch.id)
        batch.completed_units = counts["completed"]
        batch.total_units = counts["total"]
        batch.last_error = code
        if batch.status != "paused":
            batch.status = (
                "partially_analyzed"
                if counts["completed"] or counts["queued"] or counts["running"]
                else "analysis_failed"
            )
        batch.revision += 1
        return True


def run_import_unit(vault, fence: ImportUnitFence, provider_factory, validate_provider) -> bool:
    try:
        snapshot = _snapshot_for_dispatch(vault, fence)
        if snapshot["kind"] == "consolidation":
            return _publish(vault, fence, {})
        validate_provider()
        provider = provider_factory()
        result = provider.generate_structured(snapshot["request"], snapshot["schema"])
        return _publish(vault, fence, result.data)
    except Exception as exc:
        if isinstance(exc, ValueError) and str(exc) == "IMPORT_ANALYSIS_FENCE_LOST":
            return False
        if isinstance(exc, ContextBudgetError):
            code, message = (
                "CONTEXT_BUDGET_EXCEEDED",
                "The bounded analysis request exceeds the configured context budget.",
            )
        elif isinstance(exc, ProviderExecutionError):
            code = "ANALYSIS_RESULT_UNKNOWN" if exc.outcome == "unknown" else exc.code
            message = "The provider did not return a safely publishable analysis result."
        elif isinstance(exc, HTTPException) and isinstance(exc.detail, dict):
            code, message = (
                exc.detail.get("code", "ANALYSIS_PROVIDER_FAILED"),
                "The configured model is unavailable for this analysis.",
            )
        elif isinstance(exc, (ValidationError, ValueError, KeyError, TypeError)):
            code, message = (
                "ANALYSIS_VALIDATION_FAILED",
                "The analysis result or its source evidence failed validation.",
            )
        else:
            code, message = (
                "ANALYSIS_PROVIDER_FAILED",
                "The model could not analyze this bounded import unit.",
            )
        logger.warning("Import analysis unit failed code=%s unit_id=%s", code, fence.unit_id)
        try:
            fail_import_unit(vault, fence, code, message)
        except OperationalError as persistence_exc:
            logger.warning(
                "Import analysis failure could not be persisted unit_id=%s error=%s",
                fence.unit_id,
                type(persistence_exc).__name__,
            )
        return True
