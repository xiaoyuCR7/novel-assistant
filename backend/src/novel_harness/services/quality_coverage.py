"""Read-only review coverage from latest chapter reports and frozen dependencies."""

from collections import Counter, defaultdict
from copy import deepcopy

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select, true

from novel_harness.db.job_models import AIJobControl
from novel_harness.db.models import AIJob, ChapterDocument, ChapterVersion, StoryNode
from novel_harness.services.entity_states import assert_project_entity_states_resolved
from novel_harness.services.job_context import assert_source, capture_summary_source
from novel_harness.services.projects import require_project
from novel_harness.services.quality import _assert_acceptance_context, capture_quality_context

STATES = ("unchecked", "stale", "pending", "confirmed")
# SQLite's default trim removes only ASCII spaces. Match Python str.strip's Unicode
# whitespace while keeping the manuscript itself out of the navigation result.
_WHITESPACE = (
    "\t\n\v\f\r\x1c\x1d\x1e\x1f \x85\xa0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000"
)


def _assert_chapter_sources(session, project_id, snapshot, effects, index, verified):
    accepted = effects.get("accepted_chapters", {})
    # Earlier handoffs influence the current chapter. Later chapters do not.
    for item in snapshot["chapters"][: index + 1]:
        chapter_id = item["chapter_id"]
        if chapter_id in verified:
            continue
        document = session.get(ChapterDocument, chapter_id)
        if document is None:
            raise ValueError("Missing source document")
        source = item["source"]
        if chapter_id in accepted:
            version = session.get(ChapterVersion, accepted[chapter_id])
            if (
                version is None
                or version.project_id != project_id
                or version.chapter_id != chapter_id
                or document.current_version_id != version.id
                or document.content != version.content
            ):
                raise ValueError("Accepted manuscript changed")
            source = {
                **source,
                "content": version.content,
                "version_id": version.id,
                "revision": document.revision,
            }
        assert_source(session, source)
        assert_project_entity_states_resolved(session, project_id, chapter_id)
        if chapter_id in accepted:
            # Accepted prose may no longer contain reference markers. Its frozen
            # manifest still validates those original references in assert_source.
            current = jsonable_encoder(
                capture_summary_source(
                    session,
                    project_id,
                    chapter_id,
                )["content_check_context"]
            )
            explicit = deepcopy(item["context"].get("explicit_sources", []))
            current["explicit_sources"] = explicit
            current["allowed_reference_ids"] = sorted(
                {
                    *current["allowed_reference_ids"],
                    *(ref["id"] for ref in explicit),
                }
            )
        else:
            current = capture_quality_context(
                session,
                project_id,
                chapter_id,
                snapshot.get("instructions", ""),
            )
        _assert_acceptance_context(session, project_id, item, current, accepted)
        verified.add(chapter_id)


def _ordered_chapters(session, project_id):
    # Avoid loading StoryNode.summary and every saved manuscript for navigation.
    nodes = {
        row.id: row
        for row in session.execute(
            select(
                StoryNode.id,
                StoryNode.parent_id,
                StoryNode.order_index,
                StoryNode.created_at,
                StoryNode.kind,
                StoryNode.title,
                func.length(ChapterDocument.content).label("content_length"),
                (func.length(func.trim(ChapterDocument.content, _WHITESPACE)) > 0).label(
                    "has_content"
                ),
            )
            .outerjoin(ChapterDocument, ChapterDocument.chapter_id == StoryNode.id)
            .where(
                StoryNode.project_id == project_id,
                StoryNode.deleted_at.is_(None),
            )
        )
    }

    def order(node):
        chain, seen = [], set()
        while node and node.id not in seen:
            seen.add(node.id)
            chain.append((node.order_index, node.created_at, node.id))
            node = nodes.get(node.parent_id)
        return tuple(reversed(chain))

    return [node for node in sorted(nodes.values(), key=order) if node.kind == "chapter"]


def read_coverage(session, project_id, status="all", limit=30, offset=0):
    require_project(session, project_id)
    nodes = _ordered_chapters(session, project_id)
    items = {
        node.id: {
            "chapter_id": node.id,
            "title": node.title,
            "status": "unchecked",
            "reason": "尚无质量检测记录。",
            "job_id": None,
            "checked_at": None,
            "can_review": bool(node.has_content) and 0 < (node.content_length or 0) <= 50000,
        }
        for node in nodes
    }
    chapters = func.json_each(AIJobControl.source_snapshot, "$.chapters").table_valued(
        "key", "value"
    )
    chapter_id = func.json_extract(chapters.c.value, "$.chapter_id")
    ranked = (
        select(
            chapter_id.label("chapter_id"),
            AIJob.id.label("job_id"),
            chapters.c.key.label("index"),
            AIJob.status,
            AIJob.updated_at,
            (
                func.json_type(AIJob.result, func.printf("$.chapters[%d].before", chapters.c.key))
                == "object"
            ).label("has_review"),
            func.row_number()
            .over(partition_by=chapter_id, order_by=(AIJob.created_at.desc(), AIJob.id.desc()))
            .label("position"),
        )
        .select_from(AIJob)
        .join(AIJobControl, AIJobControl.job_id == AIJob.id)
        .join(
            chapters,
            true(),
        )
        .where(AIJob.project_id == project_id, AIJob.task_type == "quality_workflow")
        .subquery()
    )
    by_job = defaultdict(list)
    for row in session.execute(select(ranked).where(ranked.c.position == 1)).mappings():
        if row["chapter_id"] in items:
            by_job[row["job_id"]].append(row)
    for job_id, rows in by_job.items():
        # One latest source packet per run, not all historical requests/results.
        snapshot, effects = session.execute(
            select(
                AIJobControl.source_snapshot,
                AIJobControl.effects,
            ).where(AIJobControl.job_id == job_id)
        ).one()
        verified = set()
        for row in rows:
            item = items[row["chapter_id"]]
            item.update(
                job_id=job_id,
                status="pending",
                reason="任务尚未完成质量检测，请查看原任务。",
                checked_at=row["updated_at"].isoformat() if row["has_review"] else None,
            )
            try:
                _assert_chapter_sources(
                    session,
                    project_id,
                    snapshot,
                    effects,
                    int(row["index"]),
                    verified,
                )
            except (HTTPException, ValueError, KeyError, TypeError):
                item.update(
                    status="stale", reason="正文、目录或已知参考来源已变化，请按当前资料复核。"
                )
                continue
            if not row["has_review"]:
                continue
            if row["chapter_id"] in effects.get("accepted_selections", {}):
                item["reason"] = "已局部采纳；组合稿尚未整体复核，不能沿用原候选结论。"
            elif row["chapter_id"] in effects.get("accepted_chapters", {}):
                item.update(status="confirmed", reason="作者已完整采纳，当前正文与已知来源仍匹配。")
            else:
                item["reason"] = "已有质量报告，尚未完整采纳；模型评分通过不等于作者确认。"
    ordered = list(items.values())
    counts = {state: 0 for state in STATES} | dict(Counter(item["status"] for item in ordered))
    filtered = (
        ordered if status == "all" else [item for item in ordered if item["status"] == status]
    )
    return {
        "items": filtered[offset : offset + limit],
        "total": len(filtered),
        "chapter_count": len(ordered),
        "counts": counts,
        "next_offset": offset + limit if offset + limit < len(filtered) else None,
    }
