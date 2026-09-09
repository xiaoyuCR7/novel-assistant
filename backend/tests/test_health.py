import sqlite3

from fastapi.testclient import TestClient

from novel_harness.main import create_app


def test_health_reports_database_and_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVEL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NOVEL_AI_PROVIDER", "demo")

    with TestClient(create_app()) as client:
        response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "database": "ok",
        "ai_provider": "demo",
    }


def test_health_degrades_when_workspace_registry_is_unavailable(client, monkeypatch):
    def unavailable():
        raise sqlite3.OperationalError("registry unavailable")

    monkeypatch.setattr(client.app.state.vault_registry, "check", unavailable)

    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "degraded",
        "database": "unavailable",
        "ai_provider": "demo",
    }


def test_built_frontend_is_served_without_shadowing_api(tmp_path, monkeypatch):
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<main>小说导演台</main>", encoding="utf-8")
    monkeypatch.setenv("NOVEL_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("NOVEL_STATIC_DIR", str(static_dir))
    monkeypatch.setenv("NOVEL_AI_PROVIDER", "demo")

    with TestClient(create_app()) as client:
        assert client.get("/").text == "<main>小说导演台</main>"
        assert client.get("/api/v1/health").json()["status"] == "ok"
