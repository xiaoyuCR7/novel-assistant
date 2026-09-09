"""Durable chapter completion with rebuildable, atomically replaced continuity ledger."""

import hashlib
import json
import os
import tempfile
from time import monotonic

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select, update

from novel_harness.db.models import ChapterDocument, ChapterSummary, ChapterVersion, StoryNode
from novel_harness.services.serialization import serialize
from novel_harness.services.versions import (
    begin_version_write,
    create_version,
    get_or_create_document,
    require_chapter,
)


def current_summary(session, chapter_id):
    return session.scalar(
        select(ChapterSummary)
        .where(ChapterSummary.chapter_id == chapter_id)
        .order_by(ChapterSummary.created_at.desc(), ChapterSummary.id.desc())
    )


def write_ledger(session):
    if not session.info.get("projecting_ledger"):
        from novel_harness.services.projections import queue_ledger

        queue_ledger(session)
        return
    _write_ledger_file(session)


def ledger_text(session):
    from novel_harness.services.retrieval import ordered_chapters
    from novel_harness.services.summary_projection import story_summary_details

    order = {node.id: i for i, node in enumerate(ordered_chapters(session))}
    summaries = session.scalars(
        select(ChapterSummary)
        .join(StoryNode, ChapterSummary.chapter_id == StoryNode.id)
        .where(ChapterSummary.status == "valid")
    ).all()
    summaries = sorted(summaries, key=lambda item: order.get(item.chapter_id, -1))
    blocks = ["# 章节连续性账本\n\n由数据库重建的软参考；AI 总结不等于作者确认事实。\n"]
    for summary in summaries:
        label = "作者已编辑" if summary.origin == "author_edited" else "AI 总结"
        if summary.provider == "demo":
            label += " · 离线演示摘录（非模型推理）"
        blocks.append(
            f"\n## {summary.title}\n\n{label}\n\n"
            f"chapter_id: {summary.chapter_id}\nversion_id: {summary.version_id}\n"
            f"sha256: {summary.content_hash}\nrevision: {summary.revision}\n"
            f"generated_at: {summary.created_at}\n\n{summary.recap}\n\n"
            + json.dumps(story_summary_details(summary.details), ensure_ascii=False, indent=2)
            + "\n"
        )
    return "".join(blocks)


def _write_ledger_file(session):
    rendered = ledger_text(session)
    root = session.info["vault_root"] / "rag"
    root.mkdir(parents=True, exist_ok=True)
    path = root / "chapter-continuity-ledger.md"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=root, prefix=".ledger-", suffix=".tmp", delete=False
        ) as file:
            temporary = file.name
            file.write(rendered)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def invalidate_summary(session, chapter_id):
    for summary in session.scalars(
        select(ChapterSummary)
        .execution_options(include_deleted=True)
        .where(ChapterSummary.chapter_id == chapter_id, ChapterSummary.status == "valid")
    ):
        summary.status = "stale"
    chapter = require_chapter(session, chapter_id)
    chapter.status = "drafting"
    session.flush()
    write_ledger(session)


def prepare_completion(session, chapter_id, expected_revision):
    from novel_harness.services.job_context import capture_summary_source

    begin_version_write(session)
    chapter = require_chapter(session, chapter_id)
    document = get_or_create_document(session, chapter_id)
    if document.revision != expected_revision:
        raise HTTPException(409, detail={"code": "SUMMARY_SOURCE_CHANGED"})
    if not document.content.strip():
        raise HTTPException(422, detail={"code": "EMPTY_CHAPTER"})
    version = (
        session.get(ChapterVersion, document.current_version_id)
        if (document.current_version_id)
        else None
    )
    if version is None or version.content != document.content:
        create_version(
            session, chapter_id, content=document.content, source="manual", summary="完成章节快照"
        )
    chapter.status = "summary_pending"
    session.flush()
    return capture_summary_source(session, chapter.project_id, chapter_id)


def generate_summary(store, fence, provider_factory, *, validate_provider=None):
    from copy import deepcopy

    from novel_harness.ai.base import ExecutionLimits
    from novel_harness.db.job_models import AIJobControl
    from novel_harness.services.job_context import assert_source, capture_summary_source
    from novel_harness.services.job_stages import StageRunner
    from novel_harness.services.job_state import command_hash
    from novel_harness.services.pipeline import CreationPipeline

    with store.database.job_session_scope() as session:
        job = store._job(session, fence.job_id)
        control = session.get(AIJobControl, job.id)
        source = deepcopy(control.source_snapshot)
        limits = ExecutionLimits.model_validate(control.provider_identity).model_dump()
        published = bool(control.effects.get("summary_id"))
        previous = current_summary(session, job.chapter_id)
        digest = hashlib.sha256(source["content"].encode()).hexdigest()
        check_context_hash = command_hash(source["content_check_context"])
        previous_check = previous.details.get("content_check", {}) if previous else {}
        reuse = bool(
            previous
            and previous.status == "valid"
            and previous.content_hash == digest
            and previous_check.get("version") == 1
            and previous_check.get("context_hash") == check_context_hash
        )

    if not published:

        def guard():
            if validate_provider:
                validate_provider()
            with store.database.job_session_scope() as session:
                store.assert_running(session, fence)
                assert_source(session, source)

        guard()
        result = None
        stage_runner = None
        started = monotonic()
        if not reuse:
            from novel_harness.services.summary_generation import generate

            pipeline = CreationPipeline(None)
            pipeline.provider_factory = provider_factory
            stage_runner = StageRunner(store, fence, guard)
            result = generate(
                source["content"],
                job.token_budget,
                stage_runner,
                pipeline._invoke,
                limits,
                source["content_check_context"],
            )
        execution = {
            **(stage_runner.provider_metadata if stage_runner else {"stages": {}}),
            "prompt_version": job.prompt_version,
            "duration_ms": round((monotonic() - started) * 1000),
            "summary_reused": reuse,
        }

        if not reuse:
            check_state = {}

            def persist_check(session, current):
                from novel_harness.services.drift import (
                    collect_local_findings,
                    has_open_severe_content_conflicts,
                    persist_content_check,
                )

                assert_source(session, source)
                live_check_context = capture_summary_source(
                    session, current.project_id, current.chapter_id
                )["content_check_context"]
                data = result["data"]
                audit = result.get("content_check_audit", {})
                findings = [
                    *collect_local_findings(session, current.chapter_id),
                    *audit.get("findings", data["content_findings"]),
                ]
                persisted = persist_content_check(
                    session,
                    current.chapter_id,
                    current.id,
                    source["revision"],
                    source["content"],
                    findings,
                    audit.get("observations", data["content_observations"]),
                    live_check_context,
                )
                check_state["blocked"] = has_open_severe_content_conflicts(
                    session, current.id, source["revision"]
                )
                store.local_checkpoint(
                    session,
                    current,
                    "content_check",
                    {
                        "version": 1,
                        "context_hash": check_context_hash,
                        "conflict_ids": [item.id for item in persisted],
                    },
                )

            guard()
            store.publish(fence, persist_check, terminal=False)
            if check_state.get("blocked"):
                store.pause(fence, "content_review_required")
                return

        def publish(session, current):
            assert_source(session, source)
            latest = current_summary(session, current.chapter_id)
            if latest and latest.status == "valid" and latest.content_hash == digest:
                summary = latest  # Preserve author edits and same-hash summaries.
            else:
                if result is None:
                    raise HTTPException(409, detail={"code": "SOURCE_CHANGED"})
                for old in session.scalars(
                    select(ChapterSummary).where(
                        ChapterSummary.chapter_id == current.chapter_id,
                    )
                ):
                    old.status = "superseded"
                summary_details = dict(result["data"])
                findings = summary_details.pop("content_findings")
                observations = summary_details.pop("content_observations")
                summary_details["content_check"] = {
                    "version": 1,
                    "context_hash": check_context_hash,
                    "finding_count": len(
                        result.get("content_check_audit", {}).get("findings", findings)
                    ),
                    "observation_count": len(
                        result.get("content_check_audit", {}).get("observations", observations)
                    ),
                }
                summary = ChapterSummary(
                    project_id=current.project_id,
                    chapter_id=current.chapter_id,
                    version_id=source["version_id"],
                    title=require_chapter(session, current.chapter_id).title,
                    content_hash=digest,
                    recap=result["data"]["recap"],
                    details=summary_details,
                    provider=result["provider"],
                    supersedes_id=latest.id if latest else None,
                )
                session.add(summary)
                session.flush()
            candidates = []
            proposals = result["data"].get("memory_candidates", []) if result else []
            if proposals:
                from novel_harness.services.memory_candidates import (
                    persist_candidates,
                    serialize_candidate,
                )

                version = session.get(ChapterVersion, summary.version_id)
                candidates = persist_candidates(
                    session,
                    source_version=version,
                    proposals=proposals,
                    source_summary=summary,
                    source_job=current,
                )
                candidate_refs = [serialize_candidate(item) for item in candidates]
            else:
                candidate_refs = []
            control = session.get(AIJobControl, current.id)
            control.effects = {
                "summary_id": summary.id,
                "ledger_pending": True,
                "memory_candidate_ids": [item.id for item in candidates],
            }
            current.result = {
                "summary_id": summary.id,
                "chapter_status": "summary_pending",
                "pending_canon_changes": candidate_refs,
                "execution": execution,
            }
            store.local_checkpoint(session, current, "summary_publish", control.effects)

        guard()
        store.publish(fence, publish, terminal=False)

    def project_ledger(session, current):
        repair_ledger(session, current.chapter_id)

    try:
        store.publish(fence, project_ledger, terminal=False)
        with store.database.job_session_scope() as session:
            pending = session.get(AIJobControl, fence.job_id).effects.get("ledger_pending")
        if pending:
            store.pause(fence, "ledger_pending")
        else:
            store.publish(fence, lambda session, current: None)
    except OSError:
        store.pause(fence, "ledger_pending")


def repair_ledger(session, chapter_id):
    """Local-only repair from current SQL state; usable even after cancellation."""
    chapter = require_chapter(session, chapter_id)
    summary = current_summary(session, chapter_id)
    write_ledger(session)
    return {"chapter_status": chapter.status, "summary": serialize(summary) if summary else None}


def finish_ledger(session):
    """Acknowledge only after projecting committed data successfully."""
    from novel_harness.db.job_models import AIJobControl
    from novel_harness.db.models import AIJob
    from novel_harness.services.job_store import JobStore

    latest = select(
        ChapterSummary.chapter_id,
        ChapterSummary.status,
        ChapterSummary.content_hash,
        func.row_number().over(
            partition_by=ChapterSummary.chapter_id,
            order_by=(ChapterSummary.created_at.desc(), ChapterSummary.id.desc()),
        ).label("position"),
    ).where(ChapterSummary.deleted_at.is_(None)).subquery()
    chapters = session.execute(
        select(StoryNode, ChapterDocument.content, latest.c.content_hash)
        .join(ChapterDocument, ChapterDocument.chapter_id == StoryNode.id)
        .join(latest, latest.c.chapter_id == StoryNode.id)
        .where(
            StoryNode.kind == "chapter", StoryNode.status != "completed",
            latest.c.position == 1, latest.c.status == "valid",
        )
    )
    for chapter, content, content_hash in chapters:
        if content_hash == hashlib.sha256(content.encode()).hexdigest():
            chapter.status = "completed"
    for job, control, chapter in session.execute(
        select(AIJob, AIJobControl, StoryNode)
        .join(AIJobControl, AIJobControl.job_id == AIJob.id)
        .join(StoryNode, StoryNode.id == AIJob.chapter_id)
        .where(
            AIJob.task_type == "chapter_summary",
            AIJobControl.effects["ledger_pending"].as_boolean().is_(True),
        )
    ):
        control.effects = {**control.effects, "ledger_pending": False}
        job.result = {**job.result, "chapter_status": chapter.status}
        JobStore.local_checkpoint(session, job, "ledger_projection", control.effects)
        if job.status == "recovery_required" and control.recovery_reason == "ledger_pending":
            job.status = "succeeded"
            control.recovery_reason = None
            control.control_revision += 1


def edit_summary(session, chapter_id, payload):
    summary = current_summary(session, chapter_id)
    if not summary:
        raise HTTPException(404, detail={"code": "SUMMARY_NOT_FOUND"})
    if summary.id != payload.summary_id or summary.revision != payload.revision:
        raise HTTPException(
            409,
            detail={"code": "revision_conflict", "current": jsonable_encoder(serialize(summary))},
        )
    if summary.status != "valid":
        raise HTTPException(409, detail={"code": "SUMMARY_STALE"})
    details = payload.details.model_dump() if payload.details else dict(summary.details)
    if "content_check" in summary.details:
        details["content_check"] = summary.details["content_check"]
    details["recap"] = payload.recap
    result = session.execute(
        update(ChapterSummary)
        .where(
            ChapterSummary.id == summary.id,
            ChapterSummary.revision == payload.revision,
            ChapterSummary.status == "valid",
            ChapterSummary.deleted_at.is_(None),
        )
        .values(
            recap=payload.recap,
            details=details,
            origin="author_edited",
            revision=payload.revision + 1,
        )
        .execution_options(synchronize_session=False)
    )
    session.refresh(summary)
    if result.rowcount != 1:
        raise HTTPException(
            409,
            detail={"code": "revision_conflict", "current": jsonable_encoder(serialize(summary))},
        )
    from novel_harness.services.search_index import sync_record

    sync_record(session, summary)
    write_ledger(session)
    return serialize(summary)
