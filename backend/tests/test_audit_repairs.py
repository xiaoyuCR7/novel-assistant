import pytest
from fastapi import HTTPException
from job_helpers import create_chapter_version, run_job, save_chapter

from novel_harness.ai.demo import DemoProvider
from novel_harness.db.models import AIJob, ChapterDocument, StoryNode
from novel_harness.services.pipeline import CreationPipeline


@pytest.mark.parametrize(
    "contract",
    [
        {"forbidden_phrases": [123]},
        {"forbidden_revelations": "not a list"},
        {"required_entity_ids": "not a list"},
        {"target_words": -1},
    ],
)
def test_invalid_contract_is_rejected_without_changing_draft(
    client, project, seeded_chapter, contract
):
    url = f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}"
    save_chapter(client, url, json={"content": "safe"})
    assert (
        save_chapter(client, url, json={"content": "bad", "contract": contract}).status_code == 422
    )
    assert client.get(url).json()["content"] == "safe"


def test_interleaved_acceptance_rechecks_committed_revision(client, project, seeded_chapter):
    base = f"/api/v1/projects/{project['id']}"
    payload = {"project_id": project["id"], "chapter_id": seeded_chapter, "task_type": "draft"}
    jobs = [run_job(client, base + "/ai/jobs", json=payload).json() for _ in range(2)]
    vault = client.app.state.vault_registry.require(project["id"])
    sessions = [vault.database._sessions(), vault.database._sessions()]
    cached = []
    try:
        for session, job in zip(sessions, jobs, strict=True):
            session.info.update(vault_root=vault.root, index_ready=True)
            cached.append(
                (
                    session.get(AIJob, job["id"]),
                    session.get(ChapterDocument, seeded_chapter),
                    session.get(StoryNode, seeded_chapter),
                )
            )
        CreationPipeline(DemoProvider()).accept(sessions[0], jobs[0]["id"])
        sessions[0].commit()
        with pytest.raises(HTTPException) as error:
            CreationPipeline(DemoProvider()).accept(sessions[1], jobs[1]["id"])
        assert error.value.status_code == 409
        assert error.value.detail["code"] == "CANDIDATE_SOURCE_CHANGED"
    finally:
        for session in sessions:
            session.rollback()
            session.close()
    assert len(client.get(base + f"/chapters/{seeded_chapter}/versions").json()) == 1


def test_version_creation_rejects_unavailable_stable_reference(client, project, seeded_chapter):
    response = create_chapter_version(
        client,
        f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}/versions",
        json={"content": "[[ref:entity:missing]]"},
    )
    assert response.status_code == 422


def test_feedback_rejects_unavailable_job_before_writing(client, project):
    base = f"/api/v1/projects/{project['id']}"
    response = client.post(
        base + "/feedback",
        json={"project_id": project["id"], "job_id": "not-in-this-vault", "rating": 3},
    )
    assert response.status_code == 422
    assert client.get(base + "/preferences").json() == []


def test_completion_refreshes_source_before_creating_its_version(client, project, seeded_chapter):
    from novel_harness.db.models import ChapterDocument
    from novel_harness.services.chapter_summaries import prepare_completion

    base = f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}"
    save_chapter(client, base, json={"content": "old content"})
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as stale_session:
        stale = stale_session.get(ChapterDocument, seeded_chapter)
        updated = save_chapter(client, base, json={"content": "new author content"}).json()
        assert stale.content == "old content"
        result = prepare_completion(stale_session, seeded_chapter, updated["revision"])
        assert result["content"] == "new author content"
    assert client.get(base).json()["content"] == "new author content"
