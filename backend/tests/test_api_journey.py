from job_helpers import run_job, save_chapter


def test_author_journey_crosses_all_backend_layers(client):
    project = client.post(
        "/api/v1/projects",
        json={
            "title": "潮汐档案",
            "premise": "档案员发现城市每天被重写一次。",
            "genre": "都市奇幻",
            "target_words": 120000,
            "daily_goal": 1200,
        },
    ).json()
    listed = client.get("/api/v1/projects")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [project["id"]]

    chapter = client.post(
        f"/api/v1/projects/{project['id']}/nodes",
        json={"kind": "chapter", "title": "被删除的星期二", "order_index": 1},
    ).json()
    save_chapter(
        client,
        f"/api/v1/projects/{project['id']}/chapters/{chapter['id']}",
        json={
            "content": "作者保留的工作副本。",
            "contract": {
                "purpose": "证明星期二曾经存在",
                "forbidden_phrases": ["命运的齿轮"],
                "target_words": 3000,
            },
        },
    )
    job = run_job(
        client,
        f"/api/v1/projects/{project['id']}/ai/jobs",
        json={
            "project_id": project["id"],
            "chapter_id": chapter["id"],
            "task_type": "full_chapter",
            "instructions": "用档案记录与现实错位制造悬念。",
            "token_budget": 2000,
        },
    ).json()
    assert job["status"] == "succeeded"
    assert "api_key" not in str(job).lower()

    feedback = client.post(
        f"/api/v1/projects/{project['id']}/feedback",
        json={
            "project_id": project["id"],
            "chapter_id": chapter["id"],
            "job_id": job["id"],
            "rating": 4,
            "tags": ["节奏"],
            "original_text": job["result"]["candidate_text"],
            "corrected_text": job["result"]["candidate_text"] + "\n档案页在此时翻了一面。",
            "comment": "转折前再留一个可见动作。",
        },
    ).json()
    confirmed = client.post(
        f"/api/v1/projects/{project['id']}/feedback/preferences/{feedback['preference_candidate']['id']}/confirm"
    ).json()
    rule_id = confirmed["linked_style_rule_id"]
    rule = client.get(f"/api/v1/projects/{project['id']}/library/style_rule/{rule_id}").json()
    assert client.patch(
        f"/api/v1/projects/{project['id']}/library/style_rule/{rule_id}",
        json={"revision": rule["revision"], "is_pinned": True},
    ).status_code == 200
    # Confirming a new hard style rule invalidates the earlier source manifest.
    assert (
        client.post(f"/api/v1/projects/{project['id']}/ai/jobs/{job['id']}/accept").status_code
        == 409
    )
    job = run_job(
        client,
        f"/api/v1/projects/{project['id']}/ai/jobs",
        json={
            "project_id": project["id"],
            "chapter_id": chapter["id"],
            "task_type": "full_chapter",
            "instructions": "采用已确认的风格规则继续写作。",
            "token_budget": 2000,
        },
    ).json()
    assert job["status"] == "succeeded", (job["error_code"], job["error_message"])
    version = client.post(f"/api/v1/projects/{project['id']}/ai/jobs/{job['id']}/accept").json()
    assert version["generation_job_id"] == job["id"]

    fact = client.post(
        f"/api/v1/projects/{project['id']}/canon",
        json={
            "predicate": "calendar_has_tuesday",
            "value": False,
            "source_version_id": version["id"],
            "status": "confirmed",
        },
    )
    assert fact.status_code == 201

    workspace = client.get(f"/api/v1/projects/{project['id']}/workspace").json()
    assert workspace["canon_facts"][0]["source_version_id"] == version["id"]
    assert client.get(f"/api/v1/projects/{project['id']}/progress").json()["current_words"] > 0
    assert client.get(f"/api/v1/projects/{project['id']}/export").status_code == 200
