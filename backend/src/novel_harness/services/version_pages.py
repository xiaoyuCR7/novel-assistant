"""Lightweight, cursor-paginated chapter-version history."""

from fastapi import HTTPException
from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from novel_harness.db.models import ChapterVersion
from novel_harness.services.versions import require_chapter


def read_page(
    session: Session, chapter_id: str, limit: int, before: str | None,
) -> dict[str, list[dict] | str | None]:
    """Read one metadata-only page without loading immutable version content."""
    require_chapter(session, chapter_id)
    scope = [ChapterVersion.chapter_id == chapter_id]
    if before is not None:
        cursor = session.execute(
            select(ChapterVersion.id, ChapterVersion.chapter_id, ChapterVersion.created_at).where(
                ChapterVersion.id == before
            )
        ).mappings().one_or_none()
        if cursor is None or cursor["chapter_id"] != chapter_id:
            raise HTTPException(
                422,
                detail={
                    "code": "INVALID_VERSION_CURSOR",
                    "message": "版本分页游标无效，或不属于当前章节。",
                },
            )
        scope.append(
            tuple_(ChapterVersion.created_at, ChapterVersion.id)
            < tuple_(cursor["created_at"], cursor["id"])
        )
    rows = session.execute(
        select(
            ChapterVersion.id,
            ChapterVersion.chapter_id,
            ChapterVersion.created_at,
            ChapterVersion.source,
            ChapterVersion.summary,
            ChapterVersion.word_count,
            ChapterVersion.parent_version_id,
            ChapterVersion.restored_from_version_id,
            ChapterVersion.generation_job_id,
        )
        .where(*scope)
        .order_by(ChapterVersion.created_at.desc(), ChapterVersion.id.desc())
        .limit(limit + 1)
    ).mappings().all()
    more, rows = len(rows) > limit, rows[:limit]
    return {
        "items": [dict(row) for row in rows],
        "next_cursor": rows[-1]["id"] if more else None,
    }
