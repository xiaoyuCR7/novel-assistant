"""Explicit lightweight job pages. Details and source packets stay on detail endpoint."""

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select

from novel_harness.ai.prompts import prompt_version_for
from novel_harness.db.job_models import AIJobControl, AIJobStageAttempt
from novel_harness.db.models import AIJob
from novel_harness.services.job_state import ACTIVE, REPLACEABLE, replacement_requires_confirmation


def read_page(session, project_id, chapter_id, kind, active_only, limit, before):
    cursor_scope = [
        AIJob.project_id == project_id,
        AIJob.chapter_id == chapter_id,
        AIJob.task_type == "chapter_summary"
        if kind == "summary"
        else AIJob.task_type.not_in(
            {"chapter_summary", "preparation_analysis", "preparation_followup", "wiki_summary"}
        ),
    ]
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
    items = []
    for row in rows:
        item, control = dict(row), controls.get(row["id"])
        stage = stages.get(row["id"])
        actions = []
        if control and row["status"] in ACTIVE | {"recovery_required", "failed"}:
            actions.append("cancel")
        if (
            control
            and row["status"] in {"recovery_required", "failed"}
            and control.format_version == 1
            and row["prompt_version"] == prompt_version_for(row["task_type"])
        ):
            actions.append("resume")
        if control is None and row["status"] in {"recovery_required", "failed"}:
            actions.append("cancel")
        if row["status"] in REPLACEABLE and not (control and control.effects.get("summary_id")):
            actions.append("replace")
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
