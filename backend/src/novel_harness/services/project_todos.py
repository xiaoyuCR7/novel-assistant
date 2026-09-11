"""Read-only project-wide work queue; manuscript and model packets stay in SQLite."""

from sqlalchemy import and_, case, exists, func, literal, or_, select, true, union_all

from novel_harness.db.job_models import AIJobControl
from novel_harness.db.models import (
    AIJob,
    ChapterDocument,
    ChapterSummary,
    Conflict,
    ConversationJob,
    StoryNode,
)
from novel_harness.services.conversation_threads import TASKS, default_id, visible_job_filter
from novel_harness.services.projects import require_project


def _columns(
    identifier,
    kind,
    title,
    detail,
    destination,
    action,
    priority,
    *,
    job_id=None,
    conversation_id=None,
):
    return [
        identifier.label("id"),
        literal(kind).label("kind"),
        title.label("title"),
        detail.label("detail"),
        literal(destination).label("destination"),
        StoryNode.id.label("chapter_id"),
        StoryNode.title.label("chapter_title"),
        (job_id if job_id is not None else literal(None)).label("job_id"),
        (conversation_id if conversation_id is not None else literal(None)).label(
            "conversation_id"
        ),
        literal(action).label("action_label"),
        literal(priority).label("priority"),
    ]


def read_todos(session, project_id, limit=30, offset=0):
    require_project(session, project_id)
    live_chapter = and_(
        StoryNode.project_id == project_id,
        StoryNode.kind == "chapter",
        StoryNode.deleted_at.is_(None),
    )
    owner = (
        select(ConversationJob.conversation_id)
        .where(
            ConversationJob.job_id == AIJob.id,
        )
        .correlate(AIJob)
        .scalar_subquery()
    )
    visible = [
        AIJob.project_id == project_id,
        visible_job_filter(),
        or_(AIJob.chapter_id.is_(None), live_chapter),
    ]
    recovery_parts = []
    for destination, tasks in (
        ("ai", TASKS),
        ("quality", {"quality_workflow"}),
        ("write", {"chapter_summary"}),
    ):
        recovery_parts.append(
            select(
                *_columns(
                    literal("recovery:") + AIJob.id,
                    "task_recovery",
                    literal("任务需要处理"),
                    literal("上次任务已中断或失败，请查看原因并选择后续操作。"),
                    destination,
                    "查看任务",
                    0,
                    job_id=AIJob.id,
                    conversation_id=owner,
                )
            )
            .select_from(AIJob)
            .outerjoin(StoryNode, StoryNode.id == AIJob.chapter_id)
            .where(
                *visible,
                AIJob.task_type.in_(tasks),
                AIJob.status.in_({"failed", "recovery_required"}),
                AIJob.accepted_version_id.is_(None),
            )
        )

    source_revision = func.json_extract(AIJob.result, "$.source_revision")
    candidates = (
        select(
            *_columns(
                literal("candidate:") + AIJob.id,
                "writing_candidate",
                literal("写作候选待查看"),
                literal("已有尚未采纳的正文候选；采纳前仍会检查来源与内容。"),
                "ai",
                "查看候选",
                1,
                job_id=AIJob.id,
                conversation_id=owner,
            )
        )
        .select_from(AIJob)
        .join(StoryNode, StoryNode.id == AIJob.chapter_id)
        .outerjoin(
            ChapterDocument,
            ChapterDocument.chapter_id == StoryNode.id,
        )
        .where(
            *visible,
            AIJob.task_type.in_(TASKS),
            AIJob.status == "succeeded",
            AIJob.accepted_version_id.is_(None),
            func.length(func.trim(func.json_extract(AIJob.result, "$.candidate_text"))) > 0,
            or_(source_revision.is_(None), source_revision == ChapterDocument.revision),
        )
    )

    quality_chapters = func.json_each(AIJob.result, "$.chapters").table_valued("key", "value")
    quality_id = func.json_extract(quality_chapters.c.value, "$.chapter_id")
    accepted = func.json_each(AIJobControl.effects, "$.accepted_chapters").table_valued(
        "key", "value"
    )
    quality = (
        select(
            *_columns(
                literal("quality:") + AIJob.id + literal(":") + quality_id,
                "quality_candidate",
                literal("优化候选待查看"),
                literal("请按协作章节顺序查看并采纳；质量报告和来源检查仍以详情为准。"),
                "quality",
                "查看优化稿",
                1,
                job_id=AIJob.id,
            )
        )
        .select_from(AIJob)
        .join(quality_chapters, true())
        .join(
            StoryNode,
            StoryNode.id == quality_id,
        )
        .join(AIJobControl, AIJobControl.job_id == AIJob.id)
        .where(
            AIJob.project_id == project_id,
            live_chapter,
            AIJob.task_type == "quality_workflow",
            AIJob.status.in_({"succeeded", "failed", "cancelled"}),
            func.json_extract(quality_chapters.c.value, "$.status").in_({"ready", "needs_review"}),
            func.length(func.trim(func.json_extract(quality_chapters.c.value, "$.candidate_text")))
            > 0,
            ~exists(select(accepted.c.key).where(accepted.c.key == quality_id)),
        )
    )

    latest_summary = (
        select(
            ChapterSummary.chapter_id,
            ChapterSummary.version_id,
            ChapterSummary.status,
            func.row_number()
            .over(
                partition_by=ChapterSummary.chapter_id,
                order_by=(ChapterSummary.created_at.desc(), ChapterSummary.id.desc()),
            )
            .label("position"),
        )
        .where(ChapterSummary.project_id == project_id, ChapterSummary.deleted_at.is_(None))
        .subquery()
    )
    valid_summary = and_(
        latest_summary.c.status == "valid",
        latest_summary.c.version_id == ChapterDocument.current_version_id,
    )
    chapter_work = (
        select(
            *_columns(
                literal("chapter:") + StoryNode.id,
                "chapter_completion",
                case(
                    (latest_summary.c.status == "stale", "章节总结已过期"),
                    (StoryNode.status != "completed", "正文尚未完成"),
                    else_="章节缺少有效总结",
                ),
                literal("核对已保存正文，再完成章节并更新连续性总结。"),
                "write",
                "打开章节",
                2,
            )
        )
        .select_from(StoryNode)
        .join(
            ChapterDocument,
            ChapterDocument.chapter_id == StoryNode.id,
        )
        .outerjoin(
            latest_summary,
            and_(latest_summary.c.chapter_id == StoryNode.id, latest_summary.c.position == 1),
        )
        .where(
            live_chapter,
            func.length(func.trim(ChapterDocument.content)) > 0,
            or_(StoryNode.status != "completed", ~func.coalesce(valid_summary, False)),
            ~exists(
                select(AIJob.id).where(
                    AIJob.project_id == project_id,
                    AIJob.chapter_id == StoryNode.id,
                    AIJob.task_type == "chapter_summary",
                    AIJob.status.in_({"queued", "running", "cancel_requested"}),
                )
            ),
        )
    )
    conflicts = (
        select(
            *_columns(
                literal("conflict:") + Conflict.id,
                "conflict",
                literal("内容冲突待确认"),
                func.substr(Conflict.message, 1, 240),
                "conflicts",
                "查看冲突",
                0,
            )
        )
        .select_from(Conflict)
        .join(StoryNode, StoryNode.id == Conflict.chapter_id)
        .where(
            Conflict.project_id == project_id,
            live_chapter,
            Conflict.status == "open",
        )
    )
    queue = union_all(*recovery_parts, candidates, quality, chapter_work, conflicts).subquery()
    counts = dict(session.execute(select(queue.c.kind, func.count()).group_by(queue.c.kind)).all())
    total = sum(counts.values())
    rows = session.execute(
        select(queue)
        .order_by(
            queue.c.priority,
            queue.c.chapter_title,
            queue.c.id,
        )
        .offset(offset)
        .limit(limit)
    ).mappings()
    items = []
    for row in rows:
        item = dict(row)
        item.pop("priority")
        if item["destination"] == "ai" and item["conversation_id"] is None:
            item["conversation_id"] = default_id(project_id, item["chapter_id"])
        items.append(item)
    return {
        "items": items,
        "total": total,
        "counts": counts,
        "next_offset": offset + limit if offset + limit < total else None,
    }
