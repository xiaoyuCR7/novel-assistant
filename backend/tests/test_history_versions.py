import json

from job_helpers import run_job, save_chapter


def test_accepted_history_tracks_current_and_superseded_manuscript(client, project, seeded_chapter):
    base = f"/api/v1/projects/{project['id']}"
    payload = {"project_id": project["id"], "chapter_id": seeded_chapter}
    candidate = run_job(client, base + "/ai/jobs", json={**payload, "task_type": "draft"}).json()
    assert client.post(base + f"/ai/jobs/{candidate['id']}/accept").status_code == 201

    def turns():
        result = run_job(client, base + "/ai/jobs", json={**payload, "task_type": "chat"}).json()
        fragment = next(
            f for f in result["context_snapshot"]["fragments"] if f["source_type"] == "conversation"
        )
        return json.loads(fragment["content"])

    current = next(t for t in turns() if t["status"] != "discussion")
    assert current["status"] == "accepted_current"
    assert candidate["result"]["candidate_text"] not in current["assistant"]
    save_chapter(client, base + f"/chapters/{seeded_chapter}", json={"content": "全新修订"})
    old = next(t for t in turns() if t["status"] != "discussion")
    assert old["status"] == "accepted_superseded"
