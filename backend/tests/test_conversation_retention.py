"""Conversation retention and scope proofs against temporary project databases."""

from datetime import timedelta

from sqlalchemy import func, select

from novel_harness.db.base import utc_now
from novel_harness.db.models import AIJob, Idea, StoryNode


def seed_history(database, project_id, count, chapter_id=None):
    ids = []
    oldest = utc_now() - timedelta(days=400)
    with database.job_session_scope() as session:
        for index in range(count):
            job = AIJob(
                project_id=project_id,
                chapter_id=chapter_id,
                task_type="chat",
                status="succeeded",
                prompt_version="retention-fixture",
                instructions=f"作者第{index}轮：" + "保留完整的会话原文。" * 1200,
                result={"reply": f"答复第{index}轮：" + "保留完整的历史答复。" * 1800},
                created_at=oldest + timedelta(seconds=index),
                updated_at=oldest + timedelta(seconds=index),
            )
            session.add(job)
            session.flush()
            ids.append(job.id)
    return ids


def all_history_ids(client, base, chapter_id=None):
    ids = []
    params = {"limit": 29}
    if chapter_id:
        params["chapter_id"] = chapter_id
    while True:
        response = client.get(base + "/ai/jobs/page", params=params)
        assert response.status_code == 200, response.text
        page = response.json()
        ids.extend(item["id"] for item in page["items"])
        if not page["next_cursor"]:
            return ids
        params["before"] = page["next_cursor"]
        assert len(ids) < 1000, "History pagination repeated a cursor"


def test_year_old_conversations_survive_gc_full_pagination_and_scope_isolation(
    client, project, seeded_chapter
):
    base = f"/api/v1/projects/{project['id']}"
    database = client.app.state.vault_registry.require(project["id"]).database
    global_ids = seed_history(database, project["id"], 125)
    chapter_ids = seed_history(database, project["id"], 2, seeded_chapter)
    with database.job_session_scope() as session:
        expired = Idea(
            project_id=project["id"],
            title="已由作者删除的临时素材",
            content="过期测试素材",
            deleted_at=utc_now() - timedelta(days=40),
            purge_after=utc_now() - timedelta(days=10),
        )
        session.add(expired)
        session.flush()
        expired_id = expired.id
        oldest = session.get(AIJob, global_ids[0])
        original_instructions = oldest.instructions
        original_reply = oldest.result["reply"]

    # This route runs normal project maintenance and collect_expired, not only
    # the task-specific session that deliberately avoids unrelated maintenance.
    assert client.get(base + "/workspace/navigation").status_code == 200
    assert client.get(base + "/trash").status_code == 200
    with database.job_session_scope() as session:
        assert session.get(Idea, expired_id, execution_options={"include_deleted": True}) is None
        assert session.scalar(select(func.count()).select_from(AIJob)) == 127

    seen = all_history_ids(client, base)
    assert len(seen) == len(set(seen)) == 125
    assert set(seen) == set(global_ids)
    assert set(all_history_ids(client, base, seeded_chapter)) == set(chapter_ids)
    oldest_detail = client.get(base + f"/ai/jobs/{global_ids[0]}")
    assert oldest_detail.status_code == 200
    assert oldest_detail.json()["instructions"] == original_instructions
    assert oldest_detail.json()["result"]["reply"] == original_reply
    assert len(original_instructions) > 10000
    assert len(original_reply) > 16000

    other = client.post("/api/v1/projects", json={"title": "隔离的另一部作品"}).json()
    other_base = f"/api/v1/projects/{other['id']}"
    assert all_history_ids(client, other_base) == []
    assert client.get(other_base + f"/ai/jobs/{global_ids[0]}").status_code == 404


def test_chapter_trash_hides_scope_without_deleting_conversation_and_restore_recovers_list(
    client, project, seeded_chapter
):
    base = f"/api/v1/projects/{project['id']}"
    database = client.app.state.vault_registry.require(project["id"]).database
    ids = seed_history(database, project["id"], 1, seeded_chapter)
    assert all_history_ids(client, base, seeded_chapter) == ids
    assert client.delete(base + f"/library/node/{seeded_chapter}").status_code == 200
    assert (
        client.get(base + "/ai/jobs/page", params={"chapter_id": seeded_chapter}).status_code == 404
    )
    detail = client.get(base + f"/ai/jobs/{ids[0]}")
    assert detail.status_code == 200
    original_reply = detail.json()["result"]["reply"]

    # A discarded chapter cannot cascade-delete AIJob history after thirty days.
    # Its remaining task foreign key makes the attempted hard purge reversible.
    with database.job_session_scope() as session:
        chapter = session.get(
            StoryNode, seeded_chapter, execution_options={"include_deleted": True}
        )
        chapter.purge_after = utc_now() - timedelta(days=1)
    purge = client.delete(base + f"/trash/node/{seeded_chapter}/purge")
    assert purge.status_code == 409
    assert purge.json()["detail"]["code"] == "MATERIAL_STILL_REFERENCED"
    assert client.get(base + "/trash").status_code == 200
    assert client.get(base + f"/ai/jobs/{ids[0]}").json()["result"]["reply"] == original_reply
    assert client.post(base + f"/trash/node/{seeded_chapter}/restore").status_code == 200
    assert all_history_ids(client, base, seeded_chapter) == ids
