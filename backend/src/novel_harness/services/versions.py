"""Chapter working copy and append-only version operations."""

from __future__ import annotations

import json
from difflib import unified_diff

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from novel_harness.db.models import ChapterDocument, ChapterVersion, Entity, StoryNode
from novel_harness.services.serialization import serialize


def count_words(text: str) -> int:
    return len("".join(text.split()))


def begin_version_write(session: Session) -> None:
    # SQLite legacy SELECTs do not acquire a transaction. Lock before reading
    # the revision, then discard stale identity-map values from concurrent tabs.
    if not session.connection().connection.driver_connection.in_transaction:
        session.execute(text("BEGIN IMMEDIATE"))
        session.expire_all()


def require_chapter(session: Session, chapter_id: str) -> StoryNode:
    chapter = session.get(StoryNode, chapter_id)
    if chapter is None or chapter.deleted_at:
        raise HTTPException(status_code=404, detail={"code": "CHAPTER_NOT_FOUND"})
    if chapter.kind != "chapter":
        raise HTTPException(status_code=409, detail={"code": "NODE_IS_NOT_CHAPTER"})
    return chapter


def get_or_create_document(session: Session, chapter_id: str) -> ChapterDocument:
    chapter = require_chapter(session, chapter_id)
    document = session.get(ChapterDocument, chapter_id)
    if document is None:
        document = ChapterDocument(chapter_id=chapter_id, project_id=chapter.project_id)
        session.add(document)
        session.flush()
    return document


def update_document(
    session: Session, chapter_id: str, content: str, contract: dict, revision: int | None = None
) -> ChapterDocument:
    document = get_or_create_document(session, chapter_id)
    from novel_harness.services.references import resolve_references

    resolve_references(session, content + "\n" + json.dumps(contract, ensure_ascii=False))
    for field in ("pov_entity_id", "location_entity_id"):
        value = contract.get(field)
        if value and (not isinstance(value, str) or session.get(Entity, value) is None):
            raise HTTPException(
                422,
                detail={
                    "code": "REFERENCE_UNAVAILABLE",
                    "message": "章节契约引用的人物或地点不属于当前项目，或已删除。",
                },
            )
    changed = document.content != content
    expected = document.revision if revision is None else revision
    result = session.execute(
        update(ChapterDocument)
        .where(ChapterDocument.chapter_id == chapter_id, ChapterDocument.revision == expected)
        .values(content=content, contract=contract, revision=expected + 1)
        .execution_options(synchronize_session=False)
    )
    session.refresh(document)
    if result.rowcount != 1:
        raise HTTPException(
            409,
            detail={
                "code": "revision_conflict",
                "current": jsonable_encoder(serialize_document(document)),
                "message": "正文已在其他窗口更新，本次草稿未覆盖服务器内容。请复制保留后重新加载。",
            },
        )
    if changed:
        from novel_harness.services.chapter_summaries import invalidate_summary

        invalidate_summary(session, chapter_id)
    return document


def create_version(
    session: Session,
    chapter_id: str,
    *,
    content: str,
    source: str,
    summary: str = "",
    generation_job_id: str | None = None,
    restored_from_version_id: str | None = None,
    expected_revision: int | None = None,
    version_id: str | None = None,
) -> ChapterVersion:
    begin_version_write(session)
    document = get_or_create_document(session, chapter_id)
    if expected_revision is not None and document.revision != expected_revision:
        raise HTTPException(
            409,
            detail={
                "code": "revision_conflict",
                "current": jsonable_encoder(serialize_document(document)),
                "message": "正文已更新，本次版本未覆盖新稿。请保留草稿并重新加载。",
            },
        )
    from novel_harness.services.references import resolve_references

    resolve_references(session, content)
    if document.content != content:
        from novel_harness.services.chapter_summaries import invalidate_summary

        invalidate_summary(session, chapter_id)
    identity = {"id": version_id} if version_id is not None else {}
    version = ChapterVersion(
        **identity,
        chapter_id=chapter_id,
        project_id=document.project_id,
        parent_version_id=document.current_version_id,
        restored_from_version_id=restored_from_version_id,
        generation_job_id=generation_job_id,
        content=content,
        summary=summary,
        word_count=count_words(content),
        source=source,
    )
    session.add(version)
    session.flush()
    document.content = content
    document.current_version_id = version.id
    document.revision += 1
    session.flush()
    return version


def require_version(session: Session, chapter_id: str, version_id: str) -> ChapterVersion:
    require_chapter(session, chapter_id)
    version = session.get(ChapterVersion, version_id)
    if version is None:
        raise HTTPException(status_code=404, detail={"code": "VERSION_NOT_FOUND"})
    if version.chapter_id != chapter_id:
        raise HTTPException(status_code=409, detail={"code": "CROSS_CHAPTER_VERSION"})
    return version


def list_versions(session: Session, chapter_id: str) -> list[ChapterVersion]:
    require_chapter(session, chapter_id)
    return list(
        session.scalars(
            select(ChapterVersion)
            .where(ChapterVersion.chapter_id == chapter_id)
            .order_by(ChapterVersion.created_at.desc())
        ).all()
    )


def restore_version(
    session: Session, chapter_id: str, version_id: str, expected_revision: int,
) -> ChapterVersion:
    begin_version_write(session)
    target = require_version(session, chapter_id, version_id)
    document = get_or_create_document(session, chapter_id)
    if document.revision != expected_revision:
        raise HTTPException(409, detail={
            'code': 'revision_conflict',
            'message': '正文已更新，恢复未覆盖新稿。请保留草稿并重新加载。',
            'current': jsonable_encoder(serialize_document(document)),
        })
    return create_version(
        session,
        chapter_id,
        content=target.content,
        source="restore",
        summary=f"恢复自版本 {target.id}",
        restored_from_version_id=target.id,
    )


def diff_versions(session: Session, chapter_id: str, from_id: str, to_id: str) -> str:
    before = require_version(session, chapter_id, from_id)
    after = require_version(session, chapter_id, to_id)
    return "\n".join(
        unified_diff(
            before.content.splitlines(),
            after.content.splitlines(),
            fromfile=before.id,
            tofile=after.id,
            lineterm="",
        )
    )


def serialize_document(document: ChapterDocument) -> dict:
    from sqlalchemy.orm import object_session

    session = object_session(document)
    chapter = require_chapter(session, document.chapter_id)
    return {**serialize(document), "status": chapter.status}
