"""Saved manuscript progress and revision-protected author goals; never invokes a model."""

from datetime import UTC, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import select, text

from novel_harness.db.base import utc_now
from novel_harness.db.models import ChapterDocument, ProjectWritingGoals, StoryNode
from novel_harness.services.project_todos import read_todos
from novel_harness.services.projects import require_project
from novel_harness.services.versions import begin_version_write, count_words


def _goals(project, record):
    return {
        "revision": record.revision if record else 0,
        "target_words": project.target_words,
        "daily_goal": project.daily_goal,
        "weekly_chapters": record.weekly_chapters if record else 0,
        "deadline": record.deadline.isoformat() if record and record.deadline else None,
    }


def read_writing_goals(session, project_id):
    # Keep the goals, saved manuscript and candidate counts in one read snapshot.
    if not session.connection().connection.driver_connection.in_transaction:
        session.execute(text("BEGIN"))
    project = require_project(session, project_id)
    record = session.get(ProjectWritingGoals, project_id)
    progress = {
        "saved_words": 0,
        "saved_chapters": 0,
        "draft_chapters": 0,
        "pending_review_candidates": 0,
        "author_completed_chapters": 0,
        "chapter_count": 0,
    }
    rows = session.execute(
        select(StoryNode.status, ChapterDocument.content)
        .outerjoin(ChapterDocument, ChapterDocument.chapter_id == StoryNode.id)
        .where(
            StoryNode.project_id == project_id,
            StoryNode.kind == "chapter",
            StoryNode.deleted_at.is_(None),
        )
    )
    for status, content in rows:
        progress["chapter_count"] += 1
        words = count_words(content or "")
        progress["saved_words"] += words
        if words:
            progress["saved_chapters"] += 1
            progress[
                "author_completed_chapters" if status == "completed" else "draft_chapters"
            ] += 1
    # Count candidate items, not chapters: multiple suggestions can belong to the same chapter.
    counts = read_todos(session, project_id, limit=1)["counts"]
    progress["pending_review_candidates"] = counts.get("writing_candidate", 0) + counts.get(
        "quality_candidate", 0
    )
    today = utc_now().replace(tzinfo=UTC).astimezone(timezone(timedelta(hours=8))).date()
    start = today - timedelta(days=today.weekday())
    return {
        "goals": _goals(project, record),
        "progress": progress,
        "week": {
            "timezone": "Asia/Shanghai (UTC+08:00)",
            "start_date": start.isoformat(),
            "end_date": (start + timedelta(days=6)).isoformat(),
            "today": today.isoformat(),
            # Historical status changes have no reliable author-completion timestamp.
            "completed_chapters": None,
        },
    }


def update_writing_goals(session, project_id, payload):
    begin_version_write(session)
    project = require_project(session, project_id)
    record = session.get(ProjectWritingGoals, project_id)
    current = _goals(project, record)
    if payload.revision != current["revision"]:
        raise HTTPException(
            409,
            detail={
                "code": "WRITING_GOALS_CHANGED",
                "message": "创作目标已在其他页面更新，本次输入未覆盖已保存目标。请核对后重新修改。",
                "current": current,
            },
        )
    if record is None:
        record = ProjectWritingGoals(project_id=project_id, revision=1)
        session.add(record)
    else:
        record.revision += 1
    project.target_words = payload.target_words
    project.daily_goal = payload.daily_goal
    record.weekly_chapters = payload.weekly_chapters
    record.deadline = payload.deadline
    session.flush()
    return read_writing_goals(session, project_id)
