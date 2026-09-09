import sqlite3
from contextlib import closing

from fastapi.testclient import TestClient

from novel_harness.main import create_app


def test_success_response_is_sent_only_after_database_commit(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVEL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NOVEL_AI_PROVIDER", "demo")
    app = create_app()
    observed = []
    project_id = None

    async def observe(scope, receive, send):
        async def checked_send(message):
            if (
                scope.get("path", "").endswith("/assets/generate")
                and message["type"] == "http.response.start"
            ):
                with closing(
                    sqlite3.connect(tmp_path / "projects" / project_id / "project.db")
                ) as db:
                    observed.append(db.execute("SELECT count(*) FROM assets").fetchone()[0])
            await send(message)

        await app(scope, receive, checked_send)

    with TestClient(observe) as client:
        project_id = client.post("/api/v1/projects", json={"title": "提交顺序"}).json()["id"]
        result = client.post(
            f"/api/v1/projects/{project_id}/assets/generate",
            json={
                "project_id": project_id,
                "kind": "scene",
                "prompt": "雾港",
                "size": "1024x1024",
            },
        )
        assert result.status_code == 201
    assert observed == [1]
