"""Story knowledge routes."""

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from novel_harness.api.dependencies import get_session
from novel_harness.db.models import Entity, Idea, StyleProfile
from novel_harness.schemas.story import (
    CanonFactCreate,
    EntityCreate,
    IdeaCreate,
    NodeCreate,
    PlotThreadCreate,
    RelationCreate,
    StyleProfileCreate,
    TimelineEventCreate,
)
from novel_harness.services.feedback import active_style_rules
from novel_harness.services.serialization import serialize
from novel_harness.services.story import (
    add_canon,
    add_node,
    add_plot,
    add_record,
    add_relation,
    add_timeline,
)

router = APIRouter(prefix="/projects/{project_id}", tags=["story"])


@router.post("/ideas", status_code=status.HTTP_201_CREATED)
def post_idea(
    project_id: str, payload: IdeaCreate, session: Session = Depends(get_session, scope="function")
):
    return serialize(add_record(session, project_id, Idea, payload.model_dump()))


@router.post("/nodes", status_code=status.HTTP_201_CREATED)
def post_node(
    project_id: str, payload: NodeCreate, session: Session = Depends(get_session, scope="function")
):
    return serialize(add_node(session, project_id, payload.model_dump()))


@router.post("/entities", status_code=status.HTTP_201_CREATED)
def post_entity(
    project_id: str,
    payload: EntityCreate,
    session: Session = Depends(get_session, scope="function"),
):
    return serialize(add_record(session, project_id, Entity, payload.model_dump()))


@router.post("/relations", status_code=status.HTTP_201_CREATED)
def post_relation(
    project_id: str,
    payload: RelationCreate,
    session: Session = Depends(get_session, scope="function"),
):
    return serialize(add_relation(session, project_id, payload.model_dump()))


@router.post("/canon", status_code=status.HTTP_201_CREATED)
def post_canon(
    project_id: str,
    payload: CanonFactCreate,
    session: Session = Depends(get_session, scope="function"),
):
    return serialize(add_canon(session, project_id, payload.model_dump()))


@router.post("/timeline", status_code=status.HTTP_201_CREATED)
def post_timeline(
    project_id: str,
    payload: TimelineEventCreate,
    session: Session = Depends(get_session, scope="function"),
):
    return serialize(add_timeline(session, project_id, payload.model_dump()))


@router.post("/plots", status_code=status.HTTP_201_CREATED)
def post_plot(
    project_id: str,
    payload: PlotThreadCreate,
    session: Session = Depends(get_session, scope="function"),
):
    return serialize(add_plot(session, project_id, payload.model_dump()))


@router.post("/styles", status_code=status.HTTP_201_CREATED)
def post_style(
    project_id: str,
    payload: StyleProfileCreate,
    session: Session = Depends(get_session, scope="function"),
):
    return serialize(add_record(session, project_id, StyleProfile, payload.model_dump()))


@router.get("/styles/active-context")
def get_active_style_context(
    project_id: str, session: Session = Depends(get_session, scope="function")
):
    return {"rules": [serialize(rule) for rule in active_style_rules(session, project_id)]}
