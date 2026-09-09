"""Chapter document and version routes."""

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from novel_harness.api.dependencies import get_session
from novel_harness.schemas.writing import (
    ChapterUpdate,
    VersionCreate,
    VersionPage,
    VersionRestore,
)
from novel_harness.services.serialization import serialize
from novel_harness.services.version_pages import read_page
from novel_harness.services.versions import (
    create_version,
    diff_versions,
    get_or_create_document,
    list_versions,
    require_version,
    restore_version,
    serialize_document,
    update_document,
)

router = APIRouter(prefix="/projects/{project_id}/chapters", tags=["chapters"])


@router.get("/{chapter_id}")
def get_chapter(chapter_id: str, session: Session = Depends(get_session, scope="function")):
    return serialize_document(get_or_create_document(session, chapter_id))


@router.put("/{chapter_id}")
def put_chapter(
    chapter_id: str,
    payload: ChapterUpdate,
    session: Session = Depends(get_session, scope="function"),
):
    return serialize_document(
        update_document(session, chapter_id, payload.content, payload.contract, payload.revision)
    )


@router.post("/{chapter_id}/versions", status_code=status.HTTP_201_CREATED)
def post_version(
    chapter_id: str,
    payload: VersionCreate,
    session: Session = Depends(get_session, scope="function"),
):
    return serialize(create_version(session, chapter_id, **payload.model_dump()))


@router.get("/{chapter_id}/versions")
def get_versions(chapter_id: str, session: Session = Depends(get_session, scope="function")):
    return [serialize(version) for version in list_versions(session, chapter_id)]


@router.get("/{chapter_id}/versions/page", response_model=VersionPage)
def get_version_page(
    chapter_id: str,
    limit: int = Query(default=50, ge=1, le=100),
    before: str | None = None,
    session: Session = Depends(get_session, scope="function"),
):
    return read_page(session, chapter_id, limit, before)


@router.get("/{chapter_id}/versions/{version_id}")
def get_version(
    chapter_id: str, version_id: str, session: Session = Depends(get_session, scope="function")
):
    return serialize(require_version(session, chapter_id, version_id))


@router.get("/{chapter_id}/versions/{from_id}/diff/{to_id}")
def get_diff(
    chapter_id: str,
    from_id: str,
    to_id: str,
    session: Session = Depends(get_session, scope="function"),
):
    return {"diff": diff_versions(session, chapter_id, from_id, to_id)}


@router.post("/{chapter_id}/versions/{version_id}/restore", status_code=status.HTTP_201_CREATED)
def post_restore(
    chapter_id: str, version_id: str, payload: VersionRestore,
    session: Session = Depends(get_session, scope="function"),
):
    return serialize(restore_version(session, chapter_id, version_id, payload.expected_revision))
