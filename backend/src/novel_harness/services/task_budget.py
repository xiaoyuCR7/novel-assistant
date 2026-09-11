"""Local, read-only capacity preflight; never prepares or dispatches a job."""

from dataclasses import asdict

from fastapi import HTTPException

from novel_harness.ai.base import ExecutionLimits
from novel_harness.db.models import ChapterDocument
from novel_harness.schemas.ai import AIJobCreate, TaskBudgetPreflight
from novel_harness.services.entity_states import assert_project_entity_states_resolved
from novel_harness.services.pipeline import CreationPipeline
from novel_harness.services.projects import require_project
from novel_harness.services.versions import require_chapter
from novel_harness.services.writing_context import (
    collect_task_hard_context,
    validate_chapter_contract,
)


def preflight(session, payload: AIJobCreate, limits: ExecutionLimits) -> TaskBudgetPreflight:
    project = require_project(session, payload.project_id)
    document = None
    if payload.chapter_id is not None:
        chapter = require_chapter(session, payload.chapter_id)
        if chapter.project_id != payload.project_id:
            raise HTTPException(404, detail={"code": "CHAPTER_NOT_FOUND"})
        document = session.get(ChapterDocument, payload.chapter_id)
        if document is None or document.revision != payload.expected_revision:
            raise HTTPException(409, detail={"code": "SOURCE_CHANGED"})
    contract = document.contract if document else {}
    validate_chapter_contract(contract)
    if payload.chapter_id is not None:
        assert_project_entity_states_resolved(
            session, payload.project_id, payload.chapter_id
        )
    hard, _, _ = collect_task_hard_context(
        session,
        project,
        payload.chapter_id,
        contract,
        payload.task_type,
        payload.instructions,
    )
    required = CreationPipeline(None).estimate_initial_input(
        payload.task_type,
        payload.instructions,
        contract,
        {
            "token_budget": payload.token_budget,
            "execution_limits": limits.model_dump(),
            "fragments": [asdict(fragment) for fragment in hard],
            # New tasks include the continuation policy even with no history yet.
            "conversation": {"version": 1},
        },
    )
    effective = min(payload.token_budget, limits.context_capacity - limits.output_token_budget)
    can_fit = required <= effective
    return TaskBudgetPreflight(
        required_input_tokens=required,
        token_budget=payload.token_budget,
        output_token_budget=limits.output_token_budget,
        context_capacity=limits.context_capacity,
        effective_input_limit=effective,
        can_fit=can_fit,
        message=(
            "首阶段必要输入预计可容纳。"
            if can_fit
            else "必要输入超限；请提高本项目输入预算，或检查输出预留与模型容量。"
            "未裁剪正文或硬约束。"
        )
        + "仅估算首阶段硬输入，软参考可移除；后续生成阶段不保证可容纳。",
    )
