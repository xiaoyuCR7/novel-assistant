"""Editable goals do not generate text or mark chapters as delivered."""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from novel_harness.api.dependencies import get_job_session, get_job_store
from novel_harness.schemas.writing_goals import WritingGoalsUpdate
from novel_harness.services.writing_goals import read_writing_goals, update_writing_goals

router = APIRouter(prefix="/projects/{project_id}/writing-goals", tags=["writing-goals"])


@router.get("")
def read(project_id: str, session: Session = Depends(get_job_session, scope="function")):
    return read_writing_goals(session, project_id)


@router.put("")
def update(project_id: str, payload: WritingGoalsUpdate, request: Request):
    with get_job_store(project_id, request).write() as session:
        return update_writing_goals(session, project_id, payload)
