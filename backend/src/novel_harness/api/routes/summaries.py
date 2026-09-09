from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from novel_harness.api.dependencies import get_job_session, get_job_store, get_session
from novel_harness.schemas.jobs import CompleteChapter, RetrySummary
from novel_harness.schemas.summaries import SummaryPatch
from novel_harness.services.chapter_summaries import (
    current_summary,
    edit_summary,
    prepare_completion,
    repair_ledger,
)
from novel_harness.services.serialization import serialize
from novel_harness.services.versions import require_chapter

router = APIRouter(prefix="/projects/{project_id}/chapters/{chapter_id}", tags=["summaries"])


@router.get("/summary")
def get_summary(chapter_id: str, session: Session = Depends(get_session, scope="function")):
    require_chapter(session, chapter_id)
    summary = current_summary(session, chapter_id)
    return {**serialize(summary), 'ledger_pending': bool(session.scalar(text(
        "SELECT kind FROM pending_projections WHERE kind='ledger'"
    )))} if summary else None


@router.post("/complete", status_code=202)
def complete(
    project_id: str,
    chapter_id: str,
    request: Request,
    payload: CompleteChapter,
    idempotency_key: str = Header(alias="Idempotency-Key"),
):
    store = get_job_store(project_id, request)
    command = dict(
        project_id=project_id,
        chapter_id=chapter_id,
        task_type="chapter_summary",
        instructions="",
        token_budget=12000,
        expected_revision=payload.expected_revision,
    )
    if payload.replaces_job_id is not None:
        command.update(
            replaces_job_id=payload.replaces_job_id, confirm_unknown=payload.confirm_unknown,
        )
    result = store.enqueue(
        command,
        idempotency_key,
        lambda session: {
            "source_snapshot": prepare_completion(session, chapter_id, payload.expected_revision),
            "provider_identity": request.app.state.model_settings.identity(),
            "embedding_identity": None,
        },
    )
    request.app.state.job_executor.wake()
    return result


@router.post("/summary/retry", status_code=202)
def retry(
    project_id: str,
    chapter_id: str,
    request: Request,
    payload: RetrySummary,
    idempotency_key: str = Header(alias="Idempotency-Key"),
):
    from novel_harness.api.routes.ai import resume_validation

    store = get_job_store(project_id, request)
    job = store.read(payload.job_id)
    if job["chapter_id"] != chapter_id or job["task_type"] != "chapter_summary":
        raise HTTPException(404, detail={"code": "JOB_NOT_FOUND"})
    result = store.resume(
        payload.job_id,
        idempotency_key,
        payload.expected_control_revision,
        payload.confirm_unknown,
        lambda session, control: resume_validation(request, session, control),
    )
    request.app.state.job_executor.wake()
    return result


@router.post("/summary/repair-ledger")
def repair(chapter_id: str, session: Session = Depends(get_job_session, scope="function")):
    from novel_harness.services.versions import begin_version_write

    begin_version_write(session)
    return repair_ledger(session, chapter_id)


@router.patch("/summary")
def patch_summary(
    chapter_id: str,
    payload: SummaryPatch,
    session: Session = Depends(get_session, scope="function"),
):
    return edit_summary(session, chapter_id, payload)
