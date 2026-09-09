"""FastAPI dependencies."""

from collections.abc import Iterator

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session


def get_session(project_id: str, request: Request) -> Iterator[Session]:
    # Routes use scope="function": commit must precede the HTTP success response.
    vault = request.app.state.vault_registry.require(project_id)
    if not request.url.path.endswith("/purge"):
        from novel_harness.services.library import collect_expired

        with vault.database.session_scope() as maintenance:
            collect_expired(maintenance)
    with vault.database.session_scope() as session:
        session.info["project_id"] = project_id
        if request.app.state.embedding_provider:
            from novel_harness.services.local_vectors import LocalVectorIndex

            session.info["vectors"] = LocalVectorIndex(
                vault.root, request.app.state.embedding_provider
            )
        yield session


def verify_project_payload(project_id: str, payload_project_id: str) -> None:
    if project_id != payload_project_id:
        raise HTTPException(409, detail={"code": "CROSS_PROJECT_REFERENCE"})


def get_job_store(project_id: str, request: Request):
    from novel_harness.services.job_store import JobStore

    vault = request.app.state.vault_registry.require(project_id, job_only=True)
    return JobStore(vault.database, project_id)


def get_job_session(project_id: str, request: Request):
    store = get_job_store(project_id, request)
    with store.database.job_session_scope() as session:
        yield session
