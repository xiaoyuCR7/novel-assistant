"""Quality workflow API boundaries, using isolated demo projects."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import func, select

from novel_harness.db.models import (
    ActivityEvent,
    AIJob,
    ChapterDocument,
    ChapterSummary,
    ChapterVersion,
    Entity,
    StoryNode,
)


def force_rewrite(client):
    from novel_harness.ai.base import TextResult
    from novel_harness.ai.demo import DemoProvider

    class RewriteDemo(DemoProvider):
        def generate_text(self, request):
            if request.task == "quality_rewrite":
                return TextResult(
                    text="邮差辨认出信封上的笔迹，抬头看向街尾。",
                    provider=self.name,
                    model=self.model,
                )
            return super().generate_text(request)

        def generate_structured(self, request, schema):
            result = super().generate_structured(request, schema)
            if request.task == "quality_review":
                result.data["scores"] = dict.fromkeys(result.data["scores"], 60)
            return result

    client.app.state.ai_provider = RewriteDemo()


def setup_summary_handoff(client, project):
    """Four prior summaries: accepting chapter four shifts the recent-three window."""
    from novel_harness.services.versions import create_version

    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        ids = []
        summary_ids = []
        for index in range(5):
            chapter = StoryNode(
                project_id=project["id"],
                kind="chapter",
                title=f"第{index + 1}章",
                order_index=index,
            )
            session.add(chapter)
            session.flush()
            ids.append(chapter.id)
            if index < 4:
                version = create_version(
                    session, chapter.id, content=f"第{index + 1}章的旧正文。", source="manual"
                )
                summary = ChapterSummary(
                    project_id=project["id"],
                    chapter_id=chapter.id,
                    version_id=version.id,
                    title=chapter.title,
                    content_hash="a" * 64,
                    recap=f"第{index + 1}章的摘要。",
                    details={},
                )
                session.add(summary)
                session.flush()
                summary_ids.append(summary.id)
    force_rewrite(client)
    job = create_run(client, project, inputs(client, project, ids[3:])).json()
    assert client.app.state.job_executor.run_once()
    url = f"/api/v1/projects/{project['id']}/quality/runs/{job['id']}"
    assert client.get(url).json()["status"] == "succeeded"
    return url, ids, summary_ids


def create_run(client, project, chapters, key="quality-test", mode="collaborate", **extra):
    return client.post(
        f"/api/v1/projects/{project['id']}/quality/runs",
        headers={"Idempotency-Key": key},
        json={"mode": mode, "chapters": chapters, **extra},
    )


def inputs(client, project, ids):
    base = f"/api/v1/projects/{project['id']}"
    return [
        {
            "chapter_id": cid,
            "expected_revision": client.get(base + f"/chapters/{cid}").json()["revision"],
        }
        for cid in ids
    ]


def test_quality_requires_saved_text_and_matching_revision(client, project, seeded_chapter):
    chapters = inputs(client, project, [seeded_chapter])
    assert create_run(client, project, chapters, mode="polish").status_code == 422
    chapters[0]["expected_revision"] += 1
    assert create_run(client, project, chapters).status_code == 409


def test_quality_is_project_isolated_and_ordered(client, project, seeded_chapter):
    base = f"/api/v1/projects/{project['id']}"
    later = client.post(
        base + "/nodes", json={"kind": "chapter", "title": "后章", "order_index": 10}
    ).json()["id"]
    chapters = inputs(client, project, [seeded_chapter, later])
    assert create_run(client, project, chapters[::-1]).status_code == 422
    assert create_run(client, project, [chapters[0], chapters[0]]).status_code == 422
    other = client.post("/api/v1/projects", json={"title": "其他项目"}).json()
    assert create_run(client, other, chapters).status_code == 404


def test_quality_alternates_and_preserves_formal_manuscripts(client, project, seeded_chapter):
    base = f"/api/v1/projects/{project['id']}"
    later = client.post(
        base + "/nodes", json={"kind": "chapter", "title": "后章", "order_index": 10}
    ).json()["id"]
    chapters = inputs(client, project, [seeded_chapter, later])
    response = create_run(client, project, chapters)
    assert response.status_code == 202, response.text
    job = response.json()
    assert create_run(client, project, chapters).json()["id"] == job["id"]
    assert create_run(client, project, chapters, key="duplicate").status_code == 409
    assert client.app.state.job_executor.run_once()
    url = base + "/quality/runs/" + job["id"]
    finished = client.get(url).json()
    assert finished["status"] == "succeeded", finished
    assert finished["result"]["completed_chapters"] == 2
    assert [m["role"] for m in finished["result"]["messages"]] == [
        "writer",
        "optimizer",
        "writer",
        "optimizer",
    ]
    assert all(
        client.get(base + f"/chapters/{cid}").json()["content"] == ""
        for cid in [seeded_chapter, later]
    )
    assert client.get(base + "/ai/jobs", params={"chapter_id": seeded_chapter}).json() == []
    assert (
        client.get(base + "/ai/jobs/page", params={"chapter_id": seeded_chapter}).json()["items"]
        == []
    )
    assert client.get(base + "/quality/runs").json()["items"][0]["id"] == job["id"]
    assert client.post(url + "/accept", json={"chapter_id": later}).status_code == 409
    first = client.post(url + "/accept", json={"chapter_id": seeded_chapter})
    assert first.status_code == 200, first.text
    assert (
        client.post(url + "/accept", json={"chapter_id": seeded_chapter}).json()["id"]
        == first.json()["id"]
    )
    assert client.post(url + "/accept", json={"chapter_id": later}).status_code == 200


def test_quality_stale_candidate_never_overwrites_author_edit(client, project, seeded_chapter):
    base = f"/api/v1/projects/{project['id']}"
    job = create_run(client, project, inputs(client, project, [seeded_chapter])).json()
    assert client.app.state.job_executor.run_once()
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        doc = session.get(ChapterDocument, seeded_chapter)
        doc.content = "作者的新稿"
        doc.revision += 1
    response = client.post(
        base + f"/quality/runs/{job['id']}/accept",
        json={"chapter_id": seeded_chapter, "confirmed": True},
    )
    assert response.status_code == 409
    assert client.get(base + f"/chapters/{seeded_chapter}").json()["content"] == "作者的新稿"


def test_quality_stale_sources_pause_before_model_call(
    client, project, seeded_chapter, monkeypatch
):
    job = create_run(client, project, inputs(client, project, [seeded_chapter])).json()
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        session.get(StoryNode, seeded_chapter).title = "新标题"

    def forbidden(*args, **kwargs):
        raise AssertionError("Called a model with stale sources")

    monkeypatch.setattr(client.app.state.ai_provider, "generate_text", forbidden)
    assert client.app.state.job_executor.run_once()
    result = client.get(f"/api/v1/projects/{project['id']}/quality/runs/{job['id']}").json()
    assert result["status"] == "recovery_required"
    assert result["error_code"] == "QUALITY_SOURCE_CHANGED"


def test_accepting_prior_chapter_does_not_reject_shifted_summary_window(client, project):
    url, ids, summaries = setup_summary_handoff(client, project)
    assert client.post(url + "/accept", json={"chapter_id": ids[3]}).status_code == 200
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        assert session.get(ChapterSummary, summaries[3]).status == "stale"
    response = client.post(url + "/accept", json={"chapter_id": ids[4]})
    assert response.status_code == 200, response.text


def test_changed_frozen_summary_still_blocks_later_acceptance(client, project):
    url, ids, summaries = setup_summary_handoff(client, project)
    assert client.post(url + "/accept", json={"chapter_id": ids[3]}).status_code == 200
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        summary = session.get(ChapterSummary, summaries[2])
        summary.recap = "作者更正的关键线索。"
        summary.revision += 1
    response = client.post(url + "/accept", json={"chapter_id": ids[4], "confirmed": True})
    assert response.status_code == 409


def test_accepted_prior_contract_change_blocks_next_candidate(client, project, seeded_chapter):
    base = f"/api/v1/projects/{project['id']}"
    later = client.post(
        base + "/nodes", json={"kind": "chapter", "title": "后章", "order_index": 10}
    ).json()["id"]
    job = create_run(client, project, inputs(client, project, [seeded_chapter, later])).json()
    assert client.app.state.job_executor.run_once()
    url = base + f"/quality/runs/{job['id']}"
    assert client.post(url + "/accept", json={"chapter_id": seeded_chapter}).status_code == 200
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        document = session.get(ChapterDocument, seeded_chapter)
        document.contract = {"purpose": "作者更改前章目标"}
        document.revision += 1
    response = client.post(url + "/accept", json={"chapter_id": later})
    assert response.status_code == 409, response.text


def writing_job(client, project, chapter_id, key):
    base = f"/api/v1/projects/{project['id']}"
    revision = client.get(base + f"/chapters/{chapter_id}").json()["revision"]
    return client.post(
        base + "/ai/jobs",
        headers={"Idempotency-Key": key},
        json={
            "project_id": project["id"],
            "chapter_id": chapter_id,
            "task_type": "draft",
            "expected_revision": revision,
        },
    )


def fail_queued_job(client, project, job_id):
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        session.get(AIJob, job_id).status = "failed"


@pytest.mark.parametrize("first_kind", ["quality", "writing"])
def test_quality_and_writing_cannot_overlap_even_on_resume(
    client, project, seeded_chapter, first_kind
):
    base = f"/api/v1/projects/{project['id']}"
    chapters = inputs(client, project, [seeded_chapter])
    first = (
        create_run(client, project, chapters)
        if first_kind == "quality"
        else writing_job(client, project, seeded_chapter, "writing-first")
    )
    assert first.status_code == 202, first.text
    blocked = (
        writing_job(client, project, seeded_chapter, "writing-blocked")
        if first_kind == "quality"
        else create_run(client, project, chapters)
    )
    assert blocked.status_code == 409
    job = first.json()
    fail_queued_job(client, project, job["id"])
    second = (
        writing_job(client, project, seeded_chapter, "writing-second")
        if first_kind == "quality"
        else create_run(client, project, chapters)
    )
    assert second.status_code == 202, second.text
    resumed = client.post(
        base + f"/ai/jobs/{job['id']}/resume",
        headers={"Idempotency-Key": "first-resume"},
        json={"expected_control_revision": job["control_revision"]},
    )
    assert resumed.status_code == 409, resumed.text


def test_concurrent_quality_acceptance_creates_one_version(client, project, seeded_chapter):
    job = create_run(client, project, inputs(client, project, [seeded_chapter])).json()
    assert client.app.state.job_executor.run_once()
    url = f"/api/v1/projects/{project['id']}/quality/runs/{job['id']}/accept"
    barrier = Barrier(2)

    def accept(_):
        barrier.wait(timeout=5)
        return client.post(url, json={"chapter_id": seeded_chapter})

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(accept, range(2)))
    assert [response.status_code for response in responses] == [200, 200]
    assert responses[0].json()["id"] == responses[1].json()["id"]
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(ChapterVersion)
                .where(ChapterVersion.generation_job_id == job["id"])
            )
            == 1
        )


def test_concurrent_approvals_require_latest_control_revision(client, project, seeded_chapter):
    job = create_run(
        client, project, inputs(client, project, [seeded_chapter]), quality_target=95
    ).json()
    assert client.app.state.job_executor.run_once()
    url = f"/api/v1/projects/{project['id']}/quality/runs/{job['id']}"
    paused = client.get(url).json()
    assert paused["status"] == "recovery_required"
    assert paused["error_code"] == "QUALITY_REVIEW_REQUIRED"
    assert "replace" not in paused["allowed_actions"]
    page = client.get(f"/api/v1/projects/{project['id']}/quality/runs").json()
    assert "replace" not in page["items"][0]["allowed_actions"]
    barrier = Barrier(2)

    def approve(_):
        barrier.wait(timeout=5)
        return client.post(
            url + "/approve",
            json={
                "chapter_id": seeded_chapter,
                "confirmed": True,
                "expected_control_revision": paused["control_revision"],
            },
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(approve, range(2)))
    assert sorted(response.status_code for response in responses) == [200, 409]
    current = client.get(url).json()
    assert current["status"] == "recovery_required"
    assert current["control_revision"] == paused["control_revision"] + 1
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(ActivityEvent)
                .where(
                    ActivityEvent.entity_id == job["id"], ActivityEvent.kind == "quality_override"
                )
            )
            == 1
        )


@pytest.mark.parametrize("location", ["instructions", "manuscript"])
def test_explicit_entity_profile_is_complete_quality_context(
    client, project, seeded_chapter, location
):
    from novel_harness.db.job_models import AIJobControl

    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        entity = Entity(
            project_id=project["id"],
            name="钟楼看守",
            kind="character",
            summary="一名看守",
            profile={"speaking_style": "从不用问句，句尾保留停顿"},
        )
        session.add(entity)
        session.flush()
        entity_id = entity.id
        if location == "manuscript":
            from novel_harness.services.versions import get_or_create_document

            document = get_or_create_document(session, seeded_chapter)
            document.content = f"看守站在门旁。[[ref:entity:{entity_id}]]"
    instructions = (
        f"请遵守这份人物档案 [[ref:entity:{entity_id}]]" if location == "instructions" else ""
    )
    response = create_run(
        client, project, inputs(client, project, [seeded_chapter]), instructions=instructions
    )
    assert response.status_code == 202, response.text
    with vault.database.job_session_scope() as session:
        control = session.get(AIJobControl, response.json()["id"])
        context = control.source_snapshot["chapters"][0]["context"]
        explicit = context["explicit_sources"]
        assert len(explicit) == 1
        assert explicit[0]["id"] == f"entity:{entity_id}"
        assert "从不用问句，句尾保留停顿" in explicit[0]["content"]
        assert f"entity:{entity_id}" in context["allowed_reference_ids"]
        assert control.source_snapshot["chapters"][0]["source"]["manifest"][0]["id"] == entity_id


def test_explicit_reference_revision_change_stops_quality_send(
    client, project, seeded_chapter, monkeypatch
):
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        entity = Entity(
            project_id=project["id"], name="未钉选人物", kind="character", profile={"voice": "直白"}
        )
        session.add(entity)
        session.flush()
        entity_id = entity.id
    job = create_run(
        client,
        project,
        inputs(client, project, [seeded_chapter]),
        instructions=f"保留 [[ref:entity:{entity_id}]] 的人物特点。 ",
    ).json()
    with vault.database.session_scope() as session:
        entity = session.get(Entity, entity_id)
        entity.profile = {"voice": "迂回"}
        entity.revision += 1

    def forbidden(*args, **kwargs):
        raise AssertionError("Model called after a referenced profile changed")

    monkeypatch.setattr(client.app.state.ai_provider, "generate_text", forbidden)
    assert client.app.state.job_executor.run_once()
    result = client.get(f"/api/v1/projects/{project['id']}/quality/runs/{job['id']}").json()
    assert result["status"] == "recovery_required"
    assert result["error_code"] == "QUALITY_SOURCE_CHANGED"


def test_quality_rejects_foreign_explicit_reference(client, project, seeded_chapter):
    other = client.post("/api/v1/projects", json={"title": "另一部作品"}).json()
    vault = client.app.state.vault_registry.require(other["id"])
    with vault.database.session_scope() as session:
        entity = Entity(project_id=other["id"], name="另一部作品的人物", kind="character")
        session.add(entity)
        session.flush()
        entity_id = entity.id
    response = create_run(
        client,
        project,
        inputs(client, project, [seeded_chapter]),
        instructions=f"参考 [[ref:entity:{entity_id}]]",
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "REFERENCE_UNAVAILABLE"


@pytest.mark.parametrize("change_reference", [False, True])
def test_prior_explicit_source_stays_guarded_after_rewrite_removes_marker(
    client, project, seeded_chapter, change_reference
):
    from novel_harness.services.versions import get_or_create_document

    base = f"/api/v1/projects/{project['id']}"
    later = client.post(
        base + "/nodes", json={"kind": "chapter", "title": "后章", "order_index": 10}
    ).json()["id"]
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        entity = Entity(
            project_id=project["id"],
            name="口音独特的邮差",
            kind="character",
            profile={"voice": "直白"},
        )
        session.add(entity)
        session.flush()
        entity_id = entity.id
        document = get_or_create_document(session, seeded_chapter)
        document.content = f"邮差看向来信。[[ref:entity:{entity_id}]]"
    force_rewrite(client)
    job = create_run(client, project, inputs(client, project, [seeded_chapter, later])).json()
    assert client.app.state.job_executor.run_once()
    url = base + f"/quality/runs/{job['id']}"
    assert client.get(url).json()["status"] == "succeeded"
    accepted = client.post(url + "/accept", json={"chapter_id": seeded_chapter})
    assert accepted.status_code == 200, accepted.text
    assert "[[ref:" not in accepted.json()["content"]
    if change_reference:
        with vault.database.session_scope() as session:
            entity = session.get(Entity, entity_id)
            entity.profile = {"voice": "委婉"}
            entity.revision += 1
    response = client.post(url + "/accept", json={"chapter_id": later})
    assert response.status_code == (409 if change_reference else 200), response.text


@pytest.mark.parametrize("new_kind", ["quality", "writing"])
def test_old_partial_candidate_cannot_interrupt_new_active_work(
    client, project, seeded_chapter, monkeypatch, new_kind
):
    from novel_harness.ai.base import ProviderExecutionError

    base = f"/api/v1/projects/{project['id']}"
    later = client.post(
        base + "/nodes", json={"kind": "chapter", "title": "后章", "order_index": 10}
    ).json()["id"]
    provider = client.app.state.ai_provider
    original_generate = provider.generate_text

    def fail_second(request):
        if request.context.get("chapter_id") == later:
            raise ProviderExecutionError("Second chapter failed", outcome="known")
        return original_generate(request)

    monkeypatch.setattr(provider, "generate_text", fail_second)
    old = create_run(client, project, inputs(client, project, [seeded_chapter, later])).json()
    assert client.app.state.job_executor.run_once()
    url = base + f"/quality/runs/{old['id']}"
    partial = client.get(url).json()
    assert partial["status"] == "failed"
    assert partial["result"]["chapters"][0]["status"] == "ready"
    monkeypatch.setattr(provider, "generate_text", original_generate)
    new = (
        create_run(client, project, inputs(client, project, [seeded_chapter]), key="new-quality")
        if new_kind == "quality"
        else writing_job(client, project, seeded_chapter, "new-writing")
    )
    assert new.status_code == 202, new.text
    response = client.post(url + "/accept", json={"chapter_id": seeded_chapter})
    assert response.status_code == 409, response.text
    assert client.get(base + f"/chapters/{seeded_chapter}").json()["content"] == ""
    assert client.get(base + f"/ai/jobs/{new.json()['id']}").json()["status"] == "queued"


def test_later_acceptance_guards_summary_used_by_accepted_prior_candidate(client, project):
    url, ids, summaries = setup_summary_handoff(client, project)
    assert client.post(url + "/accept", json={"chapter_id": ids[3]}).status_code == 200
    # Chapter four used summaries 1/2/3. Chapter five used 2/3/4 plus chapter
    # four's optimized candidate, so summary one's semantics remain a dependency.
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        summary = session.get(ChapterSummary, summaries[0])
        summary.recap = "作者更正：来信实际上来自另一人。"
        summary.revision += 1
    response = client.post(url + "/accept", json={"chapter_id": ids[4], "confirmed": True})
    assert response.status_code == 409, response.text


def test_partially_accepted_quality_run_cannot_offer_or_execute_resume(
    client, project, seeded_chapter, monkeypatch
):
    from novel_harness.ai.base import ProviderExecutionError

    base = f"/api/v1/projects/{project['id']}"
    later = client.post(
        base + "/nodes", json={"kind": "chapter", "title": "后章", "order_index": 10}
    ).json()["id"]
    provider = client.app.state.ai_provider
    original_generate = provider.generate_text

    def fail_second(request):
        if request.context.get("chapter_id") == later:
            raise ProviderExecutionError("Second chapter failed", outcome="known")
        return original_generate(request)

    monkeypatch.setattr(provider, "generate_text", fail_second)
    job = create_run(client, project, inputs(client, project, [seeded_chapter, later])).json()
    assert client.app.state.job_executor.run_once()
    url = base + f"/quality/runs/{job['id']}"
    assert client.get(url).json()["status"] == "failed"
    assert client.post(url + "/accept", json={"chapter_id": seeded_chapter}).status_code == 200
    detail = client.get(url).json()
    page = client.get(base + "/quality/runs").json()
    response = client.post(
        base + f"/ai/jobs/{job['id']}/resume",
        headers={"Idempotency-Key": "resume-after-accept"},
        json={"expected_control_revision": detail["control_revision"]},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "QUALITY_ALREADY_ACCEPTED"
    assert "resume" not in detail["allowed_actions"]
    assert "resume" not in page["items"][0]["allowed_actions"]
