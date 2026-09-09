"""Project-local author APIs for temporal story memory."""

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from novel_harness.api.dependencies import get_session
from novel_harness.schemas.imports import (
    MemoryCandidateBulkConfirmRequest,
    MemoryCandidateDecision,
    MemoryCandidatePatch,
)
from novel_harness.schemas.memory import (
    EntityStateCreate,
    EntityStateEdit,
    GeneratedMemoryCandidateDecision,
    GeneratedMemoryCandidateEdit,
)
from novel_harness.services.entity_states import (
    create_entity_state,
    edit_entity_state,
    list_entity_states,
)
from novel_harness.services.memory_candidates import (
    ImportCandidateConflict,
    bulk_confirm_import_candidates,
    candidate_origins,
    confirm_candidate,
    confirm_import_candidate,
    edit_candidate,
    edit_import_candidate,
    list_unified_candidates,
    reject_candidate,
    reject_import_candidate,
    serialize_candidate,
    serialize_import_candidate,
)
from novel_harness.services.serialization import serialize

router = APIRouter(prefix="/projects/{project_id}", tags=["memory"])


@router.get("/memory-candidates")
def get_memory_candidates(
    project_id: str,
    chapter_id: str | None = None,
    status: Literal["pending", "confirmed", "rejected", "conflict"] | None = None,
    origin: Literal["generated", "import"] | None = None,
    limit: int = Query(50, ge=1, le=100),
    cursor: str | None = Query(None),
    session: Session = Depends(get_session, scope="function"),
):
    page = list_unified_candidates(
        session,
        project_id,
        chapter_id=chapter_id,
        status=status,
        origin=origin,
        limit=limit,
        cursor=cursor,
    )
    items = []
    for origin, candidate in page["items"]:
        data = (
            serialize_import_candidate(candidate)
            if origin == "import"
            else {**serialize_candidate(candidate), "origin": "generated"}
        )
        items.append(data)
    return {
        "items": items,
        "next_cursor": page["next_cursor"],
        "total": page["total"],
        "counts": page["counts"],
    }


def _origin(session: Session, project_id: str, candidate_id: str) -> str:
    origins = candidate_origins(session, project_id, candidate_id)
    if not origins:
        raise HTTPException(404, detail={"code": "MEMORY_CANDIDATE_NOT_FOUND"})
    if len(origins) != 1:
        raise HTTPException(409, detail={"code": "MEMORY_CANDIDATE_AMBIGUOUS"})
    return next(iter(origins))


def _checked(model, payload: dict[str, Any]):
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(
            422,
            detail={
                "code": "INVALID_MEMORY_CANDIDATE_REQUEST",
                "errors": exc.errors(include_context=False, include_input=False),
            },
        ) from exc


@router.patch("/memory-candidates/{candidate_id}")
def patch_memory_candidate(
    project_id: str,
    candidate_id: str,
    payload: dict[str, Any],
    session: Session = Depends(get_session, scope="function"),
):
    origin = _origin(session, project_id, candidate_id)
    if origin == "import":
        checked = _checked(MemoryCandidatePatch, payload)
        changes = checked.model_dump(
            mode="json", exclude_unset=True, exclude={"revision"}
        )
        return serialize_import_candidate(
            edit_import_candidate(
                session, project_id, candidate_id, checked.revision, changes
            )
        )
    checked = _checked(GeneratedMemoryCandidateEdit, payload)
    changes = checked.model_dump(mode="json", exclude_unset=True, exclude={"revision"})
    return serialize_candidate(
        edit_candidate(session, project_id, candidate_id, checked.revision, changes)
    )


@router.post("/memory-candidates/{candidate_id}/confirm")
def post_memory_candidate_confirm(
    project_id: str,
    candidate_id: str,
    payload: dict[str, Any],
    session: Session = Depends(get_session, scope="function"),
):
    origin = _origin(session, project_id, candidate_id)
    if origin == "import":
        checked = _checked(MemoryCandidateDecision, payload)
        if checked.decision not in {None, "confirm"}:
            raise HTTPException(422, detail={"code": "INVALID_MEMORY_CANDIDATE_REQUEST"})
        try:
            candidate = confirm_import_candidate(
                session,
                project_id,
                candidate_id,
                checked.revision,
                resolution=checked.resolution,
            )
        except ImportCandidateConflict as exc:
            return JSONResponse(
                status_code=409,
                content={
                    "detail": {
                        "code": "CANDIDATE_RECORD_CONFLICT",
                        "current": serialize_import_candidate(exc.current),
                    }
                },
            )
        return serialize_import_candidate(candidate)
    checked = _checked(GeneratedMemoryCandidateDecision, payload)
    return serialize_candidate(
        confirm_candidate(session, project_id, candidate_id, checked.revision)
    )


@router.post("/memory-candidates/{candidate_id}/reject")
def post_memory_candidate_reject(
    project_id: str,
    candidate_id: str,
    payload: dict[str, Any],
    session: Session = Depends(get_session, scope="function"),
):
    origin = _origin(session, project_id, candidate_id)
    if origin == "import":
        checked = _checked(MemoryCandidateDecision, payload)
        if checked.decision not in {None, "reject"} or checked.resolution is not None:
            raise HTTPException(422, detail={"code": "INVALID_MEMORY_CANDIDATE_REQUEST"})
        return serialize_import_candidate(
            reject_import_candidate(
                session, project_id, candidate_id, checked.revision
            )
        )
    checked = _checked(GeneratedMemoryCandidateDecision, payload)
    return serialize_candidate(
        reject_candidate(session, project_id, candidate_id, checked.revision)
    )


@router.post("/memory-candidates/bulk-confirm")
def post_memory_candidate_bulk_confirm(
    project_id: str,
    payload: MemoryCandidateBulkConfirmRequest,
    session: Session = Depends(get_session, scope="function"),
):
    rows = bulk_confirm_import_candidates(
        session,
        project_id,
        [item.model_dump(mode="json") for item in payload.entries],
    )
    return {"items": [serialize_import_candidate(row) for row in rows]}


@router.get("/entities/{entity_id}/states")
def get_entity_states(
    project_id: str,
    entity_id: str,
    session: Session = Depends(get_session, scope="function"),
):
    return [serialize(state) for state in list_entity_states(session, project_id, entity_id)]


@router.post("/entities/{entity_id}/states", status_code=status.HTTP_201_CREATED)
def post_entity_state(
    project_id: str,
    entity_id: str,
    payload: EntityStateCreate,
    session: Session = Depends(get_session, scope="function"),
):
    return serialize(
        create_entity_state(session, project_id, entity_id, payload.model_dump())
    )


@router.patch("/entity-states/{state_id}")
def patch_entity_state(
    project_id: str,
    state_id: str,
    payload: EntityStateEdit,
    session: Session = Depends(get_session, scope="function"),
):
    changes = payload.model_dump(exclude_unset=True, exclude={"revision"})
    return serialize(
        edit_entity_state(session, project_id, state_id, payload.revision, changes)
    )
