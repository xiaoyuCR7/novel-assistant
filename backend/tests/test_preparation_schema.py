import pytest
from pydantic import ValidationError

from novel_harness.db.models import ProjectPreparation
from novel_harness.schemas.preparation import (
    PreparationQuestion,
    PreparationQuestionCommand,
    ProjectPreparationRead,
)


def question(index=0, **changes):
    value = {
        "id": f"question-{index}",
        "fingerprint": f"fingerprint-{index}",
        "round": 1,
        "setting_key": f"world.rule_{index}",
        "category": "world_rules",
        "question": "这条世界规则如何运作？",
        "rationale": "它会在多个章节中反复使用。",
        "impact_areas": ["continuity"],
        "priority": "high",
        "answer_format": "short_text",
        "options": [],
        "source_ids": ["project:core"],
        "origin": "template",
        "status": "open",
        "answer": None,
        "canon_fact_id": None,
        "updated_at": None,
    }
    value.update(changes)
    return value


def test_project_has_one_durable_bounded_preparation(client, project):
    database = client.app.state.vault_registry.require(project["id"]).database
    with database.session_scope() as session:
        session.add(
            ProjectPreparation(
                project_id=project["id"],
                status="in_progress",
                round=1,
                questions=[question()],
            )
        )
    with database.session_scope() as session:
        saved = session.get(ProjectPreparation, project["id"])
        assert saved.questions[0]["setting_key"] == "world.rule_0"
        assert saved.revision == 1


def test_preparation_read_caps_total_questions_at_thirteen(project):
    payload = {
        "project_id": project["id"],
        "revision": 1,
        "status": "in_progress",
        "round": 2,
        "source_hash": "a" * 64,
        "questions": [question(index) for index in range(14)],
        "generation_job_ids": [],
        "impact_notice": None,
    }

    with pytest.raises(ValidationError):
        ProjectPreparationRead.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"revision": 0, "action": "defer"},
        {"revision": 1, "action": "answer", "answer": "   "},
        {"revision": 1, "action": "answer", "answer": []},
        {"revision": 1, "action": "answer", "answer": "x" * 10_001},
        {"revision": 1, "action": "answer", "answer": ["x"] * 31},
        {"revision": 1, "action": "defer", "unknown": True},
    ],
)
def test_question_commands_are_strict_and_require_meaningful_answers(payload):
    with pytest.raises(ValidationError):
        PreparationQuestionCommand.model_validate(payload)


def test_question_schema_rejects_unknown_fields_and_too_many_options():
    with pytest.raises(ValidationError):
        PreparationQuestion.model_validate({**question(), "provider_answer": "替作者决定"})
    with pytest.raises(ValidationError):
        PreparationQuestion.model_validate(
            {**question(), "answer_format": "choice", "options": [str(i) for i in range(9)]}
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "answered", "answer_format": "short_text", "answer": ["错误列表"]},
        {"status": "answered", "answer_format": "long_text", "answer": ["错误列表"]},
        {"status": "answered", "answer_format": "ordered_list", "answer": "错误文本"},
        {
            "status": "answered",
            "answer_format": "choice",
            "options": ["甲", "乙"],
            "answer": ["甲"],
        },
    ],
)
def test_answered_question_schema_enforces_declared_answer_format(changes):
    with pytest.raises(ValidationError):
        PreparationQuestion.model_validate({**question(), **changes})
