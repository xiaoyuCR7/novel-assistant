"""Conversation navigation never generates text or removes job/version evidence."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from novel_harness.api.dependencies import get_job_session, get_job_store
from novel_harness.services import conversation_threads as threads
from novel_harness.services.versions import require_chapter

router = APIRouter(prefix="/projects/{project_id}/conversations", tags=["conversations"])


class CreateConversation(BaseModel):
    id: UUID
    chapter_id: str | None = None
    title: str = Field(default="新会话", min_length=1, max_length=240)

    @field_validator("title")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("会话名称不能为空")
        return value.strip()


class BranchConversation(CreateConversation):
    from_job_id: str = Field(min_length=1, max_length=64)


class UpdateConversation(BaseModel):
    expected_revision: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=240)
    status: Literal["active", "archived", "deleted"] | None = None

    @field_validator("title")
    @classmethod
    def nonblank(cls, value):
        if value is not None and not value.strip():
            raise ValueError("会话名称不能为空")
        return value.strip() if value is not None else value


@router.get("")
def list_conversations(
    project_id: str,
    chapter_id: str | None = None,
    status: Literal["active", "archived", "deleted"] = "active",
    q: str = Query(default="", max_length=200),
    limit: int = Query(default=30, ge=1, le=100),
    before: str | None = Query(default=None, max_length=64),
    session: Session = Depends(get_job_session, scope="function"),
):
    if chapter_id:
        require_chapter(session, chapter_id)
    return threads.list_threads(session, project_id, chapter_id, status, q.strip(), limit, before)


@router.get("/{conversation_id}")
def detail(
    project_id: str,
    conversation_id: str,
    session: Session = Depends(get_job_session, scope="function"),
):
    return threads.describe(session, threads.resolve(session, project_id, conversation_id))


@router.post("", status_code=201)
def create(project_id: str, payload: CreateConversation, request: Request):
    with get_job_store(project_id, request).write() as session:
        if payload.chapter_id:
            require_chapter(session, payload.chapter_id)
        return threads.create(
            session, project_id, str(payload.id), payload.chapter_id, payload.title
        )


@router.patch("/{conversation_id}")
def update(project_id: str, conversation_id: str, payload: UpdateConversation, request: Request):
    with get_job_store(project_id, request).write() as session:
        return threads.update(
            session,
            project_id,
            conversation_id,
            payload.expected_revision,
            title=payload.title,
            status=payload.status,
        )


@router.post("/{conversation_id}/branches", status_code=201)
def branch(project_id: str, conversation_id: str, payload: BranchConversation, request: Request):
    with get_job_store(project_id, request).write() as session:
        parent = threads.resolve(session, project_id, conversation_id)
        if parent.chapter_id:
            require_chapter(session, parent.chapter_id)
        return threads.create(
            session,
            project_id,
            str(payload.id),
            parent.chapter_id,
            payload.title,
            parent_id=parent.id,
            from_job_id=payload.from_job_id,
        )
