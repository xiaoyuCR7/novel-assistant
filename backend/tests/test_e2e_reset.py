from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def test_e2e_app_requires_token_and_safe_temporary_data_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novel_harness.e2e_app import create_e2e_app

    monkeypatch.setenv("NOVEL_DATA_DIR", str(tmp_path / "novel-harness-e2e-safe"))
    monkeypatch.delenv("NOVEL_E2E_RESET_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="E2E_RESET_DISABLED"):
        create_e2e_app()

    monkeypatch.setenv("NOVEL_E2E_RESET_TOKEN", "test-token")
    monkeypatch.setenv("NOVEL_DATA_DIR", str(tmp_path / "ordinary-data"))
    with pytest.raises(RuntimeError, match="UNSAFE_E2E_DATA_DIR"):
        create_e2e_app()


def test_e2e_reset_requires_header_and_removes_every_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novel_harness.e2e_app import create_e2e_app

    data_dir = tmp_path / "novel-harness-e2e-reset"
    monkeypatch.setenv("NOVEL_DATA_DIR", str(data_dir))
    monkeypatch.setenv("NOVEL_STATIC_DIR", str(tmp_path / "no-static"))
    monkeypatch.setenv("NOVEL_AI_PROVIDER", "demo")
    monkeypatch.setenv("NOVEL_E2E_RESET_TOKEN", "test-token")

    with TestClient(create_e2e_app()) as client:
        assert client.app.state.job_executor.thread.is_alive()
        first = client.post("/api/v1/projects", json={"title": "一"})
        second = client.post("/api/v1/projects", json={"title": "二"})
        assert first.status_code == 201
        assert second.status_code == 201

        denied = client.post("/api/v1/_e2e/reset")
        assert denied.status_code == 404
        assert len(client.get("/api/v1/projects").json()) == 2

        reset = client.post(
            "/api/v1/_e2e/reset", headers={"X-Novel-E2E-Reset": "test-token"}
        )
        assert reset.status_code == 204
        assert client.get("/api/v1/projects").json() == []
        assert not any((data_dir / "projects").glob("*/project.db"))
        assert client.app.state.job_executor.thread.is_alive()
