"""Upload, preview, edit, and discard durable import drafts."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from novel_harness.ai.base import ProviderError
from novel_harness.api.dependencies import get_session
from novel_harness.schemas.imports import (
    ImportAnalysisAction,
    ImportAnalysisStatusRead,
    ImportCommitRequest,
    ImportCommitResponse,
    ImportDraftPatch,
    ImportDraftRead,
    ImportLimitsRead,
)
from novel_harness.services.import_analysis import (
    analysis_status,
    continue_analysis,
    latest_analysis_status,
    pause_analysis,
    retry_analysis,
)
from novel_harness.services.import_commit import commit_draft
from novel_harness.services.import_drafts import (
    ImportDraftStore,
    public_import_draft_payload,
)

router = APIRouter(prefix="/imports", tags=["imports"])
analysis_router = APIRouter(prefix="/projects/{project_id}/imports", tags=["imports"])
_UPLOAD_CHUNK_BYTES = 64 * 1024


def _store(request: Request) -> ImportDraftStore:
    return request.app.state.import_drafts


def _upload_error(code: str, message: str, *, status_code: int = 422) -> HTTPException:
    return HTTPException(status_code, detail={"code": code, "message": message})


def _finish_staged_file(handle, *, sync: bool) -> None:
    try:
        if sync:
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        handle.close()


async def _stage_uploads(
    store: ImportDraftStore,
    uploads: list[UploadFile],
    upload_dir: Path,
    source_kind: Literal["folder", "zip"],
) -> list[Path]:
    staged: list[Path] = []
    total = 0
    for index, upload in enumerate(uploads):
        destination = upload_dir / f"{index:08d}-{uuid4().hex}.upload"
        file_size = 0
        single_file_limit = (
            store.limits.max_total_bytes if source_kind == "zip" else store.limits.max_file_bytes
        )
        handle = await run_in_threadpool(destination.open, "xb")
        complete = False
        try:
            while chunk := await upload.read(_UPLOAD_CHUNK_BYTES):
                file_size += len(chunk)
                total += len(chunk)
                if file_size > single_file_limit:
                    raise _upload_error(
                        (
                            "IMPORT_TOTAL_TOO_LARGE"
                            if source_kind == "zip"
                            else "IMPORT_FILE_TOO_LARGE"
                        ),
                        "An uploaded file exceeds the import upload limit.",
                        status_code=413,
                    )
                if total > store.limits.max_total_bytes:
                    raise _upload_error(
                        "IMPORT_TOTAL_TOO_LARGE",
                        "The uploaded files exceed the import total limit.",
                        status_code=413,
                    )
                await run_in_threadpool(handle.write, chunk)
            complete = True
        finally:
            await run_in_threadpool(_finish_staged_file, handle, sync=complete)
        staged.append(destination)
    return staged


@router.post("", response_model=ImportDraftRead, status_code=status.HTTP_201_CREATED)
async def create_import_draft(
    request: Request,
    source_kind: Annotated[Literal["folder", "zip"], Form()],
    display_name: Annotated[str, Form(min_length=1, max_length=240)],
    files: Annotated[list[UploadFile], File(min_length=1)],
    paths: Annotated[list[str] | None, Form()] = None,
) -> ImportDraftRead:
    store = _store(request)
    upload_dir: Path | None = None
    try:
        if len(files) > store.limits.max_files:
            raise _upload_error("IMPORT_FILE_LIMIT", "The import contains too many files.")
        if source_kind == "folder" and len(paths or []) != len(files):
            raise _upload_error(
                "IMPORT_PATH_COUNT_MISMATCH",
                "Folder uploads require exactly one relative path per file.",
            )
        if source_kind == "zip":
            if len(files) != 1:
                raise _upload_error("IMPORT_ZIP_FILE_COUNT", "ZIP imports require one file.")
            if Path(files[0].filename or "").suffix.casefold() != ".zip":
                raise _upload_error("IMPORT_ZIP_REQUIRED", "ZIP imports require a .zip file.")

        upload_dir = await run_in_threadpool(store.create_upload_staging_dir)
        staged = await _stage_uploads(store, files, upload_dir, source_kind)
        if source_kind == "zip":
            record = await run_in_threadpool(store.create_from_zip_path, display_name, staged[0])
            return public_import_draft_payload(record)
        record = await run_in_threadpool(
            store.create_from_staged_files,
            display_name,
            list(zip(paths or [], staged, strict=True)),
        )
        return public_import_draft_payload(record)
    finally:
        if upload_dir is not None:
            await run_in_threadpool(store.cleanup_upload_staging_dir, upload_dir)
        for upload in files:
            await upload.close()


@router.get("/limits", response_model=ImportLimitsRead)
def get_import_limits(request: Request) -> ImportLimitsRead:
    limits = _store(request).limits
    return ImportLimitsRead(
        max_files=limits.max_files,
        max_file_bytes=limits.max_file_bytes,
        max_total_bytes=limits.max_total_bytes,
        max_compression_ratio=limits.max_compression_ratio,
    )


@router.get("/{draft_id}", response_model=ImportDraftRead)
def get_import_draft(draft_id: str, request: Request) -> ImportDraftRead:
    return public_import_draft_payload(_store(request).get(draft_id))


@router.patch("/{draft_id}", response_model=ImportDraftRead)
def patch_import_draft(
    draft_id: str, payload: ImportDraftPatch, request: Request
) -> ImportDraftRead:
    return public_import_draft_payload(_store(request).update(draft_id, payload))


@router.post(
    "/{draft_id}/commit",
    response_model=ImportCommitResponse,
    status_code=status.HTTP_201_CREATED,
)
async def commit_import_draft(
    draft_id: str, payload: ImportCommitRequest, request: Request
) -> ImportCommitResponse:
    return await run_in_threadpool(
        commit_draft,
        request.app.state.vault_registry,
        _store(request),
        draft_id,
        payload.expected_revision,
    )


@router.delete("/{draft_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_import_draft(draft_id: str, request: Request) -> Response:
    _store(request).discard(draft_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _model_identity(request: Request) -> dict:
    try:
        return request.app.state.model_settings.identity()
    except (ValueError, OSError, ProviderError) as exc:
        raise HTTPException(422, detail={"code": "MODEL_CONFIGURATION_UNAVAILABLE"}) from exc


@analysis_router.get("/latest/analysis", response_model=ImportAnalysisStatusRead)
def get_latest_import_analysis(
    project_id: str,
    session: Annotated[Session, Depends(get_session)],
) -> ImportAnalysisStatusRead:
    return latest_analysis_status(session, project_id)


@analysis_router.get("/{batch_id}/analysis", response_model=ImportAnalysisStatusRead)
def get_import_analysis(
    project_id: str,
    batch_id: str,
    session: Annotated[Session, Depends(get_session)],
) -> ImportAnalysisStatusRead:
    return analysis_status(session, project_id, batch_id)


@analysis_router.post("/{batch_id}/analysis/pause", response_model=ImportAnalysisStatusRead)
def pause_import_analysis(
    project_id: str,
    batch_id: str,
    payload: ImportAnalysisAction,
    request: Request,
    session: Annotated[Session, Depends(get_session)],
) -> ImportAnalysisStatusRead:
    pause_analysis(session, project_id, batch_id, payload.revision)
    return analysis_status(session, project_id, batch_id)


@analysis_router.post("/{batch_id}/analysis/continue", response_model=ImportAnalysisStatusRead)
def continue_import_analysis(
    project_id: str,
    batch_id: str,
    payload: ImportAnalysisAction,
    request: Request,
    session: Annotated[Session, Depends(get_session)],
) -> ImportAnalysisStatusRead:
    continue_analysis(
        session,
        project_id,
        batch_id,
        payload.revision,
        _model_identity(request),
        payload.adopt_current_provider,
    )
    request.app.state.job_executor.wake()
    return analysis_status(session, project_id, batch_id)


@analysis_router.post("/{batch_id}/analysis/retry", response_model=ImportAnalysisStatusRead)
def retry_import_analysis(
    project_id: str,
    batch_id: str,
    payload: ImportAnalysisAction,
    request: Request,
    session: Annotated[Session, Depends(get_session)],
) -> ImportAnalysisStatusRead:
    retry_analysis(
        session,
        project_id,
        batch_id,
        payload.revision,
        _model_identity(request),
        payload.adopt_current_provider,
    )
    request.app.state.job_executor.wake()
    return analysis_status(session, project_id, batch_id)
