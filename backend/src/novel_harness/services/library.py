"""Typed material adapters, optimistic writes, and recoverable deletion."""

from __future__ import annotations

import json
from datetime import timedelta

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import literal, select, text, union_all, update
from sqlalchemy.exc import IntegrityError

from novel_harness.db.base import utc_now
from novel_harness.db.models import (
    Asset,
    CanonFact,
    ChapterSummary,
    Entity,
    EntityRelation,
    Idea,
    PlotThread,
    SourceDocument,
    StoryNode,
    StyleProfile,
    StyleRule,
    TimelineEvent,
)
from novel_harness.services.serialization import serialize

SOURCES = {
    "entity": (Entity, "name", "summary"),
    "relation": (EntityRelation, "relation_type", "description"),
    "idea": (Idea, "title", "content"),
    "node": (StoryNode, "title", "summary"),
    "canon": (CanonFact, "predicate", "value"),
    "timeline": (TimelineEvent, "title", "description"),
    "plot": (PlotThread, "title", "promise"),
    "style": (StyleProfile, "name", "config"),
    "style_rule": (StyleRule, "instruction", "instruction"),
    "asset": (Asset, "prompt", "prompt"),
    "summary": (ChapterSummary, "title", "recap"),
    "source_document": (SourceDocument, "title", "content"),
}

PREPARATION_FACT_SOURCE_PREFIX = "project_preparation:"


def _reject_preparation_fact_mutation(kind, item, requested_source_note=None):
    current_note = item.source_note if kind == "canon" else None
    if kind == "canon" and any(
        isinstance(note, str) and note.startswith(PREPARATION_FACT_SOURCE_PREFIX)
        for note in (current_note, requested_source_note)
    ):
        raise HTTPException(
            409,
            detail={
                "code": "PREPARATION_FACT_MANAGED",
                "message": "该确认事实由创作准备管理，请回到创作准备修改或撤回答案。",
            },
        )


def adapter(kind):
    if kind not in SOURCES:
        raise HTTPException(404, detail={"code": "MATERIAL_TYPE_NOT_FOUND"})
    return SOURCES[kind]


def present(kind, record, *, include_content=True):
    _, title_field, content_field = adapter(kind)
    data = jsonable_encoder(serialize(record))
    content = data[content_field]
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)
    result = {
        "id": record.id,
        "type": kind,
        "title": str(data[title_field]),
        "content": content,
        "preview": content[:160],
        "revision": record.revision,
        "is_pinned": record.is_pinned,
        "deleted_at": data["deleted_at"],
        "purge_after": data["purge_after"],
        "status": data.get("status"),
        "origin": data.get("origin"),
        "record": data,
    }
    if kind == "source_document" and not include_content:
        result["content"] = ""
        result["preview"] = ""
        result["record"] = {key: value for key, value in data.items() if key != "content"}
    return result


def require_item(session, kind, item_id, *, deleted=False):
    model, _, _ = adapter(kind)
    item = session.scalar(
        select(model).where(model.id == item_id).execution_options(include_deleted=deleted)
    )
    if item is None:
        raise HTTPException(404, detail={"code": "MATERIAL_NOT_FOUND"})
    return item


def detail(session, project_id, kind, item_id):
    if kind == "manuscript":
        row = (
            session.execute(
                text(
                    "SELECT n.title,v.content,v.id AS version_id,d.revision "
                    "FROM chapter_documents d JOIN story_nodes n ON n.id=d.chapter_id "
                    "JOIN chapter_versions v ON v.id=d.current_version_id "
                    "WHERE d.chapter_id=:id AND d.project_id=:project AND n.project_id=:project "
                    "AND v.project_id=:project AND v.chapter_id=d.chapter_id "
                    "AND n.deleted_at IS NULL"
                ),
                {"id": item_id, "project": project_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise HTTPException(404, detail={"code": "MATERIAL_NOT_FOUND"})
        return {
            "id": item_id,
            "type": kind,
            "title": row["title"] + " · 正文版本",
            "content": row["content"],
            "preview": row["content"][:160],
            "revision": row["revision"],
            "is_pinned": False,
            "deleted_at": None,
            "purge_after": None,
            "status": "saved",
            "origin": None,
            "record": {"chapter_id": item_id, "version_id": row["version_id"]},
        }
    item = require_item(session, kind, item_id)
    if item.project_id != project_id:
        raise HTTPException(404, detail={"code": "MATERIAL_NOT_FOUND"})
    if kind == "summary" and not session.scalar(
        select(StoryNode.id).where(
            StoryNode.id == item.chapter_id,
            StoryNode.project_id == project_id,
            StoryNode.deleted_at.is_(None),
        )
    ):
        raise HTTPException(404, detail={"code": "MATERIAL_NOT_FOUND"})
    return present(kind, item)


def list_items(session, *, deleted=False):
    items = []
    for kind, (model, _, _) in SOURCES.items():
        if kind == "source_document":
            columns = tuple(
                getattr(SourceDocument, name)
                for name in (
                    "id",
                    "project_id",
                    "import_batch_id",
                    "chapter_id",
                    "relative_path",
                    "stored_path",
                    "category",
                    "title",
                    "encoding",
                    "size_bytes",
                    "byte_hash",
                    "content_hash",
                    "content_revision",
                    "revision",
                    "is_pinned",
                    "deleted_at",
                    "purge_after",
                    "created_at",
                    "updated_at",
                )
            )
            statement = select(*columns)
            statement = statement.where(
                SourceDocument.deleted_at.is_not(None)
                if deleted
                else SourceDocument.deleted_at.is_(None)
            )
            for row in session.execute(statement).mappings():
                data = jsonable_encoder(dict(row))
                items.append(
                    {
                        "id": data["id"],
                        "type": kind,
                        "title": data["title"],
                        "content": "",
                        "preview": "",
                        "revision": data["revision"],
                        "is_pinned": data["is_pinned"],
                        "deleted_at": data["deleted_at"],
                        "purge_after": data["purge_after"],
                        "status": None,
                        "origin": None,
                        "record": data,
                    }
                )
            continue
        statement = select(model).execution_options(include_deleted=deleted)
        if deleted:
            statement = statement.where(model.deleted_at.is_not(None))
        elif kind == "summary":
            statement = statement.join(StoryNode, model.chapter_id == StoryNode.id).where(
                model.status == "valid",
                StoryNode.deleted_at.is_(None),
            )
        items.extend(
            present(kind, item, include_content=kind != "source_document")
            for item in session.scalars(statement)
        )
    return sorted(items, key=lambda i: (not i["is_pinned"], i["title"], i["id"]))


def create_item(session, project_id, kind, payload):
    if kind == "source_document":
        adapter(kind)
        raise HTTPException(422, detail={"code": "USE_TYPED_CREATION_FLOW"})
    if not payload.title.strip():
        raise HTTPException(422, detail={"code": "TITLE_REQUIRED"})
    model, title_field, body_field = adapter(kind)
    if kind in {"asset", "style_rule", "summary", "relation"}:
        raise HTTPException(422, detail={"code": "USE_TYPED_CREATION_FLOW"})
    content = payload.content
    if kind == "style":
        content = {"instructions": content}
    values = {title_field: payload.title.strip(), body_field: content}
    if kind in {"entity", "node", "plot"}:
        allowed = {
            "entity": {"character", "location", "organization", "item"},
            "node": {"volume", "chapter", "scene"},
            "plot": {"main", "subplot", "character", "foreshadowing"},
        }
        default = {"entity": "character", "node": "chapter", "plot": "main"}
        subtype = payload.kind or default[kind]
        if subtype not in allowed[kind]:
            raise HTTPException(422, detail={"code": "INVALID_KIND"})
        values["kind"] = subtype
    if kind == "idea":
        values["tags"] = payload.tags
    record = model(project_id=project_id, **values)
    session.add(record)
    session.flush()
    return present(kind, record)


def edit_item(session, kind, item_id, payload):
    from novel_harness.services.material_fields import validate_fields
    from novel_harness.services.versions import begin_version_write

    begin_version_write(session)
    if kind == "source_document" and (
        payload.title is not None or payload.content is not None or payload.fields
    ):
        raise HTTPException(422, detail={"code": "IMMUTABLE_IMPORT_SOURCE"})
    if kind == "summary" and (payload.content is not None or payload.title is not None):
        raise HTTPException(422, detail={"code": "USE_SUMMARY_EDITOR"})
    model, title_field, body_field = adapter(kind)
    item = require_item(session, kind, item_id)
    _reject_preparation_fact_mutation(kind, item, payload.fields.get("source_note"))
    values = {}
    if payload.title is not None:
        if not payload.title.strip():
            raise HTTPException(422, detail={"code": "TITLE_REQUIRED"})
        values[title_field] = payload.title.strip()
    if payload.content is not None:
        values[body_field] = payload.content
        if kind == "style":
            values[body_field] = {**item.config, "instructions": payload.content}
    if payload.is_pinned is not None:
        values["is_pinned"] = payload.is_pinned
    values.update(validate_fields(session, kind, item, payload.fields, values))
    values["revision"] = payload.revision + 1
    result = session.execute(
        update(model)
        .where(model.id == item_id, model.revision == payload.revision, model.deleted_at.is_(None))
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        session.refresh(item)
        raise HTTPException(
            409, detail={"code": "revision_conflict", "current": present(kind, item)}
        )
    session.refresh(item)
    if kind == "source_document":
        from novel_harness.services.search_index import update_source_document_pin_projection

        update_source_document_pin_projection(session, item)
    else:
        from novel_harness.services.search_index import sync_record

        sync_record(session, item)
    if kind == "summary":
        from novel_harness.services.chapter_summaries import write_ledger

        write_ledger(session)
    return present(kind, item)


def trash_item(session, kind, item_id):
    if kind in {"entity", "node"}:
        from novel_harness.services.versions import begin_version_write

        begin_version_write(session)
    item = require_item(session, kind, item_id)
    _reject_preparation_fact_mutation(kind, item)
    dependent_capture = None
    if kind == "entity":
        from novel_harness.services.search_index import capture_entity_dependents

        dependent_capture = capture_entity_dependents(session, item.id)
    if kind == "node" and session.scalar(
        select(StoryNode.id).where(StoryNode.parent_id == item_id)
    ):
        raise HTTPException(
            409,
            detail={"code": "NODE_HAS_CHILDREN", "message": "请先处理此大纲节点下的章节或场景。"},
        )
    item.deleted_at = utc_now()
    item.purge_after = item.deleted_at + timedelta(days=30)
    item.revision += 1
    if kind == "summary" and item.status == "valid":
        chapter = session.get(StoryNode, item.chapter_id)
        if chapter and not chapter.deleted_at:
            chapter.status = "summary_pending"
    session.flush()
    dependent_results = []
    if kind == "entity":
        from novel_harness.services.search_index import sync_entity_dependents

        dependent_results = sync_entity_dependents(
            session,
            dependent_capture,
            entity_id=item.id,
            entity_project_id=item.project_id,
            entity_active=False,
        )
    if kind in {"node", "summary"}:
        refresh_continuity(session)
    result = present(kind, item)
    if kind == "entity":
        result["inactive_dependents"] = [
            {
                "type": dependent["type"],
                "id": dependent["id"],
                "reason": "entity_deleted",
            }
            for dependent in dependent_results
            if dependent["pre_active"] and not dependent["post_active"]
        ]
    return result


def restore_item(session, kind, item_id):
    if kind in {"entity", "node"}:
        from novel_harness.services.versions import begin_version_write

        begin_version_write(session)
    item = require_item(session, kind, item_id, deleted=True)
    if item.deleted_at is None:
        raise HTTPException(404, detail={"code": "MATERIAL_NOT_FOUND"})
    dependent_capture = None
    if kind == "entity":
        from novel_harness.services.search_index import capture_entity_dependents

        dependent_capture = capture_entity_dependents(session, item.id)
    if kind == "node":
        from novel_harness.services.material_fields import validate_node_ancestors

        validate_node_ancestors(session, item, item.parent_id)
    if kind == "summary":
        import hashlib

        from novel_harness.db.models import ChapterDocument

        document = session.get(ChapterDocument, item.chapter_id)
        if (
            not document
            or hashlib.sha256(document.content.encode()).hexdigest() != item.content_hash
        ):
            item.status = "stale"
        current = session.scalar(
            select(ChapterSummary).where(
                ChapterSummary.chapter_id == item.chapter_id,
                ChapterSummary.status == "valid",
                ChapterSummary.id != item_id,
            )
        )
        if current:
            item.status = "superseded"
    item.deleted_at = None
    item.purge_after = None
    item.revision += 1
    session.flush()
    dependent_results = []
    if kind == "entity":
        from novel_harness.services.search_index import sync_entity_dependents

        dependent_results = sync_entity_dependents(
            session,
            dependent_capture,
            entity_id=item.id,
            entity_project_id=item.project_id,
            entity_active=True,
        )
    if kind in {"node", "summary"}:
        refresh_continuity(session)
    result = present(kind, item)
    if kind == "entity":
        result["reactivated_dependents"] = [
            {"type": dependent["type"], "id": dependent["id"]}
            for dependent in dependent_results
            if not dependent["pre_active"] and dependent["post_active"]
        ]
    return result


def refresh_continuity(session):
    from novel_harness.services.chapter_summaries import write_ledger
    from novel_harness.services.search_index import rebuild_index

    rebuild_index(session)
    write_ledger(session)


def collect_expired(session):
    """Opportunistic local GC: when closed, expired records are collected on next use."""
    collected = 0
    now = utc_now()
    queries = []
    for kind, (model, _, _) in SOURCES.items():
        queries.append(
            select(literal(kind).label("kind"), model.id).where(
                model.deleted_at.is_not(None), model.purge_after <= now
            )
        )
    expired = session.execute(union_all(*queries).execution_options(include_deleted=True)).all()
    for kind, item_id in expired:
        try:
            purge_item(session, kind, item_id)
            collected += 1
        except HTTPException as exc:
            if exc.status_code != 409:
                raise
    return collected


def purge_item(session, kind, item_id):
    item = require_item(session, kind, item_id, deleted=True)
    _reject_preparation_fact_mutation(kind, item)
    if not item.deleted_at or not item.purge_after or item.purge_after > utc_now():
        raise HTTPException(422, detail={"code": "RETENTION_NOT_EXPIRED"})
    # sqlite3 legacy transaction mode does not start a transaction for SELECT.
    # Ensure RELEASE SAVEPOINT cannot accidentally commit a standalone purge.
    if not session.connection().connection.driver_connection.in_transaction:
        session.execute(text("BEGIN IMMEDIATE"))
    try:
        with session.begin_nested():
            session.delete(item)
            session.flush()
    except IntegrityError as exc:
        raise HTTPException(
            409,
            detail={
                "code": "MATERIAL_STILL_REFERENCED",
                "message": "仍有设定或版本引用此素材，请先处理关联内容。",
            },
        ) from exc
    if kind == "asset" and item.relative_path:
        session.execute(
            text("INSERT OR IGNORE INTO file_delete_queue VALUES (:path)"),
            {"path": item.relative_path},
        )
    if kind in {"node", "summary"}:
        refresh_continuity(session)
    return {"purged": True}
