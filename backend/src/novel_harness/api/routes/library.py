from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from novel_harness.ai.base import ProviderError
from novel_harness.api.dependencies import get_session
from novel_harness.schemas.library import LibraryPage, MaterialCreate, MaterialPatch
from novel_harness.services import library, library_pages, retrieval
from novel_harness.services.search_index import SEARCH_INDEX_VERSION, rebuild_index

router = APIRouter(prefix="/projects/{project_id}", tags=["library"])


class RagQuery(BaseModel):
    query: str = Field(default="", max_length=2000)
    chapter_id: str | None = None
    limit: int = Field(default=30, ge=1, le=100)
    include_manuscripts: bool = False


@router.post("/rag/search")
def search_rag(payload: RagQuery, session: Session = Depends(get_session, scope="function")):
    return retrieval.search(
        session,
        payload.query,
        payload.chapter_id,
        payload.limit,
        include_manuscripts=payload.include_manuscripts,
    )


@router.get("/library")
def list_library(session: Session = Depends(get_session, scope="function")):
    return library.list_items(session)


@router.get("/library/search")
def search_library(
    q: str = Query(default="", max_length=2000),
    chapter_id: str | None = None,
    include_manuscripts: bool = False,
    lightweight: bool = False,
    category: str = "all",
    session: Session = Depends(get_session, scope="function"),
):
    return retrieval.search(
        session,
        q,
        chapter_id,
        include_manuscripts=include_manuscripts,
        lightweight=lightweight,
        category=category,
    )


@router.get("/library/page", response_model=LibraryPage)
def library_page(
    project_id: str,
    limit: int = Query(50, ge=1),
    category: str = "all",
    cursor: str | None = Query(None, max_length=4096),
    pinned: bool = False,
    session: Session = Depends(get_session, scope="function"),
):
    return library_pages.page(
        session, project_id, limit=limit, category=category, cursor=cursor, pinned=pinned
    )


@router.get("/trash/page", response_model=LibraryPage)
def trash_page(
    project_id: str,
    limit: int = Query(50, ge=1),
    category: str = "all",
    cursor: str | None = Query(None, max_length=4096),
    session: Session = Depends(get_session, scope="function"),
):
    return library_pages.page(
        session, project_id, limit=limit, category=category, cursor=cursor, deleted=True
    )


@router.get("/library/quick-access")
def quick_access(session: Session = Depends(get_session, scope="function")):
    return [item for item in library.list_items(session) if item["is_pinned"]]


@router.post("/library/{kind}", status_code=201)
def create_material(
    project_id: str,
    kind: str,
    payload: MaterialCreate,
    session: Session = Depends(get_session, scope="function"),
):
    return library.create_item(session, project_id, kind, payload)


@router.get("/library/{kind}/{item_id}")
def material_detail(
    project_id: str,
    kind: str,
    item_id: str,
    session: Session = Depends(get_session, scope="function"),
):
    return library.detail(session, project_id, kind, item_id)


@router.patch("/library/{kind}/{item_id}")
def edit_material(
    kind: str,
    item_id: str,
    payload: MaterialPatch,
    session: Session = Depends(get_session, scope="function"),
):
    return library.edit_item(session, kind, item_id, payload)


@router.delete("/library/{kind}/{item_id}")
def delete_material(
    kind: str, item_id: str, session: Session = Depends(get_session, scope="function")
):
    return library.trash_item(session, kind, item_id)


@router.get("/trash")
def list_trash(session: Session = Depends(get_session, scope="function")):
    return library.list_items(session, deleted=True)


@router.post("/trash/{kind}/{item_id}/restore")
def restore_material(
    kind: str, item_id: str, session: Session = Depends(get_session, scope="function")
):
    return library.restore_item(session, kind, item_id)


@router.delete("/trash/{kind}/{item_id}/purge")
def purge_material(
    kind: str, item_id: str, session: Session = Depends(get_session, scope="function")
):
    return library.purge_item(session, kind, item_id)


@router.get("/rag/health")
def health(session: Session = Depends(get_session, scope="function")):
    return {
        "fts": "ready"
        if session.scalar(text("SELECT version FROM search_index_state WHERE id=1"))
        == SEARCH_INDEX_VERSION
        else "needs_rebuild",
        "vectors": retrieval.vector_status(session.info.get("vectors")),
        "mode": "hybrid",
        "documents": session.scalar(text("SELECT count(*) FROM search_documents")),
        "ledger_pending": bool(
            session.scalar(text("SELECT kind FROM pending_projections WHERE kind='ledger'"))
        ),
    }


@router.post("/rag/rebuild")
def rebuild(session: Session = Depends(get_session, scope="function")):
    rebuild_index(session)
    session.commit()
    if session.info.get("vectors"):
        try:
            session.info["vectors"].rebuild(session)
        except ProviderError as exc:
            raise HTTPException(
                503,
                detail={
                    "code": "VECTOR_REBUILD_FAILED",
                    "message": str(exc) + "关键词索引已更新。",
                },
            ) from exc
    return health(session)


@router.post("/rag/repair-ledger")
def repair_projection(session: Session = Depends(get_session, scope="function")):
    from novel_harness.services.chapter_summaries import write_ledger

    write_ledger(session)
    return {"scheduled": True}
