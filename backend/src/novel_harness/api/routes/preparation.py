"""Optional, author-controlled project-preparation interview endpoints."""

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.orm import Session

from novel_harness.api.dependencies import get_job_store, get_session
from novel_harness.schemas.preparation import (
    PreparationQuestionCommand,
    PreparationRevisionCommand,
    ProjectPreparationRead,
)
from novel_harness.services.preparation import (
    acknowledge_impact,
    initialize_preparation,
    mutate_question,
    read_preparation,
    resume_preparation,
    skip_preparation,
)
from novel_harness.services.preparation_generation import prepare_preparation_job

router = APIRouter(prefix="/projects/{project_id}/preparation", tags=["preparation"])


@router.get("", response_model=ProjectPreparationRead)
def get_preparation(project_id: str, session: Session = Depends(get_session, scope="function")):
    return read_preparation(session, project_id)


@router.post("/initialize", response_model=ProjectPreparationRead)
def initialize(project_id: str, session: Session = Depends(get_session, scope="function")):
    return initialize_preparation(session, project_id)


@router.patch("/questions/{question_id}", response_model=ProjectPreparationRead)
def patch_question(
    project_id: str,
    question_id: str,
    payload: PreparationQuestionCommand,
    session: Session = Depends(get_session, scope="function"),
):
    return mutate_question(session, project_id, question_id, payload)


@router.post("/skip", response_model=ProjectPreparationRead)
def skip(
    project_id: str,
    payload: PreparationRevisionCommand,
    session: Session = Depends(get_session, scope="function"),
):
    return skip_preparation(session, project_id, payload)


@router.post("/resume", response_model=ProjectPreparationRead)
def resume(
    project_id: str,
    payload: PreparationRevisionCommand,
    session: Session = Depends(get_session, scope="function"),
):
    return resume_preparation(session, project_id, payload)


@router.post("/impact/acknowledge", response_model=ProjectPreparationRead)
def acknowledge(
    project_id: str,
    payload: PreparationRevisionCommand,
    session: Session = Depends(get_session, scope="function"),
):
    return acknowledge_impact(session, project_id, payload)


def _enqueue(project_id: str, task_type: str, request: Request, idempotency_key: str):
    store = get_job_store(project_id, request)
    command = {
        "project_id": project_id,
        "chapter_id": None,
        "task_type": task_type,
        "instructions": "只发现高影响设定缺口，不替作者回答。",
        "token_budget": 12_000,
        "expected_revision": None,
    }
    result = store.enqueue(
        command,
        idempotency_key,
        lambda session: prepare_preparation_job(
            session,
            project_id,
            task_type,
            request.app.state.model_settings.identity(),
        ),
    )
    request.app.state.job_executor.wake()
    return result


@router.post("/analyze", status_code=202)
def analyze(
    project_id: str,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key"),
):
    return _enqueue(project_id, "preparation_analysis", request, idempotency_key)


@router.post("/follow-up", status_code=202)
def follow_up(
    project_id: str,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key"),
):
    return _enqueue(project_id, "preparation_followup", request, idempotency_key)
