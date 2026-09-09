"""Auditable feedback learning and conflict decision services."""

from __future__ import annotations

from difflib import unified_diff

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from novel_harness.db.models import (
    AIJob,
    Conflict,
    ConflictOption,
    EntityState,
    Feedback,
    PreferenceCandidate,
    StyleRule,
)
from novel_harness.services.projects import require_project
from novel_harness.services.serialization import serialize
from novel_harness.services.versions import require_chapter

LEGACY_PREFERENCE_PREFIX = "后续生成应参考作者在“"
LEGACY_PREFERENCE_SUFFIX = "”类别下的修正，避免重复同类问题。"
INEFFECTIVE_FEEDBACK_RULE = (StyleRule.rule_type == "feedback") & (
    (func.trim(StyleRule.instruction) == "")
    | StyleRule.instruction.like(LEGACY_PREFERENCE_PREFIX + "%" + LEGACY_PREFERENCE_SUFFIX)
)


def actionable_instruction(instruction: str) -> bool:
    value = instruction.strip()
    return bool(value) and not (
        value.startswith(LEGACY_PREFERENCE_PREFIX) and value.endswith(LEGACY_PREFERENCE_SUFFIX)
    )


def present_candidate(candidate: PreferenceCandidate) -> dict:
    return {
        **serialize(candidate),
        "requires_instruction": not actionable_instruction(candidate.instruction),
    }


def create_feedback(session: Session, values: dict) -> tuple[Feedback, PreferenceCandidate]:
    project = require_project(session, values["project_id"])
    chapter_id = values.get("chapter_id")
    if chapter_id:
        chapter = require_chapter(session, chapter_id)
        if chapter.project_id != project.id:
            raise HTTPException(status_code=409, detail={"code": "CROSS_PROJECT_REFERENCE"})
    if values.get("job_id"):
        job = session.get(AIJob, values["job_id"])
        if job is None:
            raise HTTPException(422, detail={"code": "FEEDBACK_JOB_UNAVAILABLE"})
        if job.project_id != project.id or job.chapter_id != chapter_id:
            raise HTTPException(409, detail={"code": "FEEDBACK_SCOPE_MISMATCH"})
    feedback = Feedback(**values)
    session.add(feedback)
    session.flush()
    diff_summary = "\n".join(
        unified_diff(
            values.get("original_text", "").splitlines(),
            values.get("corrected_text", "").splitlines(),
            fromfile="generated",
            tofile="corrected",
            lineterm="",
        )
    )
    tags = values.get("tags", [])
    category = tags[0] if tags else "general"
    instruction = values.get("comment", "").strip()
    candidate = PreferenceCandidate(
        project_id=project.id,
        category=category,
        instruction=instruction,
        diff_summary=diff_summary or "作者保留评价但未提供文本差异。",
        source_feedback_ids=[feedback.id],
    )
    session.add(candidate)
    session.flush()
    return feedback, candidate


def require_candidate(session: Session, candidate_id: str) -> PreferenceCandidate:
    candidate = session.get(PreferenceCandidate, candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail={"code": "PREFERENCE_NOT_FOUND"})
    return candidate


def confirm_candidate(
    session: Session, candidate_id: str, instruction: str | None = None
) -> PreferenceCandidate:
    from novel_harness.services.versions import begin_version_write

    begin_version_write(session)
    candidate = require_candidate(session, candidate_id)
    rule = None
    if candidate.linked_style_rule_id:
        rule = session.get(StyleRule, candidate.linked_style_rule_id)
        if rule is None or rule.deleted_at:
            raise HTTPException(409, detail={
                "code": "PREFERENCE_RULE_UNAVAILABLE",
                "message": "关联规则已删除或不可用，请先到回收站恢复；系统不会自动重建。",
            })
        if rule.project_id != candidate.project_id:
            raise HTTPException(409, detail={"code": "CROSS_PROJECT_REFERENCE"})
    # A live rule may have been edited through the material editor. A repeated
    # confirmation must not overwrite that author's newer wording.
    effective = (instruction if instruction is not None else (
        rule.instruction if rule else candidate.instruction
    )).strip()
    if not actionable_instruction(effective):
        raise HTTPException(422, detail={
            "code": "PREFERENCE_INSTRUCTION_REQUIRED",
            "message": "修正文本已存档。请补充明确的风格偏好后再确认，系统不会自行猜测修改规律。",
        })
    if rule is None:
        rule = StyleRule(
            project_id=candidate.project_id,
            rule_type="feedback",
            instruction=effective,
            status="confirmed",
            source_feedback_ids=candidate.source_feedback_ids,
        )
        session.add(rule)
        session.flush()
        candidate.linked_style_rule_id = rule.id
    elif rule.status != "confirmed" or rule.instruction != effective:
        rule.instruction = effective
        rule.status = "confirmed"
        rule.revision += 1
    candidate.instruction = effective
    candidate.status = "confirmed"
    session.flush()
    return candidate


def disable_candidate(session: Session, candidate_id: str) -> PreferenceCandidate:
    from novel_harness.services.versions import begin_version_write

    begin_version_write(session)
    candidate = require_candidate(session, candidate_id)
    candidate.status = "disabled"
    if candidate.linked_style_rule_id:
        rule = session.get(StyleRule, candidate.linked_style_rule_id)
        if rule and not rule.deleted_at and rule.status != "disabled":
            rule.status = "disabled"
            rule.revision += 1
    session.flush()
    return candidate


def active_style_rules(session: Session, project_id: str) -> list[StyleRule]:
    require_project(session, project_id)
    return list(
        session.scalars(
            select(StyleRule)
            .where(StyleRule.project_id == project_id, StyleRule.status == "confirmed",
                   ~INEFFECTIVE_FEEDBACK_RULE)
            .order_by(StyleRule.created_at)
        ).all()
    )


def list_preference_candidates(session: Session, project_id: str) -> list[PreferenceCandidate]:
    require_project(session, project_id)
    return list(
        session.scalars(
            select(PreferenceCandidate)
            .where(PreferenceCandidate.project_id == project_id)
            .order_by(PreferenceCandidate.created_at.desc())
        ).all()
    )


def list_conflicts_with_options(
    session: Session,
    project_id: str,
    *,
    status: str | None = "open",
    limit: int = 200,
) -> list[dict]:
    require_project(session, project_id)
    filters = [Conflict.project_id == project_id]
    if status is not None:
        filters.append(Conflict.status == status)
    conflicts = list(
        session.scalars(
            select(Conflict)
            .where(*filters)
            .order_by(Conflict.created_at.desc(), Conflict.id.desc())
            .limit(limit)
        ).all()
    )
    conflict_ids = [conflict.id for conflict in conflicts]
    options_by_conflict: dict[str, list[ConflictOption]] = {}
    if conflict_ids:
        for option in session.scalars(
            select(ConflictOption)
            .where(ConflictOption.conflict_id.in_(conflict_ids))
            .order_by(ConflictOption.created_at, ConflictOption.id)
        ).all():
            options_by_conflict.setdefault(option.conflict_id, []).append(option)
    state_resolutions = _batch_entity_state_resolution_contexts(session, conflicts)
    return [
        {
            "conflict": conflict,
            "options": options_by_conflict.get(conflict.id, []),
            "entity_state_resolution": state_resolutions.get(conflict.id),
        }
        for conflict in conflicts
    ]


def _batch_entity_state_resolution_contexts(
    session: Session, conflicts: list[Conflict]
) -> dict[str, dict]:
    state_conflicts = [item for item in conflicts if item.code == "ENTITY_STATE_CONFLICT"]
    if not state_conflicts:
        return {}
    from novel_harness.services.entity_states import (
        entity_state_conflict_evidence,
        project_entity_state_conflicts_by_chapter,
    )

    current_by_chapter = project_entity_state_conflicts_by_chapter(
        session,
        state_conflicts[0].project_id,
        {item.chapter_id for item in state_conflicts},
    )
    matches_by_conflict: dict[str, list[dict]] = {}
    state_ids: set[str] = set()
    for conflict in state_conflicts:
        stored_evidence = set(conflict.evidence)
        matching = [
            item
            for item in current_by_chapter[conflict.chapter_id]
            if entity_state_conflict_evidence(item) in stored_evidence
        ]
        matches_by_conflict[conflict.id] = matching
        state_ids.update(
            state_id for item in matching for state_id in item["state_ids"]
        )
    states_by_id = {}
    if state_ids:
        states_by_id = {
            state.id: state
            for state in session.scalars(
                select(EntityState)
                .where(
                    EntityState.project_id == state_conflicts[0].project_id,
                    EntityState.id.in_(state_ids),
                )
                .order_by(EntityState.created_at, EntityState.id)
            ).all()
        }
    return {
        conflict.id: {
            "conflicts": matches_by_conflict[conflict.id],
            "states": [
                states_by_id[state_id]
                for item in matches_by_conflict[conflict.id]
                for state_id in item["state_ids"]
                if state_id in states_by_id
            ],
        }
        for conflict in state_conflicts
    }


def create_conflict(session: Session, chapter_id: str, values: dict) -> Conflict:
    chapter = require_chapter(session, chapter_id)
    conflict = Conflict(project_id=chapter.project_id, chapter_id=chapter_id, **values)
    session.add(conflict)
    session.flush()
    return conflict


def require_conflict(session: Session, conflict_id: str) -> Conflict:
    conflict = session.get(Conflict, conflict_id)
    if conflict is None:
        raise HTTPException(status_code=404, detail={"code": "CONFLICT_NOT_FOUND"})
    return conflict


def suggest_options(session: Session, conflict_id: str) -> list[ConflictOption]:
    conflict = require_conflict(session, conflict_id)
    existing = list(
        session.scalars(
            select(ConflictOption)
            .where(ConflictOption.conflict_id == conflict_id)
            .order_by(ConflictOption.created_at)
        ).all()
    )
    if existing:
        return existing
    templates = [
        (
            "conservative",
            "按确认设定局部修订",
            "核对问题证据，仅修订本章不一致之处；不改变已确认设定。",
            "修改范围小，保留既有故事方向。",
            "可能还需核对相关段落。",
            ["定位证据原句", "作者确认具体改文"],
        ),
        (
            "balanced",
            "核对并调整相邻衔接",
            "保留确认设定，检查本章与相邻章节的因果、信息和叙述衔接。",
            "可一并处理问题涉及的前后文。",
            "修改范围可能扩展到相邻章节。",
            ["核对相邻章节", "修订后重做总结与检查"],
        ),
        (
            "radical",
            "提出结构或设定调整",
            "仅记录调整提案；涉及确认设定的改动必须由作者另行批准和编辑。",
            "为有意改变故事方向保留选择。",
            "可能影响多章连续性，不能自动应用。",
            ["列出受影响设定与章节", "作者单独确认", "重新总结并复核"],
        ),
    ]
    options = [
        ConflictOption(
            conflict_id=conflict.id,
            mode=mode,
            title=title,
            description=f"本地处理方向 · {conflict.code}：{conflict.message}\n{description}",
            benefit=benefit,
            risk=risk,
            ripple_effects=ripple,
            affected_entity_ids=conflict.related_entity_ids,
        )
        for mode, title, description, benefit, risk, ripple in templates
    ]
    session.add_all(options)
    session.flush()
    return options


def decide_conflict(session: Session, conflict_id: str, option_id: str, note: str) -> Conflict:
    conflict = require_conflict(session, conflict_id)
    option = session.get(ConflictOption, option_id)
    if option is None or option.conflict_id != conflict.id:
        raise HTTPException(status_code=409, detail={"code": "INVALID_CONFLICT_OPTION"})
    resolution = entity_state_resolution_context(session, conflict)
    if resolution is not None and resolution["conflicts"]:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ENTITY_STATE_CONFLICT_UNRESOLVED",
                "message": "请先撤回一条互相矛盾的人物状态记录，再关闭冲突。",
            },
        )
    conflict.status = "decided"
    conflict.selected_option_id = option.id
    conflict.decision_note = note
    session.flush()
    return conflict


def entity_state_resolution_context(
    session: Session,
    conflict: Conflict,
    *,
    scopes: set[tuple[str, str]] | None = None,
) -> dict | None:
    if conflict.code != "ENTITY_STATE_CONFLICT":
        return None
    from novel_harness.services.entity_states import (
        entity_state_conflict_evidence,
        project_entity_state_conflicts,
    )

    current = project_entity_state_conflicts(
        session, conflict.project_id, conflict.chapter_id
    )
    if scopes is None:
        stored_evidence = set(conflict.evidence)
        matching = [
            item
            for item in current
            if entity_state_conflict_evidence(item) in stored_evidence
        ]
    else:
        matching = [
            item for item in current if (item["entity_id"], item["key"]) in scopes
        ]
    state_ids = {
        state_id for item in matching for state_id in item["state_ids"]
    }
    states = []
    if state_ids:
        states = list(
            session.scalars(
                select(EntityState)
                .where(
                    EntityState.project_id == conflict.project_id,
                    EntityState.id.in_(state_ids),
                )
                .order_by(EntityState.created_at, EntityState.id)
            ).all()
        )
    return {"conflicts": matching, "states": states}


def resolve_entity_state_conflict(
    session: Session,
    conflict_id: str,
    state_id: str,
    revision: int,
    note: str,
) -> dict:
    from novel_harness.services.entity_states import edit_entity_state
    from novel_harness.services.versions import begin_version_write

    begin_version_write(session)
    conflict = require_conflict(session, conflict_id)
    if conflict.code != "ENTITY_STATE_CONFLICT":
        raise HTTPException(
            status_code=409, detail={"code": "NOT_ENTITY_STATE_CONFLICT"}
        )
    before = entity_state_resolution_context(session, conflict)
    participating_ids = {
        item.id for item in (before or {}).get("states", [])
    }
    if state_id not in participating_ids:
        raise HTTPException(
            status_code=409,
            detail={"code": "ENTITY_STATE_NOT_IN_CONFLICT"},
        )
    edit_entity_state(
        session,
        conflict.project_id,
        state_id,
        revision,
        {"status": "retracted"},
    )
    scopes = {
        (item["entity_id"], item["key"])
        for item in (before or {}).get("conflicts", [])
    }
    after = entity_state_resolution_context(session, conflict, scopes=scopes)
    resolved = not (after or {}).get("conflicts")
    if resolved:
        conflict.status = "decided"
        conflict.selected_option_id = None
        conflict.decision_note = note
    else:
        from novel_harness.services.entity_states import entity_state_conflict_evidence

        remaining = (after or {})["conflicts"]
        conflict.evidence = sorted(
            entity_state_conflict_evidence(item) for item in remaining
        )
        conflict.related_entity_ids = sorted(
            {item["entity_id"] for item in remaining}
        )
    session.flush()
    return {
        "conflict": conflict,
        "options": list(
            session.scalars(
                select(ConflictOption)
                .where(ConflictOption.conflict_id == conflict.id)
                .order_by(ConflictOption.created_at)
            ).all()
        ),
        "entity_state_resolution": after,
        "resolved": resolved,
    }
