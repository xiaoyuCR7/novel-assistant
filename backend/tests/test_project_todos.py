from novel_harness.db.base import utc_now
from novel_harness.db.job_models import AIJobControl
from novel_harness.db.models import (
    AIJob,
    ChapterDocument,
    ChapterSummary,
    ChapterVersion,
    Conflict,
    ConversationJob,
    ConversationThread,
    StoryNode,
)


def seed_todos(client, project):
    database = client.app.state.vault_registry.require(project["id"]).database
    with database.session_scope() as session:
        for index in range(3):
            session.add(
                StoryNode(
                    id=f"chapter-{index}",
                    project_id=project["id"],
                    kind="chapter",
                    title=f"跨章{index}",
                    order_index=index,
                )
            )
        session.flush()
        session.add(
            ChapterDocument(
                chapter_id="chapter-0", project_id=project["id"], content="待完成的正文", revision=1
            )
        )
        for identifier, chapter, status, result, kind in (
            ("failed", "chapter-1", "failed", {}, "chat"),
            ("candidate", "chapter-2", "succeeded", {"candidate_text": "待采纳稿"}, "draft"),
            (
                "quality",
                None,
                "succeeded",
                {
                    "chapters": [
                        {
                            "chapter_id": "chapter-2",
                            "status": "ready",
                            "candidate_text": "优化候选",
                        }
                    ]
                },
                "quality_workflow",
            ),
        ):
            session.add(
                AIJob(
                    id=identifier,
                    project_id=project["id"],
                    chapter_id=chapter,
                    task_type=kind,
                    status=status,
                    result=result,
                    prompt_version="v2",
                )
            )
        session.flush()
        session.add(
            AIJobControl(
                job_id="quality",
                operation="quality",
                idempotency_key="q",
                request_hash="q",
                command={},
                effects={},
            )
        )
        session.add(
            Conflict(
                id="conflict",
                project_id=project["id"],
                chapter_id="chapter-2",
                code="TEST",
                severity="warning",
                message="需确认的情节冲突",
            )
        )
    return database


def test_todos_span_chapters_with_complete_counts_and_project_isolation(client, project):
    seed_todos(client, project)
    path = f"/api/v1/projects/{project['id']}/todos"
    response = client.get(path, params={"limit": 2})
    assert response.status_code == 200, response.text
    page = response.json()
    assert page["total"] == 5 and sum(page["counts"].values()) == 5
    items = list(page["items"])
    while page["next_offset"] is not None:
        page = client.get(path, params={"limit": 2, "offset": page["next_offset"]}).json()
        items.extend(page["items"])
    assert len({item["id"] for item in items}) == 5
    assert {item["chapter_id"] for item in items} == {"chapter-0", "chapter-1", "chapter-2"}
    assert {item["destination"] for item in items} == {"ai", "quality", "write", "conflicts"}
    assert all("candidate_text" not in item for item in items)
    other = client.post("/api/v1/projects", json={"title": "另一部小说"}).json()
    assert client.get(f"/api/v1/projects/{other['id']}/todos").json()["total"] == 0
    assert client.get(path, params={"limit": 101}).status_code == 422


def test_handled_todos_disappear_without_triggering_models(client, project):
    database = seed_todos(client, project)
    with database.session_scope() as session:
        session.get(AIJob, "failed").status = "cancelled"
        session.get(AIJob, "candidate").accepted_version_id = "accepted"
        session.get(AIJobControl, "quality").effects = {"accepted_chapters": {"chapter-2": "v2"}}
        session.get(Conflict, "conflict").status = "dismissed"
        session.add(
            ChapterVersion(
                id="version",
                project_id=project["id"],
                chapter_id="chapter-0",
                content="待完成的正文",
                source="manual",
            )
        )
        session.flush()
        session.get(ChapterDocument, "chapter-0").current_version_id = "version"
        session.get(StoryNode, "chapter-0").status = "completed"
        session.add(
            ChapterSummary(
                project_id=project["id"],
                chapter_id="chapter-0",
                version_id="version",
                title="跨章0",
                content_hash="hash",
                recap="总结",
            )
        )
    page = client.get(f"/api/v1/projects/{project['id']}/todos").json()
    assert page["total"] == 0
    assert not client.app.state.job_executor.run_once()


def test_todos_hide_deleted_chapters_and_archived_conversations(client, project):
    database = seed_todos(client, project)
    with database.session_scope() as session:
        session.get(StoryNode, "chapter-0").deleted_at = utc_now()
        session.add(
            ConversationThread(
                id="archived",
                project_id=project["id"],
                chapter_id="chapter-1",
                title="旧讨论",
                status="archived",
            )
        )
        session.flush()
        session.add(ConversationJob(job_id="failed", conversation_id="archived"))
    page = client.get(f"/api/v1/projects/{project['id']}/todos").json()
    assert page["total"] == 3
    assert all(item["chapter_id"] == "chapter-2" for item in page["items"])
