from fastapi.testclient import TestClient

from novel_harness.main import create_app


def test_submission_returns_committed_receipt_without_model(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVEL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NOVEL_AI_PROVIDER", "demo")
    with TestClient(create_app(start_executor=False)) as client:
        project = client.post("/api/v1/projects", json={"title": "测试"}).json()
        base = f"/api/v1/projects/{project['id']}/ai/jobs"
        command = {"project_id": project["id"], "task_type": "chat", "instructions": "开场"}
        headers = {"Idempotency-Key": "submitted"}
        response = client.post(base, json=command, headers=headers)
        assert response.status_code == 202
        job_id = response.json()["id"]
        assert client.get(base + "/" + job_id).json()["status"] == "queued"
        assert client.post(base, json=command, headers=headers).json()["id"] == job_id
        assert client.app.state.job_executor.run_once()
        assert client.get(base + "/" + job_id).json()["status"] == "succeeded"


def test_query_does_not_maintain_vault(client, project, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Polling must not maintain the Vault")

    monkeypatch.setattr("novel_harness.services.library.collect_expired", forbidden)
    monkeypatch.setattr("novel_harness.services.chapter_summaries.write_ledger", forbidden)
    monkeypatch.setattr("novel_harness.services.search_index.rebuild_index", forbidden)
    response = client.get(f"/api/v1/projects/{project['id']}/ai/jobs")
    assert response.status_code == 200
