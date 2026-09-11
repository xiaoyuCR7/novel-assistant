"""Dedicated quality workflow endpoints; ordinary chat history stays unchanged."""

from typing import Literal

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from novel_harness.api.dependencies import get_job_session, get_job_store
from novel_harness.schemas.quality import QualityAcceptance, QualityRunCreate
from novel_harness.services.quality import (
    accept_quality,
    approve_quality,
    enqueue_quality,
    quality_diff,
    require_quality,
)

router = APIRouter(prefix="/projects/{project_id}/quality/runs", tags=["quality"])


class QualityApproval(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    chapter_id: str = Field(min_length=1, max_length=128)
    confirmed: bool = False
    expected_control_revision: int = Field(ge=0)


@router.get("")
def list_runs(
    project_id: str,
    before: str | None = None,
    active_only: bool = False,
    limit: int = Query(20, ge=1, le=50),
    session: Session = Depends(get_job_session, scope="function"),
):
    from novel_harness.services.job_pages import read_page

    return read_page(session, project_id, None, "quality", active_only, limit, before)


@router.post("", status_code=202)
def create_run(
    project_id: str,
    payload: QualityRunCreate,
    request: Request,
    key: str = Header(alias="Idempotency-Key"),
):
    store = get_job_store(project_id, request)
    result = enqueue_quality(store, payload, key, request.app.state.model_settings.identity())
    request.app.state.job_executor.wake()
    return result


@router.get("/coverage")
def coverage(
    project_id: str,
    status: Literal['all', 'unchecked', 'stale', 'pending', 'confirmed'] = 'all',
    limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0),
    session: Session = Depends(get_job_session, scope="function"),
):
    from novel_harness.services.quality_coverage import read_coverage

    return read_coverage(session, project_id, status, limit, offset)


@router.get("/{job_id}")
def detail(
    project_id: str,
    job_id: str,
    request: Request,
    session: Session = Depends(get_job_session, scope="function"),
):
    require_quality(session, project_id, job_id)
    return get_job_store(project_id, request).serialize_in_session(session, job_id)


@router.post("/{job_id}/approve")
def approve(project_id: str, job_id: str, payload: QualityApproval, request: Request):
    return approve_quality(
        get_job_store(project_id, request),
        job_id,
        payload.chapter_id,
        payload.expected_control_revision,
        payload.confirmed,
    )


@router.post("/{job_id}/accept")
def accept(project_id: str, job_id: str, payload: QualityAcceptance, request: Request):
    return accept_quality(
        get_job_store(project_id, request), job_id, payload.chapter_id, payload.confirmed,
        payload.selected_hunk_ids,
    )


@router.get("/{job_id}/chapters/{chapter_id}/diff")
def diff(
    project_id: str,
    job_id: str,
    chapter_id: str,
    session: Session = Depends(get_job_session, scope="function"),
):
    return quality_diff(session, project_id, job_id, chapter_id)
