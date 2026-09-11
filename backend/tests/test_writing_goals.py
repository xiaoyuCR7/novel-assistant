from datetime import datetime

import pytest
from sqlalchemy import select, text

from novel_harness.db.models import AIJob, ChapterDocument, Project, StoryNode


def path(project):
    return f"/api/v1/projects/{project['id']}/writing-goals"


def command(revision=0, **changes):
    return {
        "revision": revision,
        "target_words": 120000,
        "daily_goal": 1200,
        "weekly_chapters": 3,
        "deadline": "2026-12-31",
        **changes,
    }


def test_defaults_reuse_existing_project_goals_without_creating_a_settings_record(client, project):
    response = client.get(path(project))
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["goals"] == {
        "revision": 0,
        "target_words": 300000,
        "daily_goal": 1800,
        "weekly_chapters": 0,
        "deadline": None,
    }
    assert data["progress"]["saved_words"] == 0
    assert data["week"]["completed_chapters"] is None
    database = client.app.state.vault_registry.require(project["id"]).database
    with database.job_session_scope() as session:
        assert session.execute(text("SELECT COUNT(*) FROM project_writing_goals")).scalar() == 0


def test_revision_conflict_keeps_other_window_goals_and_can_explicitly_clear_deadline(
    client, project
):
    first = client.put(path(project), json=command())
    assert first.status_code == 200, first.text
    assert first.json()["goals"] == command(revision=1)
    stale = client.put(path(project), json=command(target_words=9, deadline=None))
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "WRITING_GOALS_CHANGED"
    assert stale.json()["detail"]["current"] == command(revision=1)
    assert client.get(path(project)).json()["goals"] == command(revision=1)
    cleared = client.put(
        path(project), json=command(revision=1, weekly_chapters=0, daily_goal=0, deadline=None)
    )
    assert cleared.status_code == 200
    assert cleared.json()["goals"] == command(
        revision=2, weekly_chapters=0, daily_goal=0, deadline=None
    )
    database = client.app.state.vault_registry.require(project["id"]).database
    with database.job_session_scope() as session:
        stored = session.get(Project, project["id"])
        assert (stored.target_words, stored.daily_goal) == (120000, 0)


@pytest.mark.parametrize(
    "invalid",
    [
        {"target_words": 0},
        {"daily_goal": -1},
        {"weekly_chapters": 1.5},
        {"deadline": "2026-02-30"},
        {"revision": -1},
        {"target_words": True},
    ],
)
def test_invalid_goals_do_not_change_the_project(client, project, invalid):
    response = client.put(path(project), json=command(**invalid))
    assert response.status_code == 422, response.text
    assert client.get(path(project)).json()["goals"]["target_words"] == 300000


def test_progress_counts_saved_work_separately_from_candidates_and_author_completion(
    client, project
):
    from test_project_todos import seed_todos

    database = seed_todos(client, project)
    with database.job_session_scope() as session:
        document = session.get(ChapterDocument, "chapter-0")
        document.content = "甲 乙\n丙"  # Three saved characters, no formal version.
        session.get(StoryNode, "chapter-0").status = "drafting"
        session.get(StoryNode, "chapter-1").status = "completed"
        session.add(
            ChapterDocument(chapter_id="chapter-1", project_id=project["id"], content="已完成")
        )
        session.get(AIJob, "quality").result = {
            "chapters": [
                {
                    "chapter_id": "chapter-2",
                    "status": "ready",
                    "candidate_text": "模型候选不得计入已写字数",
                    "after": {"scores": {"readability": 99}},
                }
            ]
        }
    response = client.get(path(project))
    assert response.status_code == 200, response.text
    progress = response.json()["progress"]
    assert progress == {
        "saved_words": 6,
        "saved_chapters": 2,
        "draft_chapters": 1,
        "pending_review_candidates": 2,
        "author_completed_chapters": 1,
        "chapter_count": 3,
    }
    assert response.json()["week"]["completed_chapters"] is None
    with database.job_session_scope() as session:
        session.get(StoryNode, "chapter-1").deleted_at = datetime(2026, 1, 1)
    visible = client.get(path(project)).json()["progress"]
    assert visible["saved_words"] == 3
    assert visible["author_completed_chapters"] == 0
    assert visible["chapter_count"] == 2


def test_week_uses_monday_and_utc_plus_eight_without_inventing_completion_history(
    client, project, monkeypatch
):
    from novel_harness.services import writing_goals

    monkeypatch.setattr(writing_goals, "utc_now", lambda: datetime(2026, 9, 13, 16, 0))
    response = client.get(path(project))
    assert response.status_code == 200, response.text
    assert response.json()["week"] == {
        "timezone": "Asia/Shanghai (UTC+08:00)",
        "start_date": "2026-09-14",
        "end_date": "2026-09-20",
        "today": "2026-09-14",
        "completed_chapters": None,
    }
    monkeypatch.setattr(writing_goals, "utc_now", lambda: datetime(2026, 9, 13, 15, 59))
    assert client.get(path(project)).json()["week"]["start_date"] == "2026-09-07"


def test_old_project_database_adds_goals_table_without_resetting_existing_targets(client, project):
    database = client.app.state.vault_registry.require(project["id"]).database
    with database.engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE IF EXISTS project_writing_goals")
    database.dispose()
    database.create_schema()
    response = client.get(path(project))
    assert response.status_code == 200, response.text
    assert response.json()["goals"]["target_words"] == 300000
    assert client.put(path(project), json=command()).status_code == 200
    with database.job_session_scope() as session:
        assert (
            session.scalar(select(Project.target_words).where(Project.id == project["id"]))
            == 120000
        )


def test_goals_survive_a_new_real_backend_process(tmp_path):
    from test_conversation_process_restart import running_backend

    root = tmp_path / "writing-goals-vault"
    with running_backend(root) as (client, first_pid):
        project = client.post("/api/v1/projects", json={"title": "目标重启验收"}).json()
        response = client.put(path(project), json=command())
        assert response.status_code == 200, response.text
        saved = response.json()["goals"]
    with running_backend(root) as (client, second_pid):
        assert first_pid != second_pid
        assert client.get(path(project)).json()["goals"] == saved
