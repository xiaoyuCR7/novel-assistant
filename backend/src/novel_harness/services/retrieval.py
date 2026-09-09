"""Eligible chunk candidates first; hydrate only the selected source records."""

import json
import sqlite3
from typing import Protocol

from fastapi import HTTPException
from sqlalchemy import bindparam, select, text

from novel_harness.ai.base import ProviderError
from novel_harness.db.models import StoryNode
from novel_harness.services.search_index import dependency_eligibility_filters, tokenize

CANDIDATE_LIMIT = 100
STRUCTURE_LIMIT = 30
VECTOR_STATES = frozenset({"ready", "needs_rebuild", "disabled", "degraded"})
VECTOR_FAILURES = (ProviderError, sqlite3.Error, OSError, ValueError)


class VectorRetriever(Protocol):
    def search(self, query: str, limit: int) -> list[tuple[str, float]]: ...


def vector_failure_state(exc, *, durable=False):
    if durable and (
        not isinstance(exc, ProviderError) or getattr(exc, "outcome", "unknown") != "known"
    ):
        raise exc
    return "degraded"


def vector_status(vectors, *, durable=False):
    if vectors is None:
        return "disabled"
    try:
        state = vectors.status() if hasattr(vectors, "status") else "ready"
    except VECTOR_FAILURES as exc:
        return vector_failure_state(exc, durable=durable)
    return state if state in VECTOR_STATES else "degraded"


def select_query_tokens(query, limit=80):
    tokens = tokenize(query)
    if len(tokens) <= limit:
        return tokens
    head = limit // 2
    return tokens[:head] + tokens[-(limit - head) :]


def ordered_nodes(session):
    nodes = {node.id: node for node in session.scalars(select(StoryNode))}

    def order(node):
        chain = []
        visited = set()
        while node and node.id not in visited:
            visited.add(node.id)
            chain.append((node.order_index, node.created_at, node.id))
            node = nodes.get(node.parent_id)
        return tuple(reversed(chain))

    return sorted(nodes.values(), key=order)


def ordered_chapters(session):
    return [node for node in ordered_nodes(session) if node.kind == "chapter"]


def fact_applies(record, chapter_id, positions):
    if not chapter_id:
        return True
    current = positions[chapter_id]
    start, end = record.get("valid_from_node_id"), record.get("valid_to_node_id")
    return (not start or (start in positions and positions[start] <= current)) and (
        not end or (end in positions and positions[end] >= current)
    )


def eligibility(session, chapter_id, include_manuscripts, exclude_ids, category="all"):
    from novel_harness.services.library_pages import validate_category

    validate_category(category)
    filters, parameters = [], {}
    if category != "all":
        filters.append("d.source_type = :category")
        parameters["category"] = category
    if not include_manuscripts:
        filters.append("d.source_type != 'manuscript'")
    if exclude_ids:
        filters.append("d.source_id NOT IN :excluded")
        parameters["excluded"] = list(exclude_ids)
    filters.append(
        "(d.source_type NOT IN ('summary','manuscript') OR EXISTS "
        "(SELECT 1 FROM story_nodes n WHERE n.id=d.chapter_id AND n.deleted_at IS NULL))"
    )
    filters.append("(d.source_type NOT IN ('canon','style_rule') OR d.status='confirmed')")
    # Derived rows are only candidates while their authoritative dependency
    # graph is active. This prevents stale FTS/vector projections from
    # resurrecting facts or relations whose entities were soft-deleted.
    filters.extend(dependency_eligibility_filters())
    if chapter_id:
        from novel_harness.services.versions import require_chapter

        require_chapter(session, chapter_id)
        nodes = ordered_nodes(session)
        positions = {node.id: i for i, node in enumerate(nodes)}
        current = positions[chapter_id]
        parameters["prior"] = [node.id for node in nodes[:current] if node.kind == "chapter"]
        parameters["before"] = [node.id for node in nodes[: current + 1]]
        parameters["after"] = [node.id for node in nodes[current:]]
        filters.extend(
            [
                "(d.source_type NOT IN ('summary','manuscript') OR d.chapter_id IN :prior)",
                "(d.source_type != 'canon' OR ((d.valid_from_node_id IS NULL OR "
                "d.valid_from_node_id IN :before) AND (d.valid_to_node_id IS NULL OR "
                "d.valid_to_node_id IN :after)))",
            ]
        )
    return " AND ".join(filters) or "1=1", parameters


def execute_candidates(session, sql, parameters):
    statement = text(sql)
    for name, value in parameters.items():
        if isinstance(value, (list, set, tuple)):
            statement = statement.bindparams(bindparam(name, expanding=True))
    return session.execute(statement, parameters)


def search(
    session,
    query="",
    chapter_id=None,
    limit=30,
    vectors: VectorRetriever | None = None,
    include_manuscripts=False,
    exclude_ids=None,
    lightweight=False,
    category="all",
):
    if vectors is None:
        vectors = session.info.get("vectors")
    durable_retrieval = bool(session.info.get("durable_retrieval"))
    vector_state = vector_status(vectors, durable=durable_retrieval)
    where, parameters = eligibility(session, chapter_id, include_manuscripts, exclude_ids, category)
    join = " FROM search_chunks c JOIN search_documents d ON d.key=c.document_key "
    channels = {}
    from novel_harness.services.entity_aliases import expand_aliases
    tokens = select_query_tokens(expand_aliases(session, query))
    if tokens:
        match = " OR ".join('"' + word + '"' for word in tokens)
        rows = execute_candidates(
            session,
            "WITH matches AS MATERIALIZED (SELECT c.chunk_key,c.document_key,"
            "bm25(search_chunk_fts) AS rank FROM search_chunk_fts "
            "JOIN search_chunks c ON c.chunk_key=search_chunk_fts.chunk_key "
            "JOIN search_documents d ON d.key=c.document_key "
            "WHERE search_chunk_fts MATCH :q AND "
            + where
            + "), ranked AS (SELECT chunk_key,rank,ROW_NUMBER() OVER "
            "(PARTITION BY document_key ORDER BY rank,chunk_key) AS source_rank FROM matches) "
            "SELECT chunk_key,rank FROM ranked WHERE source_rank=1 "
            "ORDER BY rank,chunk_key LIMIT :cap",
            {**parameters, "q": match, "cap": CANDIDATE_LIMIT},
        )
        channels["fts"] = [row[0] for row in rows]
    elif not chapter_id:
        channels["browse"] = list(
            execute_candidates(
                session,
                "SELECT c.chunk_key"
                + join
                + "WHERE "
                + where
                + " AND d.source_type NOT IN ('canon','style_rule')"
                + " AND c.ordinal=0 ORDER BY d.is_pinned DESC,d.title,d.key LIMIT :cap",
                {**parameters, "cap": CANDIDATE_LIMIT},
            ).scalars()
        )
    if chapter_id:
        conditions = {
            "structure": "(d.source_type NOT IN ('canon','style_rule') AND "
            "(EXISTS (SELECT 1 FROM json_each(d.linked_nodes) WHERE value=:chapter) "
            "OR (d.source_type='plot' AND d.status='active') "
            "OR d.source_type IN ('entity','relation')))",
            "pinned": "d.is_pinned=1",
        }
        for channel, condition in conditions.items():
            args = {**parameters, "cap": STRUCTURE_LIMIT}
            if channel == "structure":
                args["chapter"] = chapter_id
            channels[channel] = list(
                execute_candidates(
                    session,
                    "SELECT c.chunk_key"
                    + join
                    + "WHERE "
                    + where
                    + " AND c.ordinal=0 AND "
                    + condition
                    + " ORDER BY d.is_pinned DESC,d.key LIMIT :cap",
                    args,
                ).scalars()
            )
    if vector_state == "ready" and query.strip():
        try:
            eligible = list(
                execute_candidates(
                    session, "SELECT c.chunk_key" + join + "WHERE " + where, parameters
                ).scalars()
            )
            if hasattr(vectors, "search_chunks"):
                channels["vector"] = [
                    key
                    for key, _ in vectors.search_chunks(
                        query, CANDIDATE_LIMIT, eligible_keys=set(eligible)
                    )
                ]
            else:
                # Historical document-key adapters cannot accept an eligibility filter.
                document_count = session.scalar(text("SELECT count(*) FROM search_documents"))
                raw = vectors.search(query, max(CANDIDATE_LIMIT, document_count))
                by_document = {}
                for key in eligible:
                    by_document.setdefault(key.rsplit(":", 2)[0], key)
                channels["vector"] = [by_document[key] for key, _ in raw if key in by_document][
                    :CANDIDATE_LIMIT
                ]
        except VECTOR_FAILURES as exc:
            vector_state = vector_failure_state(exc, durable=durable_retrieval)
    scores, reasons = {}, {}
    for channel, keys in channels.items():
        for rank, key in enumerate(dict.fromkeys(keys), 1):
            scores[key] = scores.get(key, 0) + 1 / (60 + rank)
            reasons.setdefault(key, []).append(channel)
    if not scores:
        return {
            "items": [],
            "total": 0,
            "mode": "hybrid",
            "vectors": vector_state,
            **({"total_kind": "bounded_candidates"} if lightweight else {}),
        }
    metadata = (
        execute_candidates(
            session,
            "SELECT c.chunk_key,c.document_key,c.source_hash,d.source_type,d.is_pinned,d.title"
            + join
            + "WHERE c.chunk_key IN :keys AND "
            + where,
            {**parameters, "keys": list(scores)},
        )
        .mappings()
        .all()
    )
    # Structural channels nominate a source's first chunk. Prefer an actual lexical
    # or semantic hit when that same source also has a relevant later fragment.
    best_chunks = {}
    for row in metadata:
        key = row["chunk_key"]
        previous = best_chunks.get(row["document_key"])
        relevance = (bool({"fts", "vector"} & set(reasons[key])), scores[key])
        if previous is None or relevance > previous[0]:
            best_chunks[row["document_key"]] = (relevance, row)
    metadata = [row for _, row in best_chunks.values()]
    metadata.sort(
        key=lambda row: (
            row["source_type"] in {"canon", "style_rule"},
            bool(row["is_pinned"]),
            query.lower() in row["title"].lower() if query else False,
            scores[row["chunk_key"]],
        ),
        reverse=True,
    )
    selected, seen = [], set()
    for row in metadata:
        if row["document_key"] not in seen:
            seen.add(row["document_key"])
            selected.append(row["chunk_key"])
    total = len(selected)
    selected = selected[: max(0, limit)]
    projection = "c.*,d.data"
    if lightweight:
        projection = (
            "c.chunk_key,c.document_key,c.source_hash,c.strategy,c.ordinal,c.start_offset,"
            "c.end_offset,c.chunk_hash,substr(c.body,1,160) AS body,d.source_type AS type,"
            "d.source_id AS id,substr(d.title,1,240) AS title,d.is_pinned,"
            "json_extract(d.data,'$.revision') AS revision,"
            "json_extract(d.data,'$.record.status') AS status,"
            "json_extract(d.data,'$.record.is_active') AS is_active,"
            "json_extract(d.data,'$.record.origin') AS origin"
        )
    rows = execute_candidates(
        session,
        "SELECT " + projection + join + "WHERE c.chunk_key IN :keys AND " + where,
        {**parameters, "keys": selected},
    ).mappings()
    hydrated = {row["chunk_key"]: row for row in rows}
    expected = {row["chunk_key"]: row["source_hash"] for row in metadata}
    if any(
        key not in hydrated or hydrated[key]["source_hash"] != expected[key] for key in selected
    ):
        raise HTTPException(
            409, detail={"code": "SOURCE_CHANGED", "message": "检索期间素材已变化，请重新检索。"}
        )
    results = []
    labels = {
        "fts": "关键词命中",
        "structure": "章节关联",
        "pinned": "作者固定",
        "browse": "项目素材",
        "vector": "语义相近",
    }
    for key in selected:
        row = hydrated[key]
        item = (
            json.loads(row["data"])
            if not lightweight
            else {
                **{
                    name: row[name]
                    for name in ("id", "type", "title", "revision", "status", "origin")
                },
                "is_pinned": bool(row["is_pinned"]),
                "deleted_at": None,
                "purge_after": None,
            }
        )
        chunk = {
            "key": key,
            **{
                name: row[name]
                for name in (
                    "strategy",
                    "ordinal",
                    "start_offset",
                    "end_offset",
                    "source_hash",
                    "chunk_hash",
                )
            },
        }
        active_style = item["type"] == "style" and bool(
            row["is_active"] if lightweight else item["record"].get("is_active")
        )
        results.append(
            {
                **item,
                **({} if lightweight else {"content": row["body"]}),
                "preview": row["body"][:160],
                "chunk": chunk,
                "score": round(scores[key], 6),
                "channels": reasons[key],
                "constraint": (
                    "hard"
                    if item["is_pinned"]
                    and item["type"] != "source_document"
                    and (item["type"] in {"canon", "style_rule"} or active_style)
                    else "soft"
                ),
                "reason": " · ".join(labels[channel] for channel in reasons[key]),
                "citation": {
                    "type": item["type"],
                    "id": item["id"],
                    "title": item["title"],
                    "source_hash": row["source_hash"],
                    "chunk": chunk,
                },
            }
        )
    return {
        "items": results,
        "total": total,
        "mode": "hybrid",
        "vectors": vector_state,
        **({"total_kind": "bounded_candidates"} if lightweight else {}),
    }
