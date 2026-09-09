"""Bounded Wiki views over authoritative project records; never a second canon store."""

import json

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, or_, select

from novel_harness.db.models import AIJob, Entity, EntityState
from novel_harness.services.entity_aliases import aliases, register_name_matcher
from novel_harness.services.entity_aliases import expand_aliases as expand_aliases
from novel_harness.services.entity_states import resolve_entity_state
from novel_harness.services.job_state import command_hash
from novel_harness.services.retrieval import eligibility, execute_candidates, ordered_nodes
from novel_harness.services.search_index import require_ready_index

KINDS = {"entity", "plot", "timeline"}
MAX_SOURCES = 24
MAX_CONTENT = 12000
SEARCH_TEXT = (
    "(d.title || ' ' || CASE d.source_type WHEN 'entity' THEN "
    "coalesce(json_extract(d.data,'$.record.summary'),'') || ' ' || "
    "coalesce(json_extract(d.data,'$.record.profile'),'') "
    "WHEN 'plot' THEN coalesce(json_extract(d.data,'$.record.promise'),'') ELSE d.body END)"
)


def scope_filter(session, chapter_id):
    where, params = eligibility(session, chapter_id, False, None)
    nodes = ordered_nodes(session)
    positions = {node.id: i for i, node in enumerate(nodes)}
    if chapter_id:
        # Wiki is an author-facing view through this chapter, not a pre-writing prompt.
        where = where.replace("d.chapter_id IN :prior", "d.chapter_id IN :before")
        params.pop("prior", None)
        # Timelines and plot starts have dates beyond the ordinary RAG filters.
        where += (
            " AND (d.source_type!='timeline' OR "
            "(d.chapter_id IS NOT NULL AND d.chapter_id IN :before))"
            " AND (d.source_type!='node' OR d.source_id IN :before)"
            " AND (d.source_type!='plot' OR "
            "json_extract(d.data,'$.record.start_node_id') IN :before)"
        )
    return where, params, nodes, positions


def index(session, q="", kind="all", chapter_id=None, offset=0, limit=30):
    require_ready_index(session)
    where, params, _, _ = scope_filter(session, chapter_id)
    where += " AND d.source_type IN ('entity','plot','timeline')"
    if kind != "all":
        where += " AND d.source_type=:kind"
        params["kind"] = kind
    if q.strip():
        where += " AND instr(lower(" + SEARCH_TEXT + "),lower(:q))>0"
        params["q"] = q.strip()
    total = execute_candidates(
        session, "SELECT count(*) FROM search_documents d WHERE " + where, params
    ).scalar()
    rows = execute_candidates(
        session,
        "SELECT d.source_id AS id,d.source_type AS type,d.title,substr(coalesce("
        "json_extract(d.data,'$.record.summary'),json_extract(d.data,'$.record.promise'),"
        "json_extract(d.data,'$.record.description'),''),1,160) AS preview,"
        "json_extract(d.data,'$.record.profile') AS profile FROM search_documents d WHERE "
        + where
        + " ORDER BY d.title,d.key LIMIT :limit OFFSET :offset",
        {**params, "limit": limit, "offset": offset},
    ).mappings()
    return {
        "items": [
            {
                **{k: r[k] for k in ("id", "type", "title", "preview")},
                "aliases": aliases(json.loads(r["profile"] or "{}")),
            }
            for r in rows
        ],
        "total": total,
        "has_more": offset + limit < total,
    }


def page_identity(kind, item_id, chapter_id):
    return json.dumps([kind, item_id, chapter_id], separators=(",", ":"))


def snapshot(session, project_id, kind, item_id, chapter_id=None):
    if kind not in KINDS:
        raise HTTPException(404, detail={"code": "WIKI_PAGE_NOT_FOUND"})
    require_ready_index(session)
    where, params, nodes, positions = scope_filter(session, chapter_id)
    query = "SELECT d.* FROM search_documents d WHERE " + where
    root = (
        execute_candidates(
            session, query + " AND d.key=:root", {**params, "root": f"{kind}:{item_id}"}
        )
        .mappings()
        .first()
    )
    if not root:
        raise HTTPException(404, detail={"code": "WIKI_PAGE_NOT_FOUND"})
    record = json.loads(root["data"])["record"]
    if record["project_id"] != project_id:
        raise HTTPException(404, detail={"code": "WIKI_PAGE_NOT_FOUND"})
    names = [root["title"], *aliases(record.get("profile"))]
    register_name_matcher(session)
    # Explicit relations/facts and exact mentions stay distinguishable in the UI.
    direct = (
        "(json_extract(d.data,'$.record.subject_entity_id')=:entity OR "
        "json_extract(d.data,'$.record.source_entity_id')=:entity OR "
        "json_extract(d.data,'$.record.target_entity_id')=:entity)"
    )
    mentions = " OR ".join(
        f"(novel_name_matches(d.title,:name{i}) OR novel_name_matches({SEARCH_TEXT},:name{i}))"
        for i in range(len(names))
    )
    linked = [record[k] for k in ("start_node_id", "due_node_id", "chapter_id") if record.get(k)]
    related = (
        execute_candidates(
            session,
            query
            + " AND d.key!=:root AND ("
            + (direct + " OR " if kind == "entity" else "")
            + "(d.source_type IN ('summary','node','timeline','plot') AND ("
            + mentions
            + "))"
            + " OR (d.source_type='node' AND d.source_id IN :linked))"
            + " ORDER BY d.source_type,d.title,d.key LIMIT :cap",
            {
                **params,
                "root": f"{kind}:{item_id}",
                "entity": item_id,
                "linked": linked,
                **{f"name{i}": name for i, name in enumerate(names)},
                "cap": MAX_SOURCES,
            },
        )
        .mappings()
        .all()
    )
    truncated = len(related) >= MAX_SOURCES
    history = []
    state = None
    conflicts = []
    if kind == "entity":
        entity = session.get(Entity, item_id)
        if chapter_id:
            resolved = resolve_entity_state(session, entity, chapter_id)
            state, conflicts = resolved.data, resolved.conflicts
        position_map = json.dumps(positions)
        start_position = func.json_extract(
            position_map, '$."' + EntityState.valid_from_node_id + '"'
        )
        end_position = func.json_extract(
            position_map, '$."' + EntityState.valid_to_node_id + '"'
        )
        rows = session.scalars(
            select(EntityState)
            .where(
                EntityState.entity_id == item_id,
                EntityState.project_id == project_id,
                EntityState.status == "confirmed",
                or_(
                    EntityState.valid_to_node_id.is_(None),
                    # Missing/deleted endpoints yield NULL and are also excluded.
                    end_position >= start_position,
                ),
                EntityState.valid_from_node_id.in_(
                    [
                        n.id
                        for n in nodes
                        if not chapter_id or positions[n.id] <= positions[chapter_id]
                    ]
                ),
            )
            .order_by(
                start_position.desc(),
                EntityState.created_at.desc(),
                EntityState.id.desc(),
            )
            .limit(101)
        ).all()
        truncated |= len(rows) > 100
        titles = {n.id: n.title for n in nodes}
        history = [
            {
                "id": r.id,
                "chapter_id": r.valid_from_node_id,
                "chapter_title": titles[r.valid_from_node_id],
                "data": r.data,
                "revision": r.revision,
                "source_version_id": r.source_version_id,
                "valid_to_node_id": r.valid_to_node_id,
            }
            for r in rows[:100]
        ]
        history.sort(key=lambda r: (positions[r["chapter_id"]], r["id"]))
    sources, budget, links = [], MAX_CONTENT, []
    for row in [root, *related[: MAX_SOURCES - 1]]:
        data = json.loads(row["data"])["record"]
        content = row["body"]
        if row["source_type"] == "entity" and chapter_id:
            # Use resolved state rather than the latest legacy state projection.
            data = {**data, "state": state}
            content = json.dumps(
                {
                    "name": data["name"],
                    "summary": data["summary"],
                    "profile": data["profile"],
                    "state": state,
                },
                ensure_ascii=False,
            )
        if row["key"] == root["key"] and history:
            content += "\n已确认状态历史：" + json.dumps(history, ensure_ascii=False)
            data = {**data, "wiki_state_history": history}
        if row["source_type"] == "plot" and chapter_id:
            # A planned payoff/status can disclose events beyond this chapter.
            data = {k: v for k, v in data.items() if k not in ("payoff", "status", "due_node_id")}
            content = json.dumps(
                {
                    "title": data["title"],
                    "promise": data["promise"],
                    "start_node_id": data.get("start_node_id"),
                },
                ensure_ascii=False,
            )
        clipped = content[: min(2000, budget)]
        truncated |= len(clipped) < len(content)
        if not clipped and row["key"] != root["key"]:
            continue
        budget -= len(clipped)
        sources.append(
            {
                "id": row["source_id"],
                "type": row["source_type"],
                "title": row["title"],
                "content": clipped,
                "revision": data["revision"],
                "source_hash": command_hash(data),
                "reason": "主题资料"
                if row["key"] == root["key"]
                else "关联记录"
                if row["source_type"] in ("canon", "relation")
                else "提及或章节关联",
            }
        )
        if row["source_type"] in KINDS and row["key"] != root["key"]:
            links.append(
                {"id": row["source_id"], "type": row["source_type"], "title": row["title"]}
            )
    if kind == "entity":
        related_ids = set()
        for row in related:
            if row["source_type"] == "relation":
                relation = json.loads(row["data"])["record"]
                related_ids.update([relation["source_entity_id"], relation["target_entity_id"]])
        related_ids.discard(item_id)
        for entity in session.scalars(
            select(Entity)
            .where(
                Entity.project_id == project_id,
                Entity.id.in_(related_ids),
                Entity.deleted_at.is_(None),
            )
            .order_by(Entity.name, Entity.id)
        ):
            links.append({"id": entity.id, "type": "entity", "title": entity.name})
    result = {
        "id": item_id,
        "type": kind,
        "title": root["title"],
        "project_id": project_id,
        "aliases": aliases(record.get("profile")),
        "scope_chapter_id": chapter_id,
        "sources": sources,
        "links": links,
        "state_history": history,
        "state_conflicts": conflicts,
        "truncated": truncated,
    }
    result = jsonable_encoder(result)
    # Fingerprint includes history and ordering, so edits invalidate cached synthesis.
    result["fingerprint"] = command_hash(
        {
            **result,
            "chapter_order": [
                n.id for n in nodes if not chapter_id or positions[n.id] <= positions[chapter_id]
            ],
        }
    )
    return result


def latest_jobs(session, project_id, kind, item_id, chapter_id, fingerprint=None):
    scope = [
        AIJob.project_id == project_id,
        AIJob.task_type == "wiki_summary",
        AIJob.instructions == page_identity(kind, item_id, chapter_id),
    ]
    latest = session.scalar(
        select(AIJob).where(*scope).order_by(AIJob.created_at.desc(), AIJob.id.desc()).limit(1)
    )
    summaries = select(AIJob).where(*scope, AIJob.status == "succeeded")
    if fingerprint:
        # A restored source packet can match an older or resumed job. Prefer
        # that valid cache before falling back to a stale summary for display.
        summaries = summaries.order_by(
            (AIJob.result["fingerprint"].as_string() == fingerprint).desc()
        )
    summary = session.scalar(summaries.order_by(AIJob.created_at.desc(), AIJob.id.desc()).limit(1))
    return latest, summary


def active_job(session, project_id, instructions, exclude_id=None):
    from novel_harness.services.job_state import ACTIVE

    query = select(AIJob).where(
        AIJob.project_id == project_id,
        AIJob.task_type == "wiki_summary",
        AIJob.instructions == instructions,
        AIJob.status.in_(ACTIVE | {"recovery_required"}),
    )
    if exclude_id:
        query = query.where(AIJob.id != exclude_id)
    return session.scalar(query.order_by(AIJob.created_at, AIJob.id).limit(1))


def detail(session, store, project_id, kind, item_id, chapter_id=None):
    result = snapshot(session, project_id, kind, item_id, chapter_id)
    job, summary = latest_jobs(
        session, project_id, kind, item_id, chapter_id, result["fingerprint"]
    )
    job = active_job(session, project_id, page_identity(kind, item_id, chapter_id)) or job
    result["job"] = store.serialize_in_session(session, job.id) if job else None
    result["summary"] = (
        {
            "job_id": summary.id,
            "created_at": summary.created_at,
            "claims": summary.result.get("claims", []),
            "stale": summary.result.get("fingerprint") != result["fingerprint"],
        }
        if summary
        else None
    )
    return result
