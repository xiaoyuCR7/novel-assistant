"""Explicit lightweight job pages. Details and source packets stay on detail endpoint."""

import json

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select

from novel_harness.ai.prompts import prompt_version_for
from novel_harness.db.job_models import AIJobControl, AIJobStageAttempt
from novel_harness.db.models import AIJob
from novel_harness.services.job_state import ACTIVE, REPLACEABLE, replacement_requires_confirmation


def read_page(session, project_id, chapter_id, kind, active_only, limit, before,
              *, conversation_id=None):
    cursor_scope = [
        AIJob.project_id == project_id,
        *([] if kind == "quality" else [AIJob.chapter_id == chapter_id]),
        AIJob.task_type == "quality_workflow"
        if kind == "quality"
        else AIJob.task_type == "chapter_summary"
        if kind == "summary"
        else AIJob.task_type.not_in(
            {
                "chapter_summary", "preparation_analysis", "preparation_followup",
                "wiki_summary", "quality_workflow",
            }
        ),
    ]
    thread = None
    if kind == "writing":
        from novel_harness.services import conversation_threads as threads
        thread = threads.resolve(session, project_id, conversation_id,
                                 chapter_id=chapter_id, check_scope=True)
        cursor_scope.append(threads.history_filter(session, thread))
    scope = list(cursor_scope)
    if active_only:
        scope.append(AIJob.status.in_(ACTIVE | {"recovery_required"}))
    if before:
        cursor = session.execute(
            select(AIJob.id, AIJob.created_at).where(*cursor_scope, AIJob.id == before)
        ).first()
        if not cursor:
            raise HTTPException(404, detail={"code": "CURSOR_NOT_FOUND"})
        scope.append(
            or_(
                AIJob.created_at < cursor.created_at,
                and_(AIJob.created_at == cursor.created_at, AIJob.id < cursor.id),
            )
        )
    fields = [
        AIJob.id,
        AIJob.project_id,
        AIJob.chapter_id,
        AIJob.task_type,
        AIJob.status,
        AIJob.created_at,
        AIJob.updated_at,
        AIJob.accepted_version_id,
        AIJob.prompt_version,
        AIJob.error_code,
        AIJob.error_message,
        func.substr(AIJob.instructions, 1, 1000).label("instructions"),
        func.substr(
            func.coalesce(
                func.json_extract(AIJob.result, "$.reply"),
                func.json_extract(AIJob.result, "$.candidate_text"),
                "",
            ),
            1,
            500,
        ).label("preview"),
    ]
    if thread is not None:
        fields.append(func.json_extract(
            AIJob.context_snapshot, "$.conversation.source_conversation_ids",
        ).label("_conversation_sources"))
    rows = (
        session.execute(
            select(*fields)
            .where(*scope)
            .order_by(AIJob.created_at.desc(), AIJob.id.desc())
            .limit(limit + 1)
        )
        .mappings()
        .all()
    )
    more, rows = len(rows) > limit, rows[:limit]
    ids = [row["id"] for row in rows]
    controls = {
        c.job_id: c
        for c in session.execute(
            select(
                AIJobControl.job_id,
                AIJobControl.control_revision,
                AIJobControl.format_version,
                AIJobControl.recovery_reason,
                AIJobControl.effects,
                func.json_extract(AIJobControl.command, "$.replaces_job_id").label(
                    "replaces_job_id"
                ),
            ).where(AIJobControl.job_id.in_(ids))
        )
    }
    latest = (
        select(
            AIJobStageAttempt.job_id,
            AIJobStageAttempt.stage_key,
            AIJobStageAttempt.status,
            AIJobStageAttempt.outcome,
            func.row_number()
            .over(
                partition_by=AIJobStageAttempt.job_id,
                order_by=(
                    AIJobStageAttempt.created_at.desc(),
                    AIJobStageAttempt.attempt_no.desc(),
                    AIJobStageAttempt.stage_key.desc(),
                ),
            )
            .label("rn"),
        )
        .where(AIJobStageAttempt.job_id.in_(ids))
        .subquery()
    )
    stages = {row.job_id: row for row in session.execute(select(latest).where(latest.c.rn == 1))}
    owners, owner_statuses, source_ids = {}, {}, {}
    if thread is not None:
        from novel_harness.db.models import ConversationJob, ConversationThread
        owners = dict(session.execute(select(
            ConversationJob.job_id, ConversationJob.conversation_id,
        ).where(ConversationJob.job_id.in_(ids))).all())
        owner_ids = set(owners.values()) | {threads.default_id(project_id, chapter_id)}
        for row in rows:
            value = row.get("_conversation_sources")
            try:
                sources = json.loads(value) if isinstance(value, str) else []
            except (TypeError, ValueError):
                sources = []
            source_ids[row["id"]] = [source for source in sources if isinstance(source, str)] if (
                isinstance(sources, list)
            ) else []
            owner_ids.update(source_ids[row["id"]])
        owner_statuses = dict(session.execute(select(
            ConversationThread.id, ConversationThread.status,
        ).where(ConversationThread.id.in_(owner_ids))).all())
    items = []
    for row in rows:
        item, control = dict(row), controls.get(row["id"])
        item.pop("_conversation_sources", None)
        stage = stages.get(row["id"])
        actions = []
        if control and row["status"] in ACTIVE | {"recovery_required", "failed"}:
            actions.append("cancel")
        if (
            control
            and row["status"] in {"recovery_required", "failed"}
            and control.format_version == 1
            and row["prompt_version"] == prompt_version_for(row["task_type"])
            and not (
                row["task_type"] == "quality_workflow" and control.effects.get("accepted_chapters")
            )
        ):
            actions.append("resume")
        if control is None and row["status"] in {"recovery_required", "failed"}:
            actions.append("cancel")
        if (
            row["status"] in REPLACEABLE
            and row["task_type"] != "quality_workflow"
            and not (control and control.effects.get("summary_id"))
        ):
            actions.append("replace")
        if thread is not None:
            owner_id = owners.get(row["id"], threads.default_id(project_id, chapter_id))
            inherited = owner_id != thread.id
            source_deleted = any(owner_statuses.get(source) == "deleted"
                                 for source in source_ids.get(row["id"], []))
            if inherited or owner_statuses.get(owner_id, "active") != "active" or source_deleted:
                actions = [action for action in actions if action not in {"resume", "replace"}]
            item.update(conversation_id=owner_id, inherited=inherited)
        item.update(
            control_revision=control.control_revision if control else 0,
            recovery_reason=control.recovery_reason if control else None,
            effects=control.effects if control else {},
            allowed_actions=actions,
            current_stage=stage.stage_key if stage else None,
            replaces_job_id=control.replaces_job_id if control else None,
            replacement_requires_confirmation=replacement_requires_confirmation(control, stage),
            status_url=f"/api/v1/projects/{project_id}/ai/jobs/{row['id']}",
        )
        items.append(item)
    return {"items": items, "next_cursor": rows[-1]["id"] if more else None}
