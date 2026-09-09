from job_helpers import run_job, save_chapter
from sqlalchemy import select

from novel_harness.db.models import ActivityEvent, Conflict


def test_full_chapter_pipeline_saves_context_and_waits_for_acceptance(
    client, project, seeded_chapter
):
    original = "这是作者手工写下的开场。"
    save_chapter(
        client,
        f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}",
        json={
            "content": original,
            "contract": {
                "purpose": "林渡收到来自未来的遗书",
                "pov_entity_id": None,
                "forbidden_phrases": ["命运的齿轮"],
                "target_words": 2400,
            },
        },
    )

    response = run_job(
        client,
        f"/api/v1/projects/{project['id']}/ai/jobs",
        json={
            "project_id": project["id"],
            "chapter_id": seeded_chapter,
            "task_type": "full_chapter",
            "instructions": "保持克制，让威胁隐藏在日常动作里。",
            "token_budget": 2000,
        },
    )

    assert response.status_code == 200
    job = response.json()
    assert job["status"] == "succeeded"
    assert job["prompt_version"] == "novel-pipeline-v6"
    assert job["result"]["stage_order"] == [
        "plan",
        "draft",
        "continuity_review",
        "style_review",
    ]
    assert job["result"]["candidate_text"]
    assert job["context_snapshot"]["fragments"][0]["source_type"] == "chapter_contract"
    assert job["context_snapshot"]["fragments"][0]["hard"] is True

    before_accept = client.get(f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}").json()
    assert before_accept["content"] == original
    assert before_accept["current_version_id"] is None

    accepted = client.post(f"/api/v1/projects/{project['id']}/ai/jobs/{job['id']}/accept")
    assert accepted.status_code == 201
    version = accepted.json()
    assert version["content"] == job["result"]["candidate_text"]
    assert version["source"] == "ai"
    assert version["generation_job_id"] == job["id"]

    after_accept = client.get(f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}").json()
    assert after_accept["current_version_id"] == version["id"]

    duplicate = client.post(f"/api/v1/projects/{project['id']}/ai/jobs/{job['id']}/accept")
    assert duplicate.status_code == 201
    assert duplicate.json()["id"] == version["id"]


def test_scene_description_is_a_text_candidate_not_an_automatic_edit(
    client, project, seeded_chapter
):
    response = run_job(
        client,
        f"/api/v1/projects/{project['id']}/ai/jobs",
        json={
            "project_id": project["id"],
            "chapter_id": seeded_chapter,
            "task_type": "scene_description",
            "instructions": "描写第七码头在雾潮中短暂显形，强调声音与湿冷触感。",
            "token_budget": 2000,
        },
    )

    assert response.status_code == 200
    job = response.json()
    assert job["result"]["stage_order"] == ["scene_description"]
    assert job["result"]["candidate_text"]
    chapter = client.get(f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}").json()
    assert chapter["content"] == ""


def test_open_task_bound_error_requires_explicit_acceptance_override(
    client, project, seeded_chapter
):
    base = f"/api/v1/projects/{project['id']}"
    job = run_job(
        client,
        base + "/ai/jobs",
        json={
            "project_id": project["id"],
            "chapter_id": seeded_chapter,
            "task_type": "scene_description",
            "instructions": "写一个短场景。",
        },
    ).json()
    revision = job["result"]["source_revision"]
    database = client.app.state.vault_registry.require(project["id"]).database
    with database.job_session_scope() as session:
        conflict = Conflict(
            project_id=project["id"],
            chapter_id=seeded_chapter,
            code="CANON_CONTRADICTION",
            severity="error",
            message="候选与确认事实冲突。",
            evidence=[f"ai_job:{job['id']}", f"document_revision:{revision}"],
        )
        session.add(conflict)
        session.flush()
        conflict_id = conflict.id

    url = base + f"/ai/jobs/{job['id']}/accept"
    blocked = client.post(url)
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == {
        "code": "SEVERE_CONFIRMATION_REQUIRED",
        "message": "候选仍有未处理的严重内容问题，接受前需要作者明确确认。",
        "conflict_ids": [conflict_id],
    }

    assert client.post(url, json={"confirmed": True}).status_code == 201
    with database.job_session_scope() as session:
        event = session.scalar(
            select(ActivityEvent)
            .where(ActivityEvent.kind == "severe_override")
            .order_by(ActivityEvent.created_at.desc())
        )
        assert conflict_id in event.details["conflict_ids"]


def test_pipeline_context_includes_story_bible_plot_and_active_style(
    client, project, seeded_chapter
):
    entity = client.post(
        f"/api/v1/projects/{project['id']}/entities",
        json={
            "kind": "character",
            "name": "林渡",
            "summary": "失忆的邮差",
            "profile": {"voice": "寡言"},
            "state": {"alive": True},
        },
    ).json()
    canon = client.post(
        f"/api/v1/projects/{project['id']}/canon",
        json={
            "subject_entity_id": entity["id"],
            "predicate": "惧怕",
            "value": "钟声",
            "status": "confirmed",
        },
    ).json()
    client.post(
        f"/api/v1/projects/{project['id']}/plots",
        json={"kind": "main", "title": "死者来信", "promise": "追查寄信源头"},
    )
    style = client.post(
        f"/api/v1/projects/{project['id']}/styles",
        json={
            "name": "冷雾短句",
            "is_active": True,
            "config": {"rhythm": "crisp", "forbidden_phrases": ["命运的齿轮"]},
        },
    ).json()
    candidate = client.post(
        f"/api/v1/projects/{project['id']}/feedback",
        json={
            "project_id": project["id"],
            "chapter_id": seeded_chapter,
            "rating": 2,
            "comment": "动作完成后不要再解释动作。",
        },
    ).json()["preference_candidate"]
    confirmed = client.post(
        f"/api/v1/projects/{project['id']}/feedback/preferences/{candidate['id']}/confirm"
    ).json()
    base = f"/api/v1/projects/{project['id']}/library"
    for kind, item_id in (
        ("canon", canon["id"]),
        ("style", style["id"]),
        ("style_rule", confirmed["linked_style_rule_id"]),
    ):
        item = client.get(f"{base}/{kind}/{item_id}").json()
        assert client.patch(
            f"{base}/{kind}/{item_id}",
            json={"revision": item["revision"], "is_pinned": True},
        ).status_code == 200

    job = run_job(
        client,
        f"/api/v1/projects/{project['id']}/ai/jobs",
        json={
            "project_id": project["id"],
            "chapter_id": seeded_chapter,
            "task_type": "draft",
            "instructions": "写开场。",
            "token_budget": 12000,
        },
    ).json()

    source_types = {item["source_type"] for item in job["context_snapshot"]["fragments"]}
    assert {
        "story_entity",
        "canon_fact",
        "plot_thread",
        "style_profile",
        "style_rule",
    } <= source_types


def test_text_generation_persists_rule_conflicts_with_resolution_options(
    client, project, seeded_chapter
):
    save_chapter(
        client,
        f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}",
        json={"content": "", "contract": {"forbidden_phrases": ["夜班铃"]}},
    )
    run_job(
        client,
        f"/api/v1/projects/{project['id']}/ai/jobs",
        json={
            "project_id": project["id"],
            "chapter_id": seeded_chapter,
            "task_type": "draft",
            "instructions": "写邮局开场。",
            "token_budget": 2000,
        },
    )

    conflicts = client.get(f"/api/v1/projects/{project['id']}/conflicts").json()
    assert conflicts[0]["code"] == "FORBIDDEN_PHRASE"
    assert conflicts[0]["status"] == "open"
    assert {item["mode"] for item in conflicts[0]["options"]} == {
        "conservative",
        "balanced",
        "radical",
    }


def test_review_context_includes_current_draft(client, project, seeded_chapter):
    base = f"/api/v1/projects/{project['id']}"
    save_chapter(
        client, base + f"/chapters/{seeded_chapter}", json={"content": "只存在于当前工作副本的线索"}
    )
    job = run_job(
        client,
        base + "/ai/jobs",
        json={"project_id": project["id"], "chapter_id": seeded_chapter, "task_type": "review"},
    ).json()
    assert any(
        "只存在于当前工作副本的线索" in f["content"] for f in job["context_snapshot"]["fragments"]
    )


def test_severe_candidate_requires_explicit_author_confirmation(client, project, seeded_chapter):
    base = f"/api/v1/projects/{project['id']}"
    save_chapter(
        client,
        base + f"/chapters/{seeded_chapter}",
        json={"content": "作者原文", "contract": {"forbidden_revelations": ["雾潮"]}},
    )
    job = run_job(
        client,
        base + "/ai/jobs",
        json={
            "project_id": project["id"],
            "chapter_id": seeded_chapter,
            "task_type": "scene_description",
        },
    ).json()
    url = base + f"/ai/jobs/{job['id']}/accept"
    assert client.post(url).status_code == 409
    assert client.get(base + f"/chapters/{seeded_chapter}").json()["content"] == "作者原文"
    assert client.post(url, json={"confirmed": True}).status_code == 201
