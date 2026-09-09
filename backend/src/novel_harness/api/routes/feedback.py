"""Feedback, preference, and conflict routes."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from novel_harness.api.dependencies import get_session, verify_project_payload
from novel_harness.db.models import Feedback
from novel_harness.schemas.feedback import (
    ConflictCreate,
    ConflictDecision,
    EntityStateConflictResolution,
    FeedbackCreate,
    PreferenceConfirmation,
)
from novel_harness.services.feedback import (
    confirm_candidate,
    create_conflict,
    create_feedback,
    decide_conflict,
    disable_candidate,
    list_conflicts_with_options,
    list_preference_candidates,
    present_candidate,
    resolve_entity_state_conflict,
    suggest_options,
)
from novel_harness.services.serialization import serialize

router = APIRouter(prefix="/projects/{project_id}", tags=["feedback", "conflicts"])


def present_conflict(item: dict) -> dict:
    result = {
        **serialize(item["conflict"]),
        "options": [serialize(option) for option in item["options"]],
    }
    resolution = item.get("entity_state_resolution")
    if resolution is not None:
        result["entity_state_resolution"] = {
            "conflicts": resolution["conflicts"],
            "states": [serialize(state) for state in resolution["states"]],
        }
    if "resolved" in item:
        result["resolved"] = item["resolved"]
    return result


class AlertDecision(BaseModel):
    decision: Literal["accept", "dismiss"]
    confirmed: bool = False


@router.post("/chapters/{chapter_id}/drift-check")
def check_drift(chapter_id: str, session: Session = Depends(get_session, scope="function")):
    from novel_harness.services.drift import check_drift

    return check_drift(session, chapter_id)


@router.post("/conflicts/{conflict_id}/decision")
def decide_alert(
    conflict_id: str,
    payload: AlertDecision,
    session: Session = Depends(get_session, scope="function"),
):
    from novel_harness.services.drift import decide_alert

    return decide_alert(session, conflict_id, payload.decision, payload.confirmed)


@router.post("/feedback", status_code=status.HTTP_201_CREATED)
def post_feedback(
    project_id: str,
    payload: FeedbackCreate,
    session: Session = Depends(get_session, scope="function"),
):
    verify_project_payload(project_id, payload.project_id)
    feedback, candidate = create_feedback(session, payload.model_dump())
    return {
        "feedback": serialize(feedback),
        "preference_candidate": present_candidate(candidate),
    }


@router.get("/feedback/{feedback_id}")
def get_feedback(feedback_id: str, session: Session = Depends(get_session, scope="function")):
    feedback = session.get(Feedback, feedback_id)
    if feedback is None:
        raise HTTPException(status_code=404, detail={"code": "FEEDBACK_NOT_FOUND"})
    return serialize(feedback)


@router.post("/feedback/preferences/{candidate_id}/confirm")
def post_confirm(
    candidate_id: str,
    payload: PreferenceConfirmation | None = None,
    session: Session = Depends(get_session, scope="function"),
):
    return present_candidate(confirm_candidate(
        session, candidate_id, payload.instruction if payload else None,
    ))


@router.post("/feedback/preferences/{candidate_id}/disable")
def post_disable(candidate_id: str, session: Session = Depends(get_session, scope="function")):
    return present_candidate(disable_candidate(session, candidate_id))


@router.get("/preferences")
def get_preferences(project_id: str, session: Session = Depends(get_session, scope="function")):
    return [present_candidate(item) for item in list_preference_candidates(session, project_id)]


@router.get("/conflicts")
def get_conflicts(
    project_id: str,
    status_filter: Literal["open", "decided", "accepted", "dismissed"] | None = Query(
        "open", alias="status"
    ),
    limit: int = Query(200, ge=1, le=200),
    session: Session = Depends(get_session, scope="function"),
):
    return [
        present_conflict(item)
        for item in list_conflicts_with_options(
            session, project_id, status=status_filter, limit=limit
        )
    ]


@router.post("/chapters/{chapter_id}/conflicts", status_code=status.HTTP_201_CREATED)
def post_conflict(
    chapter_id: str,
    payload: ConflictCreate,
    session: Session = Depends(get_session, scope="function"),
):
    return serialize(create_conflict(session, chapter_id, payload.model_dump()))


@router.post("/conflicts/{conflict_id}/suggest-options", status_code=status.HTTP_201_CREATED)
def post_options(conflict_id: str, session: Session = Depends(get_session, scope="function")):
    return [serialize(option) for option in suggest_options(session, conflict_id)]


@router.post("/conflicts/{conflict_id}/decide")
def post_decision(
    conflict_id: str,
    payload: ConflictDecision,
    session: Session = Depends(get_session, scope="function"),
):
    return serialize(decide_conflict(session, conflict_id, payload.option_id, payload.note))


@router.post("/conflicts/{conflict_id}/resolve-entity-state")
def post_entity_state_resolution(
    conflict_id: str,
    payload: EntityStateConflictResolution,
    session: Session = Depends(get_session, scope="function"),
):
    return present_conflict(
        resolve_entity_state_conflict(
            session,
            conflict_id,
            payload.state_id,
            payload.revision,
            payload.note,
        )
    )
