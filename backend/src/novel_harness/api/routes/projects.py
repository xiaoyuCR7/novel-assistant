"""Project routes."""

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.orm import Session

from novel_harness.api.dependencies import get_session
from novel_harness.schemas.projects import ProjectCreate, ProjectRead
from novel_harness.services.projects import (
    export_project,
    project_progress,
)
from novel_harness.services.story import navigation, workspace, workspace_view

router = APIRouter(prefix="/projects", tags=["projects"])


@router.get("")
def get_projects(request: Request):
    return request.app.state.vault_registry.list_records()


@router.post("", response_model=ProjectRead, status_code=status.HTTP_201_CREATED)
def post_project(payload: ProjectCreate, request: Request):
    return request.app.state.vault_registry.create_project(payload)


@router.get("/{project_id}/workspace")
def get_workspace(
    project_id: str, request: Request, session: Session = Depends(get_session, scope="function")
) -> dict:
    request.app.state.vault_registry.touch(project_id)
    return workspace(session, project_id)


@router.get("/{project_id}/workspace/navigation")
def get_navigation(
    project_id: str, request: Request, session: Session = Depends(get_session, scope="function")
) -> dict:
    request.app.state.vault_registry.touch(project_id)
    return navigation(session, project_id)


@router.get("/{project_id}/workspace/views/{view}")
def get_workspace_view(
    project_id: str, view: str, session: Session = Depends(get_session, scope="function")
) -> dict:
    return workspace_view(session, project_id, view)


@router.get("/{project_id}/progress")
def get_progress(
    project_id: str, session: Session = Depends(get_session, scope="function")
) -> dict:
    return project_progress(session, project_id)


@router.get("/{project_id}/export")
def get_export(
    project_id: str, request: Request, session: Session = Depends(get_session, scope="function")
) -> Response:
    content = export_project(session, project_id, session.info["vault_root"])
    return Response(
        content=content,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="novel-{project_id}.zip"'},
    )
