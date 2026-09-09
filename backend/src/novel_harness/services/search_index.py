"""Transactional FTS5 projections. Chinese runs receive unigram/bigram tokens."""

import json
import re

from sqlalchemy import event, literal, select, text, union_all, update
from sqlalchemy.orm import Session, with_loader_criteria

from novel_harness.db.models import (
    CanonFact,
    ChapterDocument,
    ChapterSummary,
    ChapterVersion,
    Entity,
    EntityRelation,
    LibraryRecord,
    StoryNode,
    StyleRule,
)
from novel_harness.services.chunks import project_chunks, source_hash, source_text

SEARCH_INDEX_VERSION = 2


def tokenize(value):
    words = re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]", value.lower())
    for run in re.findall(r"[\u3400-\u9fff]+", value):
        words.extend(run[i : i + 2] for i in range(len(run) - 1))
    return list(dict.fromkeys(words))


def create_index(connection):
    tables = set(connection.scalars(text("SELECT name FROM sqlite_master WHERE type='table'")))
    state_existed = "search_index_state" in tables
    projection_tables = {
        "search_documents",
        "search_chunks",
        "search_fts",
        "search_chunk_fts",
    }
    projection_existed = bool(projection_tables & tables)
    projection_complete = projection_tables <= tables
    business_data_existed = "projects" in tables and bool(
        connection.scalar(text("SELECT EXISTS(SELECT 1 FROM projects LIMIT 1)"))
    )
    connection.execute(
        text(
            "CREATE TABLE IF NOT EXISTS search_index_state "
            "(id INTEGER PRIMARY KEY, version INTEGER NOT NULL)"
        )
    )
    if state_existed:
        connection.execute(
            text("INSERT OR IGNORE INTO search_index_state(id,version) VALUES (1,0)")
        )
        connection.execute(
            text(
                "UPDATE search_index_state SET version=0 "
                "WHERE id=1 AND (version!=:version OR :complete=0)"
            ),
            {"version": SEARCH_INDEX_VERSION, "complete": projection_complete},
        )
    else:
        connection.execute(
            text("INSERT INTO search_index_state(id,version) VALUES (1,:version)"),
            {
                "version": 0
                if projection_existed or business_data_existed
                else SEARCH_INDEX_VERSION
            },
        )
    connection.execute(
        text("CREATE TABLE IF NOT EXISTS file_delete_queue (relative_path TEXT PRIMARY KEY)")
    )
    connection.execute(
        text("""CREATE TABLE IF NOT EXISTS search_documents (
        key TEXT PRIMARY KEY, source_type TEXT NOT NULL, source_id TEXT NOT NULL,
        title TEXT NOT NULL, body TEXT NOT NULL, data TEXT NOT NULL)""")
    )
    connection.execute(
        text("""CREATE VIRTUAL TABLE IF NOT EXISTS search_fts
        USING fts5(key UNINDEXED, tokens, tokenize='unicode61')""")
    )
    columns = {row[1] for row in connection.execute(text("PRAGMA table_info(search_documents)"))}
    for name, definition in {
        "source_hash": "TEXT NOT NULL DEFAULT ''",
        "chapter_id": "TEXT",
        "valid_from_node_id": "TEXT",
        "valid_to_node_id": "TEXT",
        "is_pinned": "INTEGER NOT NULL DEFAULT 0",
        "status": "TEXT NOT NULL DEFAULT ''",
        "linked_nodes": "TEXT NOT NULL DEFAULT '[]'",
    }.items():
        if name not in columns:
            connection.execute(text(f"ALTER TABLE search_documents ADD COLUMN {name} {definition}"))
    connection.execute(
        text("""CREATE TABLE IF NOT EXISTS search_chunks (
        chunk_key TEXT PRIMARY KEY, document_key TEXT NOT NULL,
        strategy TEXT NOT NULL, ordinal INTEGER NOT NULL,
        start_offset INTEGER NOT NULL, end_offset INTEGER NOT NULL,
        source_hash TEXT NOT NULL, chunk_hash TEXT NOT NULL, body TEXT NOT NULL)""")
    )
    connection.execute(
        text("CREATE INDEX IF NOT EXISTS chunks_document ON search_chunks(document_key)")
    )
    connection.execute(
        text("CREATE INDEX IF NOT EXISTS documents_chapter ON search_documents(chapter_id)")
    )
    connection.execute(
        text("""CREATE VIRTUAL TABLE IF NOT EXISTS search_chunk_fts
        USING fts5(chunk_key UNINDEXED, tokens, tokenize='unicode61')""")
    )


def remove_projection(session, key):
    session.execute(
        text(
            "DELETE FROM search_chunk_fts WHERE chunk_key IN "
            "(SELECT chunk_key FROM search_chunks WHERE document_key=:key)"
        ),
        {"key": key},
    )
    session.execute(text("DELETE FROM search_chunks WHERE document_key=:key"), {"key": key})
    session.execute(text("DELETE FROM search_fts WHERE key=:key"), {"key": key})
    session.execute(text("DELETE FROM search_documents WHERE key=:key"), {"key": key})


def dependencies_active(session, record, *, entity_activity=None):
    """Derive dependent material activity from authoritative entity tombstones."""
    if isinstance(record, CanonFact):
        entity_ids = [record.subject_entity_id] if record.subject_entity_id else []
    elif isinstance(record, EntityRelation):
        entity_ids = [record.source_entity_id, record.target_entity_id]
    else:
        return True
    if not entity_ids:
        return True
    required_ids = set(entity_ids)
    if entity_activity is not None:
        return all(
            entity_activity.get(entity_id) == record.project_id
            for entity_id in required_ids
        )
    active_ids = set(
        session.scalars(
            select(Entity.id)
            .where(
                Entity.id.in_(required_ids),
                Entity.project_id == record.project_id,
                Entity.deleted_at.is_(None),
            )
            .execution_options(include_deleted=True)
        )
    )
    return active_ids == required_ids


def dependency_eligibility_filters():
    """SQL equivalents of ``dependencies_active`` for candidate-set filtering."""
    return (
        "(d.source_type != 'canon' OR EXISTS ("
        "SELECT 1 FROM canon_facts cf WHERE cf.id=d.source_id "
        "AND cf.deleted_at IS NULL AND cf.status='confirmed' "
        "AND (cf.subject_entity_id IS NULL OR EXISTS ("
        "SELECT 1 FROM entities ce WHERE ce.id=cf.subject_entity_id "
        "AND ce.project_id=cf.project_id AND ce.deleted_at IS NULL))))",
        "(d.source_type != 'relation' OR EXISTS ("
        "SELECT 1 FROM entity_relations er "
        "JOIN entities source_entity ON source_entity.id=er.source_entity_id "
        "JOIN entities target_entity ON target_entity.id=er.target_entity_id "
        "WHERE er.id=d.source_id AND er.deleted_at IS NULL "
        "AND source_entity.project_id=er.project_id "
        "AND target_entity.project_id=er.project_id "
        "AND source_entity.deleted_at IS NULL AND target_entity.deleted_at IS NULL))",
    )


def record_eligible(
    session, record, *, include_dependencies=True, entity_activity=None
):
    """Return whether a source record is eligible for its active projection."""
    if record.deleted_at is not None:
        return False
    if include_dependencies and not dependencies_active(
        session, record, entity_activity=entity_activity
    ):
        return False
    if isinstance(record, CanonFact) and record.status != "confirmed":
        return False
    if isinstance(record, StyleRule) and record.status != "confirmed":
        return False
    if isinstance(record, ChapterSummary):
        if record.status != "valid":
            return False
        chapter = session.get(StoryNode, record.chapter_id)
        if chapter is None or chapter.deleted_at is not None:
            return False
    return True


def capture_entity_dependents(session, entity_id):
    """Capture dependent rows and endpoint liveness with two bounded SELECTs."""
    canon = select(
        literal("canon").label("type"),
        CanonFact.id.label("id"),
        CanonFact.project_id.label("project_id"),
        CanonFact.subject_entity_id.label("subject_entity_id"),
        literal(None).label("source_entity_id"),
        literal(None).label("target_entity_id"),
        CanonFact.predicate.label("predicate"),
        CanonFact.value.label("value"),
        CanonFact.source_note.label("source_note"),
        CanonFact.source_version_id.label("source_version_id"),
        CanonFact.valid_from_node_id.label("valid_from_node_id"),
        CanonFact.valid_to_node_id.label("valid_to_node_id"),
        CanonFact.status.label("status"),
        literal(None).label("relation_type"),
        literal(None).label("description"),
        CanonFact.revision.label("revision"),
        CanonFact.is_pinned.label("is_pinned"),
        CanonFact.deleted_at.label("deleted_at"),
        CanonFact.purge_after.label("purge_after"),
        CanonFact.created_at.label("created_at"),
        CanonFact.updated_at.label("updated_at"),
    ).where(CanonFact.subject_entity_id == entity_id, CanonFact.deleted_at.is_(None))
    relation = select(
        literal("relation").label("type"),
        EntityRelation.id.label("id"),
        EntityRelation.project_id.label("project_id"),
        literal(None).label("subject_entity_id"),
        EntityRelation.source_entity_id.label("source_entity_id"),
        EntityRelation.target_entity_id.label("target_entity_id"),
        literal(None).label("predicate"),
        literal(None).label("value"),
        literal(None).label("source_note"),
        literal(None).label("source_version_id"),
        literal(None).label("valid_from_node_id"),
        literal(None).label("valid_to_node_id"),
        literal(None).label("status"),
        EntityRelation.relation_type.label("relation_type"),
        EntityRelation.description.label("description"),
        EntityRelation.revision.label("revision"),
        EntityRelation.is_pinned.label("is_pinned"),
        EntityRelation.deleted_at.label("deleted_at"),
        EntityRelation.purge_after.label("purge_after"),
        EntityRelation.created_at.label("created_at"),
        EntityRelation.updated_at.label("updated_at"),
    ).where(
        (
            (EntityRelation.source_entity_id == entity_id)
            | (EntityRelation.target_entity_id == entity_id)
        ),
        EntityRelation.deleted_at.is_(None),
    )
    records = []
    for row in session.execute(union_all(canon, relation)).mappings():
        common = {
            name: row[name]
            for name in (
                "id",
                "project_id",
                "revision",
                "is_pinned",
                "deleted_at",
                "purge_after",
                "created_at",
                "updated_at",
            )
        }
        records.append(
            CanonFact(
                **common,
                subject_entity_id=row["subject_entity_id"],
                predicate=row["predicate"],
                value=row["value"],
                source_note=row["source_note"],
                source_version_id=row["source_version_id"],
                valid_from_node_id=row["valid_from_node_id"],
                valid_to_node_id=row["valid_to_node_id"],
                status=row["status"],
            )
            if row["type"] == "canon"
            else EntityRelation(
                **common,
                source_entity_id=row["source_entity_id"],
                target_entity_id=row["target_entity_id"],
                relation_type=row["relation_type"],
                description=row["description"],
            )
        )
    entity_ids = {
        entity_id
        for record in records
        for entity_id in (
            getattr(record, "subject_entity_id", None),
            getattr(record, "source_entity_id", None),
            getattr(record, "target_entity_id", None),
        )
        if entity_id
    }
    entity_activity = {
        entity_id: project_id if deleted_at is None else None
        for entity_id, project_id, deleted_at in session.execute(
            select(Entity.id, Entity.project_id, Entity.deleted_at)
            .where(Entity.id.in_(entity_ids))
            .execution_options(include_deleted=True)
        )
    }
    pre_active = {
        ("canon" if isinstance(record, CanonFact) else "relation", record.id): record_eligible(
            session, record, entity_activity=entity_activity
        )
        for record in records
    }
    return {"records": records, "entity_activity": entity_activity, "pre_active": pre_active}


def sync_entity_dependents(
    session, captured, *, entity_id, entity_project_id, entity_active
):
    """Resync one captured dependency graph and report real activity transitions."""
    records = captured["records"]
    entity_activity = dict(captured["entity_activity"])
    entity_activity[entity_id] = entity_project_id if entity_active else None
    result = []
    for record in records:
        kind = "canon" if isinstance(record, CanonFact) else "relation"
        post_active = record_eligible(session, record, entity_activity=entity_activity)
        sync_record(session, record, dependency_eligible=post_active)
        result.append(
            {
                "type": kind,
                "id": record.id,
                "pre_active": captured["pre_active"][(kind, record.id)],
                "post_active": post_active,
            }
        )
    return sorted(result, key=lambda item: (item["type"], item["id"]))


def insert_projection(session, item):
    key = f"{item['type']}:{item['id']}"
    body = source_text(item)
    record = item["record"]
    linked = [
        value
        for name, value in record.items()
        if (name.endswith("_node_id") or name == "chapter_id") and value
    ]
    projected_item = item
    if item["type"] == "source_document":
        projected_item = {
            **item,
            "content": "",
            "preview": "",
            "record": {
                key: value for key, value in item["record"].items() if key != "content"
            },
        }
    session.execute(
        text("""INSERT INTO search_documents
        (key,source_type,source_id,title,body,data,source_hash,chapter_id,
         valid_from_node_id,valid_to_node_id,is_pinned,status,linked_nodes)
        VALUES (:key,:kind,:id,:title,:body,:data,:source_hash,:chapter_id,
                :valid_from,:valid_to,:pinned,:status,:linked)"""),
        {
            "key": key,
            "kind": item["type"],
            "id": item["id"],
            "title": item["title"],
            "body": body,
            "data": json.dumps(projected_item, ensure_ascii=False),
            "source_hash": source_hash(item),
            "chapter_id": record.get("chapter_id"),
            "valid_from": record.get("valid_from_node_id"),
            "valid_to": record.get("valid_to_node_id"),
            "pinned": item["is_pinned"],
            "status": record.get("status", ""),
            "linked": json.dumps(linked),
        },
    )
    chunks = list(project_chunks(item, body))
    session.execute(
        text("""INSERT INTO search_chunks
        (chunk_key,document_key,strategy,ordinal,start_offset,end_offset,source_hash,chunk_hash,body)
        VALUES (:chunk_key,:document_key,:strategy,:ordinal,:start_offset,:end_offset,
                :source_hash,:chunk_hash,:body)"""),
        chunks,
    )
    session.execute(
        text("INSERT INTO search_chunk_fts(chunk_key,tokens) VALUES (:key,:tokens)"),
        [
            {
                "key": chunk["chunk_key"],
                "tokens": " ".join(tokenize(item["title"] + "\n" + chunk["body"])),
            }
            for chunk in chunks
        ],
    )
    # Keep the historical document-level projection for compatibility with existing tooling.
    session.execute(
        text("INSERT INTO search_fts(key,tokens) VALUES (:key,:tokens)"),
        {"key": key, "tokens": " ".join(tokenize(item["title"] + "\n" + body))},
    )


def sync_record(
    session,
    record,
    *,
    removed=False,
    dependency_eligible=None,
    cascade=True,
):
    from novel_harness.services.library import SOURCES, present

    match = next(
        (kind for kind, (model, _, _) in SOURCES.items() if isinstance(record, model)), None
    )
    if match is None:
        return
    if cascade and isinstance(record, StoryNode) and record.kind == "chapter":
        sync_manuscript(session, record.id)
        session.execute(
            update(ChapterSummary)
            .where(ChapterSummary.chapter_id == record.id, ChapterSummary.title != record.title)
            .values(title=record.title)
        )
        for summary in session.scalars(
            select(ChapterSummary).where(ChapterSummary.chapter_id == record.id)
        ):
            sync_record(session, summary)
        from novel_harness.services.projections import queue_ledger

        queue_ledger(session)
    key = f"{match}:{record.id}"
    remove_projection(session, key)
    if removed or not record_eligible(session, record, include_dependencies=False):
        return
    if dependency_eligible is None:
        dependency_eligible = dependencies_active(session, record)
    if not dependency_eligible:
        return
    item = present(match, record)
    insert_projection(session, item)


def update_source_document_pin_projection(session, record):
    """Update mutable pin metadata without touching immutable text projections."""
    from fastapi import HTTPException
    from fastapi.encoders import jsonable_encoder

    key = f"source_document:{record.id}"
    row = session.execute(
        text("SELECT data FROM search_documents WHERE key=:key"), {"key": key}
    ).mappings().first()
    try:
        data = json.loads(row["data"] if row else "")
        projected_record = data["record"]
        if not isinstance(data, dict) or not isinstance(projected_record, dict):
            raise ValueError
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise HTTPException(
            409,
            detail={
                "code": "RAG_REBUILD_REQUIRED",
                "message": "检索派生索引需要重建；未在缺少参考时生成内容。",
            },
        ) from None
    data["revision"] = record.revision
    data["is_pinned"] = bool(record.is_pinned)
    projected_record["revision"] = record.revision
    projected_record["is_pinned"] = bool(record.is_pinned)
    projected_record["updated_at"] = jsonable_encoder(record.updated_at)
    result = session.execute(
        text(
            "UPDATE search_documents SET is_pinned=:is_pinned,data=:data "
            "WHERE key=:key"
        ),
        {
            "key": key,
            "is_pinned": bool(record.is_pinned),
            "data": json.dumps(data, ensure_ascii=False),
        },
    )
    if result.rowcount != 1:
        raise HTTPException(409, detail={"code": "RAG_REBUILD_REQUIRED"})


def rebuild_index(session):
    from novel_harness.services.library import SOURCES
    from novel_harness.services.projections import queue_ledger

    entity_activity = dict(
        session.execute(
            select(Entity.id, Entity.project_id)
            .where(Entity.deleted_at.is_(None))
            .execution_options(include_deleted=True)
        ).all()
    )
    session.execute(text("DELETE FROM search_fts"))
    session.execute(text("DELETE FROM search_documents"))
    session.execute(text("DELETE FROM search_chunk_fts"))
    session.execute(text("DELETE FROM search_chunks"))
    saw_chapter = False
    for model, _, _ in SOURCES.values():
        for item in session.scalars(select(model)):
            saw_chapter = saw_chapter or (
                isinstance(item, StoryNode) and item.kind == "chapter"
            )
            sync_record(
                session,
                item,
                dependency_eligible=dependencies_active(
                    session, item, entity_activity=entity_activity
                ),
                cascade=False,
            )
    for chapter_id in session.scalars(select(ChapterDocument.chapter_id)):
        sync_manuscript(session, chapter_id)
    if saw_chapter:
        queue_ledger(session)
    session.execute(
        text("UPDATE search_index_state SET version=:version WHERE id=1"),
        {"version": SEARCH_INDEX_VERSION},
    )


def require_ready_index(session):
    from fastapi import HTTPException

    if (
        session.scalar(text("SELECT version FROM search_index_state WHERE id=1"))
        != SEARCH_INDEX_VERSION
    ):
        raise HTTPException(
            409,
            detail={
                "code": "RAG_REBUILD_REQUIRED",
                "message": "检索派生索引需要重建；未在缺少参考时生成内容。",
            },
        )


def sync_manuscript(session, chapter_id):
    key = f"manuscript:{chapter_id}"
    remove_projection(session, key)
    row = (
        session.execute(
            text(
                "SELECT n.title,v.content,v.id AS version_id,d.revision "
                "FROM chapter_documents d JOIN story_nodes n ON n.id=d.chapter_id "
                "JOIN chapter_versions v ON v.id=d.current_version_id "
                "WHERE d.chapter_id=:id AND n.deleted_at IS NULL"
            ),
            {"id": chapter_id},
        )
        .mappings()
        .first()
    )
    if not row:
        return
    item = {
        "type": "manuscript",
        "id": chapter_id,
        "title": row["title"] + " · 正文版本",
        "content": row["content"],
        "preview": row["content"][:160],
        "revision": row["revision"],
        "is_pinned": False,
        "deleted_at": None,
        "purge_after": None,
        "record": {"chapter_id": chapter_id, "version_id": row["version_id"]},
    }
    insert_projection(session, item)


@event.listens_for(Session, "do_orm_execute")
def hide_deleted(state):
    if state.is_select and not state.execution_options.get("include_deleted"):
        state.statement = state.statement.options(
            with_loader_criteria(
                LibraryRecord, lambda cls: cls.deleted_at.is_(None), include_aliases=True
            )
        )


@event.listens_for(Session, "after_flush")
def project_changes(session, _context):
    if not session.info.get("index_ready"):
        return
    for item in session.new.union(session.dirty):
        sync_record(session, item)
        if isinstance(item, (ChapterDocument, ChapterVersion)):
            sync_manuscript(session, item.chapter_id)
    for item in session.deleted:
        sync_record(session, item, removed=True)
