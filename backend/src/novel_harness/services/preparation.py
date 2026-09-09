"""Transactional lifecycle for the optional project-preparation interview."""

from __future__ import annotations

import hashlib
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import func, select

from novel_harness.db.base import utc_now
from novel_harness.db.models import (
    ActivityEvent,
    AIJob,
    CanonFact,
    ChapterDocument,
    Project,
    ProjectPreparation,
)
from novel_harness.schemas.preparation import (
    PreparationQuestion,
    PreparationQuestionCommand,
    PreparationRevisionCommand,
    ProjectPreparationRead,
)
from novel_harness.services.preparation_templates import select_template_questions
from novel_harness.services.versions import begin_version_write


def _require_project(session, project_id: str) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(404, detail={"code": "PROJECT_NOT_FOUND"})
    return project


def _source_hash(project: Project) -> str:
    source = "\n".join((project.title, project.premise, project.genre))
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _hydrate_question(item: dict) -> PreparationQuestion:
    payload = dict(item)
    if isinstance(payload.get("updated_at"), str):
        payload["updated_at"] = datetime.fromisoformat(payload["updated_at"])
    return PreparationQuestion.model_validate(payload)


def _actionable_job_id(session, project_id: str) -> str | None:
    latest = session.execute(
        select(AIJob.id, AIJob.status)
        .where(
            AIJob.project_id == project_id,
            AIJob.task_type.in_(("preparation_analysis", "preparation_followup")),
        )
        .order_by(AIJob.created_at.desc(), AIJob.id.desc())
        .limit(1)
    ).first()
    if latest and latest.status in {
        "queued",
        "running",
        "cancel_requested",
        "recovery_required",
        "failed",
    }:
        return latest.id
    return None


def _view(session, preparation: ProjectPreparation) -> ProjectPreparationRead:
    questions = [_hydrate_question(item) for item in preparation.questions]
    impact_notice = preparation.impact_notice
    if impact_notice and isinstance(impact_notice.get("created_at"), str):
        impact_notice = {
            **impact_notice,
            "created_at": datetime.fromisoformat(impact_notice["created_at"]),
        }
    return ProjectPreparationRead(
        project_id=preparation.project_id,
        revision=preparation.revision,
        status=preparation.status,
        round=preparation.round,
        source_hash=preparation.source_hash,
        questions=questions,
        generation_job_ids=preparation.generation_job_ids,
        actionable_job_id=_actionable_job_id(session, preparation.project_id),
        impact_notice=impact_notice,
        answered_count=sum(question.status == "answered" for question in questions),
        unresolved_count=sum(
            question.status in {"open", "deferred"} for question in questions
        ),
        unresolved_high_count=sum(
            question.priority == "high" and question.status in {"open", "deferred"}
            for question in questions
        ),
    )


def read_preparation(session, project_id: str) -> ProjectPreparationRead:
    _require_project(session, project_id)
    preparation = session.get(ProjectPreparation, project_id)
    if preparation is None:
        return ProjectPreparationRead(
            project_id=project_id,
            revision=0,
            status="not_started",
            round=0,
        )
    return _view(session, preparation)


def initialize_preparation(session, project_id: str) -> ProjectPreparationRead:
    begin_version_write(session)
    project = _require_project(session, project_id)
    existing = session.get(ProjectPreparation, project_id)
    if existing is not None:
        return _view(session, existing)

    questions = select_template_questions(project.genre, project.premise, answered_keys=set())
    preparation = ProjectPreparation(
        project_id=project_id,
        status="in_progress",
        round=1,
        source_hash=_source_hash(project),
        questions=questions,
    )
    session.add(preparation)
    session.add(
        ActivityEvent(
            project_id=project_id,
            kind="preparation_initialized",
            entity_id=project_id,
            details={"question_count": len(questions), "source": "template"},
        )
    )
    session.flush()
    return _view(session, preparation)


def _require_current(
    session, project_id: str, command: PreparationRevisionCommand | PreparationQuestionCommand
) -> ProjectPreparation:
    begin_version_write(session)
    preparation = session.get(ProjectPreparation, project_id)
    if preparation is None:
        raise HTTPException(409, detail={"code": "PREPARATION_NOT_INITIALIZED"})
    if preparation.revision != command.revision:
        raise HTTPException(
            409,
            detail={
                "code": "revision_conflict",
                "current": _view(session, preparation).model_dump(mode="json"),
            },
        )
    return preparation


def _save_questions(preparation: ProjectPreparation, questions: list[dict]) -> None:
    preparation.questions = questions
    preparation.revision += 1
    if preparation.status != "skipped":
        unresolved = any(item["status"] in {"open", "deferred"} for item in questions)
        preparation.status = "in_progress" if unresolved else "completed"


def _normalize_answer(question: PreparationQuestion, answer):
    if question.answer_format == "ordered_list":
        if not isinstance(answer, list):
            raise HTTPException(
                422, detail={"code": "INVALID_PREPARATION_ANSWER_FORMAT"}
            )
        return [item.strip() for item in answer]

    if not isinstance(answer, str):
        raise HTTPException(422, detail={"code": "INVALID_PREPARATION_ANSWER_FORMAT"})
    value = answer.strip()
    if question.answer_format == "choice" and value not in question.options:
        raise HTTPException(422, detail={"code": "INVALID_PREPARATION_CHOICE"})
    return value


def _upsert_fact(session, project_id: str, question: PreparationQuestion, answer):
    source_note = f"project_preparation:{question.id}"
    fact = session.scalar(
        select(CanonFact).where(
            CanonFact.project_id == project_id,
            CanonFact.source_note == source_note,
            CanonFact.deleted_at.is_(None),
        )
    )
    if fact is None:
        fact = CanonFact(
            project_id=project_id,
            predicate=question.setting_key,
            value=answer,
            source_note=source_note,
            status="confirmed",
            is_pinned=True,
        )
        session.add(fact)
    else:
        fact.predicate = question.setting_key
        fact.value = answer
        fact.status = "confirmed"
        fact.is_pinned = True
        fact.revision += 1
    session.flush()
    from novel_harness.services.search_index import sync_record

    sync_record(session, fact)
    return fact


def _retract_fact(session, project_id: str, question: PreparationQuestion):
    fact = session.get(CanonFact, question.canon_fact_id) if question.canon_fact_id else None
    if fact is None:
        fact = session.scalar(
            select(CanonFact).where(
                CanonFact.project_id == project_id,
                CanonFact.source_note == f"project_preparation:{question.id}",
                CanonFact.deleted_at.is_(None),
            )
        )
    if fact is None:
        return None
    fact.status = "retracted"
    fact.is_pinned = False
    fact.revision += 1
    session.flush()
    from novel_harness.services.search_index import sync_record

    sync_record(session, fact)
    return fact


def mutate_question(
    session,
    project_id: str,
    question_id: str,
    command: PreparationQuestionCommand,
) -> ProjectPreparationRead:
    preparation = _require_current(session, project_id, command)
    questions = list(preparation.questions)
    index = next((i for i, item in enumerate(questions) if item["id"] == question_id), None)
    if index is None:
        raise HTTPException(404, detail={"code": "PREPARATION_QUESTION_NOT_FOUND"})

    question = _hydrate_question(questions[index])
    prior_answered = question.status == "answered"
    prior_answer = question.answer
    fact = None
    fact_changed = False
    now = utc_now()
    if command.action == "answer":
        answer = _normalize_answer(question, command.answer)
        fact = _upsert_fact(session, project_id, question, answer)
        fact_changed = not prior_answered or prior_answer != answer
        question.status = "answered"
        question.answer = answer
        question.canon_fact_id = fact.id
    else:
        if prior_answered:
            fact = _retract_fact(session, project_id, question)
            fact_changed = fact is not None
        question.status = "deferred" if command.action == "defer" else command.action
        question.answer = None
        question.canon_fact_id = None
    question.updated_at = now
    questions[index] = question.model_dump(mode="json")
    _save_questions(preparation, questions)

    if prior_answered and fact_changed and fact is not None:
        chapter_count = session.scalar(
            select(func.count(ChapterDocument.chapter_id)).where(
                ChapterDocument.project_id == project_id,
                func.length(func.trim(ChapterDocument.content)) > 0,
            )
        )
        if chapter_count:
            existing_fact_ids = (
                preparation.impact_notice.get("fact_ids", []) if preparation.impact_notice else []
            )
            preparation.impact_notice = {
                "fact_ids": list(dict.fromkeys([*existing_fact_ids, fact.id]))[:13],
                "chapter_count": chapter_count,
                "created_at": now.isoformat(),
            }

    session.add(
        ActivityEvent(
            project_id=project_id,
            kind=f"preparation_question_{command.action}",
            entity_id=fact.id if fact is not None else question.id,
            details={"question_id": question.id, "setting_key": question.setting_key},
        )
    )
    session.flush()
    return _view(session, preparation)


def skip_preparation(
    session, project_id: str, command: PreparationRevisionCommand
) -> ProjectPreparationRead:
    preparation = _require_current(session, project_id, command)
    preparation.status = "skipped"
    preparation.revision += 1
    session.add(
        ActivityEvent(
            project_id=project_id,
            kind="preparation_skipped",
            entity_id=project_id,
            details={"revision": preparation.revision},
        )
    )
    session.flush()
    return _view(session, preparation)


def resume_preparation(
    session, project_id: str, command: PreparationRevisionCommand
) -> ProjectPreparationRead:
    preparation = _require_current(session, project_id, command)
    questions = [_hydrate_question(item) for item in preparation.questions]
    unresolved = any(item.status in {"open", "deferred"} for item in questions)
    preparation.status = "in_progress" if unresolved else "completed"
    preparation.revision += 1
    session.add(
        ActivityEvent(
            project_id=project_id,
            kind="preparation_resumed",
            entity_id=project_id,
            details={"revision": preparation.revision},
        )
    )
    session.flush()
    return _view(session, preparation)


def acknowledge_impact(
    session, project_id: str, command: PreparationRevisionCommand
) -> ProjectPreparationRead:
    preparation = _require_current(session, project_id, command)
    preparation.impact_notice = None
    preparation.revision += 1
    session.add(
        ActivityEvent(
            project_id=project_id,
            kind="preparation_impact_acknowledged",
            entity_id=project_id,
            details={"revision": preparation.revision},
        )
    )
    session.flush()
    return _view(session, preparation)
