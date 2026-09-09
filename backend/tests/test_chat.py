import sqlite3
from contextlib import closing

from job_helpers import run_job, save_chapter

from novel_harness.ai.base import TextResult
from novel_harness.ai.demo import DemoProvider


def test_project_chat_without_chapter_is_persistent_and_isolated(client, project):
    base = f"/api/v1/projects/{project['id']}/ai/jobs"
    result = run_job(
        client,
        base,
        json={"project_id": project["id"], "task_type": "chat", "instructions": "讨论开篇"},
    )
    assert result.status_code == 200
    assert result.json()["result"]["reply"]
    assert len(client.get(base).json()) == 1
    other = client.post("/api/v1/projects", json={"title": "另一本"}).json()
    assert client.get(f"/api/v1/projects/{other['id']}/ai/jobs").json() == []


def test_next_turn_gets_same_chapter_history(client, project, seeded_chapter):
    captured = []

    class Recorder(DemoProvider):
        def generate_text(self, request):
            captured.append(request)
            return TextResult(text="建议先展示来信", provider="demo", model="test")

    client.app.state.ai_provider = Recorder()
    base = f"/api/v1/projects/{project['id']}/ai/jobs"
    payload = {
        "project_id": project["id"],
        "chapter_id": seeded_chapter,
        "task_type": "chat",
        "instructions": "先别揭示身份",
    }
    assert run_job(client, base, json=payload).status_code == 200
    payload["instructions"] = "按刚才的建议细化"
    assert run_job(client, base, json=payload).status_code == 200
    assert "先别揭示身份" in str(captured[-1].context)
    assert "建议先展示来信" in str(captured[-1].context)
    assert any(
        fragment["source_type"] == "conversation"
        for fragment in captured[-1].context["context_packet"]["fragments"]
    )
    assert client.get(base).json() == []


def test_continuation_preserves_text_and_stale_accept_is_rejected(client, project, seeded_chapter):
    base = f"/api/v1/projects/{project['id']}"
    save_chapter(
        client, base + f"/chapters/{seeded_chapter}", json={"content": "原有开头。", "contract": {}}
    )
    job = run_job(
        client,
        base + "/ai/jobs",
        json={
            "project_id": project["id"],
            "chapter_id": seeded_chapter,
            "task_type": "continue",
            "instructions": "继续",
        },
    ).json()
    assert job["result"]["candidate_text"].startswith("原有开头。")
    save_chapter(
        client,
        base + f"/chapters/{seeded_chapter}",
        json={"content": "作者新修改。", "contract": {}},
    )
    response = client.post(base + f"/ai/jobs/{job['id']}/accept", json={})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "CANDIDATE_SOURCE_CHANGED"


def test_network_generation_does_not_hold_vault_write_lock(
    client, project, seeded_chapter, tmp_path
):
    writable = []

    class Recorder(DemoProvider):
        def generate_text(self, request):
            try:
                with closing(sqlite3.connect(
                    tmp_path / "projects" / project["id"] / "project.db", timeout=0.02
                )) as db, db:
                    db.execute("UPDATE projects SET title=title")
                writable.append(True)
            except sqlite3.OperationalError:
                writable.append(False)
            return super().generate_text(request)

    client.app.state.ai_provider = Recorder()
    response = run_job(
        client,
        f"/api/v1/projects/{project['id']}/ai/jobs",
        json={
            "project_id": project["id"],
            "chapter_id": seeded_chapter,
            "task_type": "chat",
            "instructions": "讨论",
        },
    )
    assert response.status_code == 200
    assert writable == [True]
