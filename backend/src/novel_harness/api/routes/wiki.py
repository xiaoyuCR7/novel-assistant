from typing import Literal

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from novel_harness.api.dependencies import get_job_store, get_session
from novel_harness.services import wiki, wiki_generation

router = APIRouter(prefix="/projects/{project_id}/wiki", tags=["wiki"])
WikiKind = Literal["entity", "plot", "timeline"]


class SynthesisRequest(BaseModel):
    chapter_id: str | None = None
    fingerprint: str = Field(min_length=64, max_length=64)
    confirm_unknown: bool = False


@router.get("")
def index(
    q: str = Query("", max_length=2000),
    kind: Literal["all", "entity", "plot", "timeline"] = "all",
    chapter_id: str | None = None,
    offset: int = Query(0, ge=0, le=100000),
    limit: int = Query(30, ge=1, le=100),
    session: Session = Depends(get_session, scope="function"),
):
    return wiki.index(session, q, kind, chapter_id, offset, limit)


@router.get("/{kind}/{item_id}")
def detail(
    project_id: str,
    kind: WikiKind,
    item_id: str,
    request: Request,
    chapter_id: str | None = None,
    session: Session = Depends(get_session, scope="function"),
):
    return wiki.detail(
        session, get_job_store(project_id, request), project_id, kind, item_id, chapter_id
    )


@router.post("/{kind}/{item_id}/summarize", status_code=202)
def summarize(
    project_id: str,
    kind: WikiKind,
    item_id: str,
    payload: SynthesisRequest,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key"),
):
    result = wiki_generation.enqueue(
        get_job_store(project_id, request),
        kind,
        item_id,
        payload.chapter_id,
        payload.fingerprint,
        idempotency_key,
        request.app.state.model_settings.identity(),
        payload.confirm_unknown,
    )
    request.app.state.job_executor.wake()
    return result
