"""FastAPI application entrypoint."""

from __future__ import annotations

import logging
import sqlite3
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import OperationalError

from novel_harness.ai.demo import DemoProvider
from novel_harness.ai.local_provider import LocalProvider
from novel_harness.api.routes import (
    ai,
    assets,
    chapters,
    feedback,
    imports,
    library,
    memory,
    preparation,
    projects,
    story,
    summaries,
    wiki,
)
from novel_harness.api.routes import settings as model_settings_routes
from novel_harness.config import Settings
from novel_harness.db import models as _models  # noqa: F401
from novel_harness.db.migration import migrate_legacy_database
from novel_harness.db.vault import ProjectVaultRegistry
from novel_harness.services.executor_lock import ExecutorLock
from novel_harness.services.import_drafts import ImportDraftStore, ImportLimits
from novel_harness.services.job_executor import LocalJobExecutor
from novel_harness.services.model_settings import ModelSettings


def create_app(*, start_executor=True) -> FastAPI:
    settings = Settings()
    ai_provider = (
        LocalProvider(settings.local_model_url, settings.local_text_model)
        if settings.ai_provider == "local"
        else DemoProvider()
    )
    embedding_provider = (
        LocalProvider(settings.local_model_url, settings.local_embedding_model)
        if settings.local_embedding_model
        else None
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        with ExecutorLock(settings.data_dir):
            settings.prepare_directories()
            registry = ProjectVaultRegistry(settings.data_dir)
            executor = LocalJobExecutor(app)
            try:
                migrate_legacy_database(registry)
                app.state.settings = settings
                app.state.vault_registry = registry
                app.state.ai_provider = ai_provider
                app.state.embedding_provider = embedding_provider
                app.state.model_settings = ModelSettings(settings)
                app.state.import_drafts = ImportDraftStore(
                    settings.data_dir / "imports", ImportLimits.from_settings(settings)
                )
                app.state.job_executor = executor
                executor.initialize()
                if start_executor:
                    executor.start()
                yield
            finally:
                executor.stop()
                registry.dispose()

    app = FastAPI(title="小说创作 Agent Harness", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def local_request_boundary(request, call_next):
        try:
            model_settings_routes.same_origin(request)
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        try:
            return await call_next(request)
        except Exception as exc:
            original = exc.orig if isinstance(exc, OperationalError) else exc
            code = getattr(original, "sqlite_errorcode", 0) or 0
            if isinstance(original, sqlite3.OperationalError) and (code & 0xFF) in {
                sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED,
            }:
                return JSONResponse(
                    status_code=503,
                    headers={"Retry-After": "1"},
                    content={"detail": {
                        "code": "DATABASE_BUSY",
                        "message": "项目正在执行其他写入，请保留当前草稿，稍后重试。",
                        "retryable": True,
                    }},
                )
            diagnostic_id = uuid4().hex
            # Deliberately omit exception text, request bodies/URLs and traceback locals.
            logging.getLogger("novel_harness.api").error(
                "request_failed diagnostic=%s", diagnostic_id
            )
            return JSONResponse(
                status_code=500,
                content={
                    "detail": {
                        "code": "INTERNAL_ERROR",
                        "message": "操作失败，请保留诊断编号并重试。",
                        "diagnostic_id": diagnostic_id,
                    }
                },
            )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        if request.url.path.startswith("/api/v1/settings/model"):
            return JSONResponse(
                status_code=422,
                content={"detail": {"message": "模型设置格式无效，请检查字段长度及格式。"}},
            )
        return JSONResponse(
            status_code=422,
            content={
                "detail": {
                    "code": "INVALID_REQUEST",
                    "message": "输入字段格式或长度无效，请检查后重试。",
                    "fields": [list(error["loc"]) for error in exc.errors()],
                }
            },
        )

    from novel_harness.api.request_limits import RequestLimitMiddleware

    app.add_middleware(RequestLimitMiddleware)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/v1/health")
    def health() -> dict[str, str]:
        try:
            mode = app.state.model_settings.public()["mode"]
        except (ValueError, OSError):
            mode = "unavailable"
        try:
            database = "ok" if app.state.vault_registry.check() else "unavailable"
        except (OSError, sqlite3.Error):
            database = "unavailable"
        return {
            "status": "ok"
            if mode != "unavailable" and database == "ok"
            else "degraded",
            "database": database,
            "ai_provider": mode,
        }

    app.include_router(projects.router, prefix="/api/v1")
    app.include_router(story.router, prefix="/api/v1")
    app.include_router(chapters.router, prefix="/api/v1")
    app.include_router(ai.router, prefix="/api/v1")
    app.include_router(feedback.router, prefix="/api/v1")
    app.include_router(assets.router, prefix="/api/v1")
    app.include_router(library.router, prefix="/api/v1")
    app.include_router(summaries.router, prefix="/api/v1")
    app.include_router(memory.router, prefix="/api/v1")
    app.include_router(preparation.router, prefix="/api/v1")
    app.include_router(wiki.router, prefix="/api/v1")
    app.include_router(model_settings_routes.router, prefix="/api/v1")
    app.include_router(imports.router, prefix="/api/v1")
    app.include_router(imports.analysis_router, prefix="/api/v1")
    if (settings.static_dir / "index.html").is_file():
        app.mount("/", StaticFiles(directory=settings.static_dir, html=True), name="frontend")

    return app


app = create_app()
