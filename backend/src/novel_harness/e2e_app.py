"""Playwright-only application with a guarded temporary-workspace reset."""

from __future__ import annotations

import os
import secrets
import tempfile
from pathlib import Path, PurePosixPath

from fastapi import Header, HTTPException, Request, Response, status

from novel_harness.config import Settings
from novel_harness.db.vault import ProjectVaultRegistry, _path_is_reparse_point
from novel_harness.main import create_app


def _require_safe_e2e_root(settings: Settings) -> Path:
    token = os.environ.get("NOVEL_E2E_RESET_TOKEN", "")
    if not token:
        raise RuntimeError("E2E_RESET_DISABLED")
    root = settings.data_dir.resolve()
    temporary_root = Path(tempfile.gettempdir()).resolve(strict=True)
    if not root.is_relative_to(temporary_root) or not root.name.startswith(
        "novel-harness-e2e-"
    ):
        raise RuntimeError("UNSAFE_E2E_DATA_DIR")
    root.mkdir(parents=True, exist_ok=True)
    if _path_is_reparse_point(root) or root.resolve(strict=True) != root:
        raise RuntimeError("UNSAFE_E2E_DATA_DIR")
    return root


def _reset_projects(registry: ProjectVaultRegistry, expected_root: Path) -> None:
    if registry.root != expected_root:
        raise RuntimeError("UNSAFE_E2E_DATA_DIR")
    projects_root = registry.root / "projects"
    with registry._lock:  # noqa: SLF001 - this module is an isolated test harness.
        with registry.connect() as database:
            records = [
                dict(row)
                for row in database.execute(
                    "SELECT id,vault_path FROM project_registry ORDER BY id"
                )
            ]
        registry.dispose()
        registry._vaults.clear()  # noqa: SLF001
        registry._maintained.clear()  # noqa: SLF001
        for record in records:
            project_id = str(record["id"])
            registry._validate_id(project_id)  # noqa: SLF001
            if PurePosixPath(str(record["vault_path"])) != PurePosixPath(
                "projects", project_id
            ):
                raise RuntimeError("UNSAFE_E2E_VAULT_PATH")
            vault_root = projects_root / project_id
            if vault_root.exists():
                registry._remove_final_vault(  # noqa: SLF001
                    projects_root, vault_root, project_id
                )
        with registry.connect() as database:
            database.execute("DELETE FROM project_registry")


def create_e2e_app():
    settings = Settings()
    root = _require_safe_e2e_root(settings)
    reset_token = os.environ["NOVEL_E2E_RESET_TOKEN"]
    app = create_app()

    @app.post("/api/v1/_e2e/reset", status_code=status.HTTP_204_NO_CONTENT)
    def reset_e2e_workspace(
        request: Request,
        supplied_token: str | None = Header(default=None, alias="X-Novel-E2E-Reset"),
    ) -> Response:
        if supplied_token is None or not secrets.compare_digest(
            supplied_token, reset_token
        ):
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND"})
        executor = request.app.state.job_executor
        with executor.execution:
            _reset_projects(request.app.state.vault_registry, root)
            executor.initialized.clear()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return app
