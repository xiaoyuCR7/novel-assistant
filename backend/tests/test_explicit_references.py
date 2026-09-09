from job_helpers import run_job, save_chapter


def test_explicit_reference_resolves_current_revision_and_rejects_other_novel(
    client, project, seeded_chapter
):
    base = f"/api/v1/projects/{project['id']}"
    other = client.post("/api/v1/projects", json={"title": "隔离项目"}).json()
    foreign = client.post(
        f"/api/v1/projects/{other['id']}/library/entity", json={"title": "异项目人物"}
    ).json()
    material = client.post(
        base + "/library/entity", json={"title": "本地人物", "content": "原设定"}
    ).json()
    chapter_url = base + f"/chapters/{seeded_chapter}"
    token = f"[[ref:entity:{material['id']}]]"
    assert save_chapter(client, chapter_url, json={"content": token}).status_code == 200
    client.patch(
        base + f"/library/entity/{material['id']}", json={"revision": 1, "content": "最新人物设定"}
    )
    job = run_job(
        client,
        base + "/ai/jobs",
        json={"project_id": project["id"], "chapter_id": seeded_chapter, "task_type": "draft"},
    ).json()
    assert any(
        f["reason"] == "作者显式引用" and "最新人物设定" in f["content"]
        for f in job["context_snapshot"]["fragments"]
    )
    rejected = save_chapter(
        client, chapter_url, json={"content": f"[[ref:entity:{foreign['id']}]]"}
    )
    assert rejected.status_code == 422
    assert client.get(chapter_url).json()["content"] == token


def test_contract_cannot_reference_foreign_pov(client, project, seeded_chapter):
    other = client.post("/api/v1/projects", json={"title": "外部小说"}).json()
    person = client.post(
        f"/api/v1/projects/{other['id']}/library/entity", json={"title": "外部视角"}
    ).json()
    response = save_chapter(
        client,
        f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}",
        json={"content": "正文", "contract": {"pov_entity_id": person["id"]}},
    )
    assert response.status_code == 422
