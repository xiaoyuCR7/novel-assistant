"""Project routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile, status
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from novel_harness.api.dependencies import get_session
from novel_harness.schemas.projects import ManuscriptExportRequest, ProjectCreate, ProjectRead
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


@router.post("/backup/preview")
async def preview_backup(request: Request, file: Annotated[UploadFile, File()]):
    from novel_harness.services.backup_restore import inspect_backup

    try:
        return await run_in_threadpool(inspect_backup, file.file, request.app.state.vault_registry)
    finally:
        await file.close()


@router.post("/backup/restore", status_code=201)
async def post_restore_backup(
    request: Request, file: Annotated[UploadFile, File()],
    archive_hash: Annotated[str, Form(pattern=r"^[0-9a-f]{64}$")],
):
    from novel_harness.services.backup_restore import restore_backup

    try:
        return await run_in_threadpool(
            restore_backup, file.file, archive_hash, request.app.state.vault_registry,
        )
    finally:
        await file.close()


@router.post("/{project_id}/manuscript/export")
def manuscript_export(
    project_id: str, payload: ManuscriptExportRequest,
    session: Session = Depends(get_session, scope="function"),
):
    from novel_harness.services.manuscript_export import export_manuscript

    content = export_manuscript(session, project_id, payload)
    extension = "md" if payload.format == "markdown" else "txt"
    return Response(content, media_type="text/markdown" if extension == "md" else "text/plain",
                    headers={"Content-Disposition":
                             f'attachment; filename="manuscript-{project_id}.{extension}"'})


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


@router.get("/{project_id}/todos")
def project_todos(
    project_id: str, limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0),
    session: Session = Depends(get_session, scope="function"),
):
    from novel_harness.services.project_todos import read_todos

    return read_todos(session, project_id, limit, offset)


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
