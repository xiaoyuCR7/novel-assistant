"""Multimodal asset generation with project-isolated atomic writes."""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException
from sqlalchemy.orm import Session

from novel_harness.ai.base import AIImageRequest, AIProvider, ProviderError
from novel_harness.db.models import Asset, Entity, new_id
from novel_harness.services.projects import require_project


def generate_asset(
    session: Session,
    provider: AIProvider,
    data_dir: Path,
    *,
    project_id: str,
    entity_id: str | None,
    kind: str,
    prompt: str,
    size: str,
) -> Asset:
    require_project(session, project_id)
    if entity_id:
        entity = session.get(Entity, entity_id)
        if entity is None:
            raise HTTPException(status_code=404, detail={"code": "ENTITY_NOT_FOUND"})
        if entity.project_id != project_id:
            raise HTTPException(status_code=409, detail={"code": "CROSS_PROJECT_REFERENCE"})

    asset_id = new_id()
    relative = Path("assets") / f"{asset_id}.png"
    destination = data_dir / relative
    temp = destination.with_name(f".{destination.name}.tmp")
    try:
        result = provider.generate_image(AIImageRequest(task=kind, prompt=prompt, size=size))
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp.write_bytes(result.data)
        temp.replace(destination)
    except ProviderError as exc:
        if temp.exists():
            temp.unlink()
        raise HTTPException(
            status_code=502,
            detail={"code": "IMAGE_PROVIDER_ERROR", "message": str(exc), "retryable": True},
        ) from exc

    asset = Asset(
        id=asset_id,
        project_id=project_id,
        entity_id=entity_id,
        kind=kind,
        prompt=prompt,
        size=size,
        relative_path=relative.as_posix(),
        mime_type=result.mime_type,
        provider=result.provider,
        model=result.model,
        status="ready",
    )
    session.add(asset)
    session.flush()
    return asset
