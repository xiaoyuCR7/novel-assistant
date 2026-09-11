"""Durable AI receipts and side-effect-free task polling."""

from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from novel_harness.api.dependencies import get_job_session, get_job_store, verify_project_payload
from novel_harness.db.models import AIJob
from novel_harness.schemas.ai import AIJobCreate, JobAcceptance, TaskBudgetPreflight
from novel_harness.schemas.jobs import ResumeJob
from novel_harness.services.job_context import assert_source, capture_source
from novel_harness.services.job_state import ACTIVE
from novel_harness.services.pipeline import CreationPipeline
from novel_harness.services.serialization import serialize
from novel_harness.services.versions import require_chapter

router = APIRouter(prefix="/projects/{project_id}/ai/jobs", tags=["ai"])


def preparation(request, session, command):
    if command["chapter_id"]:
        from novel_harness.services.quality import assert_quality_available
        assert_quality_available(session, command["project_id"], command["chapter_id"])
    source = capture_source(session, command["project_id"], command["chapter_id"])
    if source["revision"] != command["expected_revision"]:
        raise HTTPException(409, detail={"code": "SOURCE_CHANGED"})
    embedding = request.app.state.embedding_provider
    return {
        "source_snapshot": source,
        "provider_identity": request.app.state.model_settings.identity(),
        "embedding_identity": (
            {"endpoint": embedding.endpoint, "model": embedding.model} if embedding else None
        ),
    }


def resume_validation(request, session, control):
    if control.effects.get("summary_id"):
        return
    if control.recovery_reason == "content_review_required":
        from novel_harness.services.drift import has_open_severe_content_conflicts

        revision = control.source_snapshot.get("revision")
        if has_open_severe_content_conflicts(session, control.job_id, revision):
            raise HTTPException(
                409,
                detail={
                    "code": "CONTENT_REVIEW_REQUIRED",
                    "message": "请先处理本章仍处于打开状态的严重内容问题。",
                },
            )
    job = session.get(AIJob, control.job_id)
    if job and job.task_type == "quality_workflow":
        from novel_harness.services.quality import validate_quality_resume
        validate_quality_resume(session, control)
    elif job and job.task_type == 'wiki_summary':
        from novel_harness.services import wiki
        from novel_harness.services.wiki_generation import assert_wiki_source
        assert_wiki_source(session, control.source_snapshot)
        active = wiki.active_job(session, job.project_id, job.instructions, job.id)
        if active:
            raise HTTPException(409, detail={'code':'WIKI_JOB_ACTIVE', 'job_id':active.id,
                'message':'该 Wiki 页面已有任务，请先处理当前任务。'})
    elif job and job.task_type in {"preparation_analysis", "preparation_followup"}:
        from novel_harness.services.preparation_generation import assert_preparation_source

        assert_preparation_source(session, control.source_snapshot)
    else:
        if job and job.chapter_id:
            from novel_harness.services.quality import assert_quality_available
            assert_quality_available(session, job.project_id, job.chapter_id)
        assert_source(session, control.source_snapshot)
    if request.app.state.model_settings.identity() != control.provider_identity:
        raise HTTPException(409, detail={"code": "PROVIDER_CHANGED"})


@router.get("")
def list_jobs(
    project_id: str,
    request: Request,
    chapter_id: str | None = None,
    conversation_id: str | None = Query(default=None, max_length=64),
    kind: Literal["writing", "summary"] = "writing",
    active_only: bool = False,
    session: Session = Depends(get_job_session, scope="function"),
):
    if chapter_id:
        require_chapter(session, chapter_id)
    store = get_job_store(project_id, request)
    scope = [
        AIJob.project_id == project_id,
        AIJob.chapter_id == chapter_id,
        AIJob.task_type == "chapter_summary"
        if kind == "summary"
        else AIJob.task_type.not_in(
            {
                "chapter_summary", "preparation_analysis", "preparation_followup",
                "wiki_summary", "quality_workflow",
            }
        ),
    ]
    if kind == "writing":
        from novel_harness.services import conversation_threads as threads
        thread = threads.resolve(session, project_id, conversation_id,
                                 chapter_id=chapter_id, check_scope=True)
        scope.append(threads.history_filter(session, thread))
    active = AIJob.status.in_(ACTIVE | {"recovery_required"})
    recent = select(AIJob.id).where(*scope).order_by(AIJob.created_at.desc()).limit(100)
    statement = select(AIJob).where(
        *scope,
        active if active_only else or_(active, AIJob.id.in_(recent)),
    )
    rows = session.scalars(statement.order_by(AIJob.created_at, AIJob.id)).all()
    return [store.serialize_in_session(session, row.id) for row in rows]


@router.get("/page")
def job_page(
    project_id: str,
    chapter_id: str | None = None,
    conversation_id: str | None = Query(default=None, max_length=64),
    kind: Literal["writing", "summary"] = "writing",
    active_only: bool = False,
    limit: int = Query(default=50, ge=1, le=100),
    before: str | None = Query(default=None, max_length=64),
    session: Session = Depends(get_job_session, scope="function"),
):
    from novel_harness.services.job_pages import read_page

    if chapter_id:
        require_chapter(session, chapter_id)
    return read_page(session, project_id, chapter_id, kind, active_only, limit, before,
                     conversation_id=conversation_id)


@router.post("", status_code=202)
def post_job(
    project_id: str,
    payload: AIJobCreate,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key"),
):
    verify_project_payload(project_id, payload.project_id)
    store = get_job_store(project_id, request)
    # Preserve old command hashes, including their existing nullable scope fields.
    command = payload.model_dump(exclude={"replaces_job_id", "confirm_unknown"})
    if payload.replaces_job_id is not None:
        command.update(
            replaces_job_id=payload.replaces_job_id, confirm_unknown=payload.confirm_unknown,
        )
    receipt = store.enqueue(
        command, idempotency_key, lambda session: preparation(request, session, command)
    )
    request.app.state.job_executor.wake()
    return receipt


@router.post("/preflight", response_model=TaskBudgetPreflight)
def preflight_job(
    project_id: str,
    payload: AIJobCreate,
    request: Request,
    session: Session = Depends(get_job_session, scope="function"),
):
    from novel_harness.ai.base import ExecutionLimits
    from novel_harness.services.task_budget import preflight

    verify_project_payload(project_id, payload.project_id)
    limits = ExecutionLimits.model_validate(request.app.state.model_settings.identity())
    from novel_harness.services.conversation_threads import assert_writable
    assert_writable(session, project_id, payload.chapter_id, payload.conversation_id)
    return preflight(session, payload, limits)


@router.get("/{job_id}")
def get_job(project_id: str, job_id: str, request: Request,
            conversation_id: str | None = Query(default=None, max_length=64)):
    store = get_job_store(project_id, request)
    if conversation_id is None:
        return store.read(job_id)
    from novel_harness.services import conversation_threads as threads
    with store.database.job_session_scope() as session:
        thread = threads.resolve(session, project_id, conversation_id)
        if not session.scalar(select(AIJob.id).where(
            AIJob.id == job_id, threads.history_filter(session, thread),
        )):
            raise HTTPException(404, detail={"code": "JOB_NOT_FOUND"})
        result = store.serialize_in_session(session, job_id)
        result["inherited"] = result["conversation_id"] != thread.id
        if result["inherited"]:
            result["allowed_actions"] = []
        return result


@router.post("/{job_id}/cancel")
def cancel_job(project_id: str, job_id: str, request: Request):
    from novel_harness.services.job_preview import previews

    store = get_job_store(project_id, request)
    result = store.cancel(job_id)
    previews.drop(str(store.database.path), job_id)
    return result


@router.get("/{job_id}/preview")
def job_preview(
    project_id: str,
    job_id: str,
    request: Request,
    session: Session = Depends(get_job_session, scope="function"),
):
    from novel_harness.services.job_preview import previews

    state = session.scalar(
        select(AIJob.status).where(AIJob.id == job_id, AIJob.project_id == project_id)
    )
    if state is None:
        raise HTTPException(404, detail={"code": "JOB_NOT_FOUND"})
    store = get_job_store(project_id, request)
    if state != "running":
        previews.drop(str(store.database.path), job_id)
    return {**previews.read(str(store.database.path), job_id), "job_id": job_id, "status": state}


@router.post("/{job_id}/resume", status_code=202)
def resume_job(
    project_id: str,
    job_id: str,
    payload: ResumeJob,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key"),
):
    result = get_job_store(project_id, request).resume(
        job_id,
        idempotency_key,
        payload.expected_control_revision,
        payload.confirm_unknown,
        lambda session, control: resume_validation(request, session, control),
    )
    request.app.state.job_executor.wake()
    return result


@router.post("/{job_id}/accept", status_code=201)
def post_accept(
    job_id: str,
    payload: JobAcceptance | None = None,
    session: Session = Depends(get_job_session, scope="function"),
):
    return serialize(
        CreationPipeline.accept(session, job_id, confirmed=bool(payload and payload.confirmed))
    )
