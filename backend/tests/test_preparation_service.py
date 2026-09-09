from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from novel_harness.db.base import utc_now
from novel_harness.db.models import (
    ActivityEvent,
    CanonFact,
    ChapterDocument,
    Project,
    ProjectPreparation,
)
from novel_harness.schemas.preparation import (
    PreparationQuestionCommand,
    PreparationRevisionCommand,
)
from novel_harness.services.preparation import (
    acknowledge_impact,
    initialize_preparation,
    mutate_question,
    read_preparation,
    resume_preparation,
    skip_preparation,
)


def _database(client, project):
    return client.app.state.vault_registry.require(project["id"]).database


def test_read_is_side_effect_free_and_initialize_is_deterministic(client, project) -> None:
    database = _database(client, project)
    with database.session_scope() as session:
        view = read_preparation(session, project["id"])
        assert view.status == "not_started"
        assert view.revision == 0
        assert session.get(ProjectPreparation, project["id"]) is None

        created = initialize_preparation(session, project["id"])
        assert created.status == "in_progress"
        assert created.round == 1
        assert 1 <= len(created.questions) <= 5
        first_ids = [question.id for question in created.questions]

    with database.session_scope() as session:
        repeated = initialize_preparation(session, project["id"])
        assert [question.id for question in repeated.questions] == first_ids


def test_answer_is_confirmed_pinned_canon_and_idempotently_updated(client, project) -> None:
    database = _database(client, project)
    with database.session_scope() as session:
        preparation = initialize_preparation(session, project["id"])
        question = preparation.questions[0]
        answered = mutate_question(
            session,
            project["id"],
            question.id,
            PreparationQuestionCommand(
                revision=preparation.revision,
                action="answer",
                answer="必须找回所有死者的最后一封信。",
            ),
        )
        saved_question = next(item for item in answered.questions if item.id == question.id)
        assert saved_question.status == "answered"
        assert saved_question.canon_fact_id

        fact = session.get(CanonFact, saved_question.canon_fact_id)
        assert fact is not None
        assert fact.project_id == project["id"]
        assert fact.predicate == question.setting_key
        assert fact.value == "必须找回所有死者的最后一封信。"
        assert fact.status == "confirmed"
        assert fact.is_pinned is True
        assert fact.source_note == f"project_preparation:{question.id}"

        updated = mutate_question(
            session,
            project["id"],
            question.id,
            PreparationQuestionCommand(
                revision=answered.revision,
                action="answer",
                answer="必须找回七位死者各自的最后一封信。",
            ),
        )
        updated_question = next(item for item in updated.questions if item.id == question.id)
        assert updated_question.canon_fact_id == fact.id
        assert session.get(CanonFact, fact.id).value == "必须找回七位死者各自的最后一封信。"
        fact_count = session.scalar(
            select(func.count(CanonFact.id)).where(
                CanonFact.source_note == f"project_preparation:{question.id}"
            )
        )
        assert fact_count == 1


def test_editing_answer_after_written_chapter_creates_persistent_impact_notice(
    client, project, seeded_chapter
) -> None:
    database = _database(client, project)
    with database.session_scope() as session:
        preparation = initialize_preparation(session, project["id"])
        question = preparation.questions[0]
        answered = mutate_question(
            session,
            project["id"],
            question.id,
            PreparationQuestionCommand(
                revision=preparation.revision,
                action="answer",
                answer="先完成送信。",
            ),
        )
        session.add(
            ChapterDocument(
                chapter_id=seeded_chapter,
                project_id=project["id"],
                content="邮差在雨里敲响第一扇门。",
            )
        )

    with database.session_scope() as session:
        edited = mutate_question(
            session,
            project["id"],
            question.id,
            PreparationQuestionCommand(
                revision=answered.revision,
                action="answer",
                answer="先查清死者的真正身份。",
            ),
        )
        assert edited.impact_notice is not None
        assert edited.impact_notice.chapter_count == 1
        assert edited.impact_notice.fact_ids == [edited.questions[0].canon_fact_id]

        acknowledged = acknowledge_impact(
            session,
            project["id"],
            PreparationRevisionCommand(revision=edited.revision),
        )
        assert acknowledged.impact_notice is None


def test_impact_notice_accumulates_changed_facts_until_acknowledged(
    client, project, seeded_chapter
) -> None:
    database = _database(client, project)
    with database.session_scope() as session:
        state = initialize_preparation(session, project["id"])
        first, second = state.questions[:2]
        for question, answer in ((first, "规则一"), (second, "规则二")):
            state = mutate_question(
                session,
                project["id"],
                question.id,
                PreparationQuestionCommand(
                    revision=state.revision,
                    action="answer",
                    answer=answer,
                ),
            )
        session.add(
            ChapterDocument(
                chapter_id=seeded_chapter,
                project_id=project["id"],
                content="正文已经采用了两项准备设定。",
            )
        )

    with database.session_scope() as session:
        for question, answer in ((first, "规则一修订"), (second, "规则二修订")):
            state = mutate_question(
                session,
                project["id"],
                question.id,
                PreparationQuestionCommand(
                    revision=state.revision,
                    action="answer",
                    answer=answer,
                ),
            )
        assert state.impact_notice is not None
        expected_ids = {
            item.canon_fact_id for item in state.questions if item.id in {first.id, second.id}
        }
        assert set(state.impact_notice.fact_ids) == expected_ids


def test_skip_resume_and_revision_conflict(client, project) -> None:
    database = _database(client, project)
    with database.session_scope() as session:
        preparation = initialize_preparation(session, project["id"])
        skipped = skip_preparation(
            session,
            project["id"],
            PreparationRevisionCommand(revision=preparation.revision),
        )
        assert skipped.status == "skipped"

        resumed = resume_preparation(
            session,
            project["id"],
            PreparationRevisionCommand(revision=skipped.revision),
        )
        assert resumed.status == "in_progress"

        with pytest.raises(HTTPException) as error:
            skip_preparation(
                session,
                project["id"],
                PreparationRevisionCommand(revision=skipped.revision),
            )
        assert error.value.status_code == 409
        assert error.value.detail["code"] == "revision_conflict"

        kinds = set(
            session.scalars(
                select(ActivityEvent.kind).where(ActivityEvent.project_id == project["id"])
            )
        )
        assert {"preparation_skipped", "preparation_resumed"}.issubset(kinds)


def test_medium_priority_questions_remain_unresolved_until_processed(client, project) -> None:
    database = _database(client, project)
    with database.session_scope() as session:
        state = initialize_preparation(session, project["id"])
        for question in state.questions:
            state = mutate_question(
                session,
                project["id"],
                question.id,
                PreparationQuestionCommand(
                    revision=state.revision,
                    action="not_applicable",
                ),
            )
        preparation = session.get(ProjectPreparation, project["id"])
        medium = state.questions[0].model_copy(
            update={
                "id": "ai-medium-regression",
                "fingerprint": "medium-regression",
                "setting_key": "plot.medium_regression",
                "priority": "medium",
                "status": "open",
                "answer": None,
                "canon_fact_id": None,
            }
        )
        preparation.questions = [
            *preparation.questions,
            medium.model_dump(mode="json"),
        ]
        preparation.status = "in_progress"
        preparation.revision += 1
        session.flush()
        state = read_preparation(session, project["id"])
        assert state.unresolved_count == 1
        assert state.unresolved_high_count == 0

        deferred = mutate_question(
            session,
            project["id"],
            medium.id,
            PreparationQuestionCommand(
                revision=state.revision,
                action="defer",
            ),
        )
        assert deferred.status == "in_progress"
        assert deferred.unresolved_count == 1


def test_retracting_answer_removes_it_from_hard_context(client, project, seeded_chapter) -> None:
    from novel_harness.services.writing_context import collect_hard_context

    database = _database(client, project)
    with database.session_scope() as session:
        state = initialize_preparation(session, project["id"])
        question = state.questions[0]
        state = mutate_question(
            session,
            project["id"],
            question.id,
            PreparationQuestionCommand(
                revision=state.revision,
                action="answer",
                answer="信件只能由指定收信人拆开。",
            ),
        )
        hard, _ = collect_hard_context(
            session,
            session.get(Project, project["id"]),
            seeded_chapter,
            {},
        )
        packet = "\n".join(item.content for item in hard)
        assert "信件只能由指定收信人拆开" in packet
        assert question.question not in packet

        state = mutate_question(
            session,
            project["id"],
            question.id,
            PreparationQuestionCommand(
                revision=state.revision,
                action="not_applicable",
            ),
        )
        fact = session.scalar(
            select(CanonFact).where(CanonFact.source_note == f"project_preparation:{question.id}")
        )
        assert fact.status == "retracted"
        assert fact.is_pinned is False
        hard, _ = collect_hard_context(
            session,
            session.get(Project, project["id"]),
            seeded_chapter,
            {},
        )
        assert "信件只能由指定收信人拆开" not in "\n".join(item.content for item in hard)


def test_generic_library_cannot_edit_or_trash_preparation_fact(client, project) -> None:
    database = _database(client, project)
    with database.session_scope() as session:
        state = initialize_preparation(session, project["id"])
        question = state.questions[0]
        state = mutate_question(
            session,
            project["id"],
            question.id,
            PreparationQuestionCommand(
                revision=state.revision,
                action="answer",
                answer="准备流程是唯一修改入口。",
            ),
        )
        fact_id = next(item for item in state.questions if item.id == question.id).canon_fact_id
        fact_revision = session.get(CanonFact, fact_id).revision

    base = f"/api/v1/projects/{project['id']}"
    edited = client.patch(
        f"{base}/library/canon/{fact_id}",
        json={"revision": fact_revision, "content": "资料库绕过修改", "fields": {}},
    )
    trashed = client.delete(f"{base}/library/canon/{fact_id}")
    assert edited.status_code == 409
    assert edited.json()["detail"]["code"] == "PREPARATION_FACT_MANAGED"
    assert trashed.status_code == 409
    assert trashed.json()["detail"]["code"] == "PREPARATION_FACT_MANAGED"

    with database.session_scope() as session:
        fact = session.get(CanonFact, fact_id)
        assert fact.value == "准备流程是唯一修改入口。"
        assert fact.deleted_at is None


def test_generic_library_cannot_purge_legacy_deleted_preparation_fact(client, project) -> None:
    database = _database(client, project)
    with database.session_scope() as session:
        state = initialize_preparation(session, project["id"])
        question = state.questions[0]
        state = mutate_question(
            session,
            project["id"],
            question.id,
            PreparationQuestionCommand(
                revision=state.revision,
                action="answer",
                answer="不可从回收站永久清理。",
            ),
        )
        fact_id = next(item for item in state.questions if item.id == question.id).canon_fact_id
        fact = session.get(CanonFact, fact_id)
        fact.deleted_at = utc_now() - timedelta(days=31)
        fact.purge_after = utc_now() - timedelta(days=1)

    response = client.delete(
        f"/api/v1/projects/{project['id']}/trash/canon/{fact_id}/purge"
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "PREPARATION_FACT_MANAGED"
    with database.session_scope() as session:
        saved = session.scalar(
            select(CanonFact)
            .where(CanonFact.id == fact_id)
            .execution_options(include_deleted=True)
        )
        assert saved is not None
