"""Asset generation and listing routes."""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_harness.api.dependencies import get_session, verify_project_payload
from novel_harness.db.models import Asset
from novel_harness.schemas.ai import AssetGenerateRequest
from novel_harness.services.assets import generate_asset
from novel_harness.services.model_settings import selected_provider
from novel_harness.services.projects import require_project
from novel_harness.services.serialization import serialize

router = APIRouter(prefix="/projects/{project_id}", tags=["assets"])


@router.post("/assets/generate", status_code=status.HTTP_201_CREATED)
def post_generate_asset(
    project_id: str,
    payload: AssetGenerateRequest,
    request: Request,
    session: Session = Depends(get_session, scope="function"),
):
    verify_project_payload(project_id, payload.project_id)
    return serialize(
        generate_asset(
            session,
            selected_provider(request),
            session.info["vault_root"],
            **payload.model_dump(),
        )
    )


@router.get("/assets")
def get_assets(project_id: str, session: Session = Depends(get_session, scope="function")):
    require_project(session, project_id)
    assets = session.scalars(
        select(Asset).where(Asset.project_id == project_id).order_by(Asset.created_at.desc())
    ).all()
    return [serialize(asset) for asset in assets]


@router.get("/assets/{asset_id}/file")
def get_asset_file(
    asset_id: str,
    request: Request,
    session: Session = Depends(get_session, scope="function"),
):
    asset = session.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail={"code": "ASSET_NOT_FOUND"})
    data_dir = session.info["vault_root"].resolve()
    path = (data_dir / asset.relative_path).resolve()
    if not path.is_relative_to(data_dir) or not path.is_file():
        raise HTTPException(status_code=404, detail={"code": "ASSET_FILE_NOT_FOUND"})
    return FileResponse(path, media_type=asset.mime_type, filename=path.name)
