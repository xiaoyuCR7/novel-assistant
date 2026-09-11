"""Explicit conversation ownership; legacy jobs belong to a virtual scope default."""

from uuid import NAMESPACE_URL, uuid5

from fastapi import HTTPException
from sqlalchemy import and_, exists, func, or_, select

from novel_harness.db.base import utc_now
from novel_harness.db.models import AIJob, ConversationJob, ConversationThread, Project, StoryNode
from novel_harness.services.job_state import command_hash

TASKS = frozenset(
    {
        "chat",
        "continue",
        "full_chapter",
        "plan",
        "draft",
        "review",
        "suggest",
        "rewrite",
        "scene_description",
    }
)
UNFINISHED = {"queued", "running", "cancel_requested", "recovery_required"}


def default_id(project_id, chapter_id):
    return str(uuid5(NAMESPACE_URL, f"novel-conversation:{project_id}:{chapter_id or 'global'}"))


def virtual_default(session, project_id, chapter_id):
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(404, detail={"code": "PROJECT_NOT_FOUND"})
    return ConversationThread(
        id=default_id(project_id, chapter_id),
        project_id=project_id,
        chapter_id=chapter_id,
        title="默认会话",
        status="active",
        revision=1,
        is_default=True,
        inherited_job_ids=[],
        creation_hash="",
        created_at=project.created_at,
        updated_at=project.created_at,
    )


def resolve(session, project_id, conversation_id=None, *, chapter_id=None, check_scope=False):
    identifier = conversation_id or default_id(project_id, chapter_id)
    thread = session.get(ConversationThread, identifier)
    if thread is None:
        scopes = (
            [chapter_id]
            if check_scope or not conversation_id
            else [
                None,
                *session.scalars(
                    select(StoryNode.id).where(
                        StoryNode.project_id == project_id, StoryNode.kind == "chapter"
                    )
                ),
            ]
        )
        scope = next((s for s in scopes if default_id(project_id, s) == identifier), False)
        if scope is False:
            raise HTTPException(404, detail={"code": "CONVERSATION_NOT_FOUND"})
        thread = virtual_default(session, project_id, scope)
    if thread.project_id != project_id or (check_scope and thread.chapter_id != chapter_id):
        raise HTTPException(404, detail={"code": "CONVERSATION_NOT_FOUND"})
    return thread


def materialize(session, thread):
    if session.get(ConversationThread, thread.id) is None:
        session.add(thread)
        session.flush()
    return thread


def own_filter(thread):
    mapped = exists(
        select(ConversationJob.job_id).where(
            ConversationJob.job_id == AIJob.id,
            ConversationJob.conversation_id == thread.id,
        )
    )
    if thread.is_default:
        mapped = or_(
            mapped,
            ~exists(
                select(ConversationJob.job_id).where(
                    ConversationJob.job_id == AIJob.id,
                )
            ),
        )
    return and_(
        AIJob.project_id == thread.project_id,
        AIJob.chapter_id == thread.chapter_id,
        AIJob.task_type.in_(TASKS),
        mapped,
    )


def visible_job_filter(*, include_archived=False):
    """SQL-only filter for cross-chapter work queues; internal jobs are unaffected."""
    blocked = (
        ConversationThread.status == "deleted"
        if include_archived
        else (ConversationThread.status != "active")
    )
    has_mapping = exists(select(ConversationJob.job_id).where(ConversationJob.job_id == AIJob.id))
    mapped_blocked = exists(
        select(ConversationJob.job_id)
        .join(
            ConversationThread,
            ConversationThread.id == ConversationJob.conversation_id,
        )
        .where(ConversationJob.job_id == AIJob.id, blocked)
    )
    default_blocked = exists(
        select(ConversationThread.id).where(
            ConversationThread.project_id == AIJob.project_id,
            or_(
                ConversationThread.chapter_id == AIJob.chapter_id,
                and_(ConversationThread.chapter_id.is_(None), AIJob.chapter_id.is_(None)),
            ),
            ConversationThread.is_default.is_(True),
            blocked,
        )
    )
    return or_(
        AIJob.task_type.not_in(TASKS), and_(~mapped_blocked, or_(has_mapping, ~default_blocked))
    )


def job_conversation_id(session, job):
    if job.task_type not in TASKS:
        return None
    mapping = session.get(ConversationJob, job.id)
    return mapping.conversation_id if mapping else default_id(job.project_id, job.chapter_id)


def visible_inherited_ids(session, thread):
    if not thread.inherited_job_ids:
        return []
    ids = set(
        session.scalars(
            select(AIJob.id).where(
                AIJob.project_id == thread.project_id,
                AIJob.chapter_id == thread.chapter_id,
                AIJob.task_type.in_(TASKS),
                AIJob.id.in_(thread.inherited_job_ids),
            )
        )
    )
    owners = dict(
        session.execute(
            select(ConversationJob.job_id, ConversationJob.conversation_id).where(
                ConversationJob.job_id.in_(ids)
            )
        ).all()
    )
    deleted = set(
        session.scalars(
            select(ConversationThread.id).where(
                ConversationThread.project_id == thread.project_id,
                ConversationThread.status == "deleted",
            )
        )
    )
    fallback = default_id(thread.project_id, thread.chapter_id)
    return [
        identifier
        for identifier in thread.inherited_job_ids
        if identifier in ids and owners.get(identifier, fallback) not in deleted
    ]


def history_filter(session, thread):
    if thread.status == "deleted":
        return AIJob.id.in_([])
    return or_(own_filter(thread), AIJob.id.in_(visible_inherited_ids(session, thread)))


def assert_writable(session, project_id, chapter_id, conversation_id=None):
    thread = resolve(session, project_id, conversation_id, chapter_id=chapter_id, check_scope=True)
    if thread.status != "active":
        raise HTTPException(
            409,
            detail={
                "code": "CONVERSATION_DELETED"
                if thread.status == "deleted"
                else "CONVERSATION_ARCHIVED",
                "message": "请先恢复此会话，或选择其他会话。",
            },
        )
    return thread


def assert_job_writable(session, job):
    if job.task_type not in TASKS:
        return
    assert_writable(session, job.project_id, job.chapter_id, job_conversation_id(session, job))
    state = job.context_snapshot.get("conversation", {})
    for source_id in state.get("source_conversation_ids", []):
        if resolve(session, job.project_id, source_id).status == "deleted":
            raise HTTPException(
                409,
                detail={
                    "code": "CONVERSATION_SOURCE_DELETED",
                    "message": "冻结上下文中的来源会话已删除，请恢复来源会话或重新发起任务。",
                },
            )


def describe(session, thread, *, job_count=None):
    count = (
        job_count
        if job_count is not None
        else session.scalar(select(func.count()).select_from(AIJob).where(own_filter(thread))) or 0
    )
    return {
        field: getattr(thread, field)
        for field in (
            "id",
            "project_id",
            "chapter_id",
            "title",
            "status",
            "revision",
            "is_default",
            "parent_conversation_id",
            "branch_from_job_id",
            "created_at",
            "updated_at",
        )
    } | {"job_count": count + len(thread.inherited_job_ids or [])}


def create(session, project_id, identifier, chapter_id, title, *, parent_id=None, from_job_id=None):
    body = dict(
        id=identifier,
        chapter_id=chapter_id,
        title=title,
        parent_id=parent_id,
        from_job_id=from_job_id,
    )
    digest = command_hash(body)
    existing = session.get(ConversationThread, identifier)
    if existing:
        if existing.project_id != project_id or existing.creation_hash != digest:
            raise HTTPException(409, detail={"code": "IDEMPOTENCY_CONFLICT"})
        return describe(session, existing)
    if identifier == default_id(project_id, chapter_id):
        raise HTTPException(409, detail={"code": "CONVERSATION_RESERVED_ID"})
    inherited = []
    if parent_id:
        parent = assert_writable(session, project_id, chapter_id, parent_id)
        ids = session.scalars(
            select(AIJob.id)
            .where(
                history_filter(session, parent),
                AIJob.status == "succeeded",
            )
            .order_by(AIJob.created_at, AIJob.id)
        ).all()
        if from_job_id not in ids:
            raise HTTPException(404, detail={"code": "BRANCH_POINT_NOT_FOUND"})
        inherited = ids[: ids.index(from_job_id) + 1]
    now = utc_now()
    thread = ConversationThread(
        id=identifier,
        project_id=project_id,
        chapter_id=chapter_id,
        title=title,
        status="active",
        revision=1,
        is_default=False,
        parent_conversation_id=parent_id,
        branch_from_job_id=from_job_id,
        inherited_job_ids=inherited,
        creation_hash=digest,
        created_at=now,
        updated_at=now,
    )
    session.add(thread)
    session.flush()
    return describe(session, thread)


def _blockers(session, thread):
    active = session.scalars(
        select(AIJob).where(
            AIJob.project_id == thread.project_id,
            AIJob.task_type.in_(TASKS),
            AIJob.status.in_(UNFINISHED),
        )
    )
    for job in active:
        owner = resolve(session, job.project_id, job_conversation_id(session, job))
        sources = job.context_snapshot.get("conversation", {}).get("source_conversation_ids", [])
        if owner.id == thread.id or thread.id in sources:
            return job.id
        if owner.inherited_job_ids and session.scalar(
            select(AIJob.id)
            .where(
                own_filter(thread),
                AIJob.id.in_(owner.inherited_job_ids),
            )
            .limit(1)
        ):
            return job.id
    return None


def update(session, project_id, identifier, revision, *, title=None, status=None):
    thread = resolve(session, project_id, identifier)
    # Replaying a lost successful response is harmless when the desired state already matches.
    matches = (title is None or title == thread.title) and (
        status is None or status == thread.status
    )
    if revision != thread.revision:
        if matches and revision == thread.revision - 1:
            return describe(session, thread)
        raise HTTPException(409, detail={"code": "CONVERSATION_CHANGED"})
    if status in {"archived", "deleted"} and (blocker := _blockers(session, thread)):
        raise HTTPException(
            409,
            detail={
                "code": "CONVERSATION_TASK_ACTIVE",
                "job_id": blocker,
                "message": "此会话或依赖它的分支仍有未完成任务，请先取消任务。",
            },
        )
    materialize(session, thread)
    if not matches:
        thread.title = title if title is not None else thread.title
        thread.status = status if status is not None else thread.status
        thread.revision += 1
        thread.updated_at = utc_now()
        session.flush()
    return describe(session, thread)


def list_threads(session, project_id, chapter_id, status, query, limit, before):
    default = resolve(session, project_id, chapter_id=chapter_id, check_scope=True)
    filters = [
        ConversationThread.project_id == project_id,
        ConversationThread.chapter_id == chapter_id,
        ConversationThread.status == status,
    ]
    default_matches = default.status == status
    if query:
        pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        text_match = or_(
            AIJob.instructions.ilike(pattern, escape="\\"),
            func.json_extract(AIJob.result, "$.reply").ilike(pattern, escape="\\"),
            func.json_extract(AIJob.result, "$.candidate_text").ilike(pattern, escape="\\"),
        )
        mapped = exists(
            select(ConversationJob.job_id)
            .where(
                ConversationJob.job_id == AIJob.id,
                ConversationJob.conversation_id == ConversationThread.id,
            )
            .correlate(AIJob, ConversationThread)
        )
        unmapped = ~exists(
            select(ConversationJob.job_id)
            .where(
                ConversationJob.job_id == AIJob.id,
            )
            .correlate(AIJob)
        )
        inherited = func.json_each(ConversationThread.inherited_job_ids).table_valued("value")
        inherited_match = exists(
            select(inherited.c.value)
            .where(
                inherited.c.value == AIJob.id,
            )
            .correlate(AIJob, ConversationThread)
        )
        filters.append(
            or_(
                ConversationThread.title.ilike(pattern, escape="\\"),
                exists(
                    select(AIJob.id)
                    .where(
                        AIJob.project_id == project_id,
                        AIJob.chapter_id == chapter_id,
                        AIJob.task_type.in_(TASKS),
                        text_match,
                        or_(
                            mapped,
                            and_(ConversationThread.is_default.is_(True), unmapped),
                            and_(inherited_match, visible_job_filter(include_archived=True)),
                        ),
                    )
                    .correlate(ConversationThread)
                ),
            )
        )
        default_matches = default_matches and (
            query.casefold() in default.title.casefold()
            or bool(
                session.scalar(select(AIJob.id).where(own_filter(default), text_match).limit(1))
            )
        )
    if before:
        cursor = resolve(session, project_id, before, chapter_id=chapter_id, check_scope=True)
        valid_cursor = (cursor.id == default.id and default_matches) or session.scalar(
            select(ConversationThread.id).where(*filters, ConversationThread.id == before)
        )
        if not valid_cursor:
            raise HTTPException(404, detail={"code": "CURSOR_NOT_FOUND"})
        filters.append(
            or_(
                ConversationThread.created_at < cursor.created_at,
                and_(
                    ConversationThread.created_at == cursor.created_at,
                    ConversationThread.id < cursor.id,
                ),
            )
        )
        default_matches = default_matches and (
            default.created_at.replace(tzinfo=None),
            default.id,
        ) < (cursor.created_at.replace(tzinfo=None), cursor.id)
    rows = session.scalars(
        select(ConversationThread)
        .where(*filters)
        .order_by(
            ConversationThread.created_at.desc(),
            ConversationThread.id.desc(),
        )
        .limit(limit + 1)
    ).all()
    if default_matches and not any(row.id == default.id for row in rows):
        rows.append(default)
    rows.sort(key=lambda row: (row.created_at.replace(tzinfo=None), row.id), reverse=True)
    owners = func.coalesce(ConversationJob.conversation_id, default.id)
    counts = dict(
        session.execute(
            select(owners, func.count())
            .select_from(AIJob)
            .outerjoin(
                ConversationJob,
                ConversationJob.job_id == AIJob.id,
            )
            .where(
                AIJob.project_id == project_id,
                AIJob.chapter_id == chapter_id,
                AIJob.task_type.in_(TASKS),
                owners.in_([row.id for row in rows[:limit]]),
            )
            .group_by(owners)
        ).all()
    )
    return {
        "items": [describe(session, row, job_count=counts.get(row.id, 0)) for row in rows[:limit]],
        "next_cursor": rows[limit - 1].id if len(rows) > limit else None,
        "default_conversation_id": default.id,
    }
