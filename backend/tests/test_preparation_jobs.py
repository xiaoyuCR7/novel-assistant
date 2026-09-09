import pytest
from pydantic import ValidationError

from novel_harness.ai.base import StructuredResult
from novel_harness.ai.prompts import PREPARATION_PROMPT_VERSION, prompt_version_for
from novel_harness.db.job_models import AIJobControl
from novel_harness.db.models import AIJob
from novel_harness.schemas.preparation import PreparationQuestionCommand
from novel_harness.services.job_store import JobStore
from novel_harness.services.preparation import (
    initialize_preparation,
    mutate_question,
    read_preparation,
)
from novel_harness.services.preparation_generation import (
    capture_preparation_source,
    generate_preparation,
    prepare_preparation_job,
)


class InvalidAnsweringProvider:
    name = "test"
    model = "invalid-answerer"

    def __init__(self, observer):
        self.attempt_observer = observer

    def generate_structured(self, request, schema):
        self.attempt_observer.before_send()
        return StructuredResult(
            provider=self.name,
            model=self.model,
            data={
                "questions": [
                    {
                        "setting_key": "world.cost",
                        "category": "world_rules",
                        "question": "力量需要付出什么代价？",
                        "rationale": "会约束所有冲突解法。",
                        "impact_areas": ["plot"],
                        "priority": "high",
                        "answer_format": "short_text",
                        "options": [],
                        "source_ids": ["project:core"],
                        "answer": "寿命",
                    }
                ]
            },
        )


class TooManyQuestionsProvider:
    name = "test"
    model = "too-many-questions"

    def __init__(self, observer):
        self.attempt_observer = observer

    def generate_structured(self, request, schema):
        self.attempt_observer.before_send()
        return StructuredResult(
            provider=self.name,
            model=self.model,
            data={
                "questions": [
                    {
                        "setting_key": f"followup.rule_{index}",
                        "category": "world_rules",
                        "question": f"第 {index} 项规则是什么？",
                        "rationale": "会反复影响长篇连续性。",
                        "impact_areas": ["continuity"],
                        "priority": "high",
                        "answer_format": "short_text",
                        "options": [],
                        "source_ids": ["project:core"],
                    }
                    for index in range(6)
                ]
            },
        )


def _store(client, project):
    vault = client.app.state.vault_registry.require(project["id"])
    return vault.database, JobStore(vault.database, project["id"])


def _enqueue(store, task_type, key, identity):
    command = {
        "project_id": store.project_id,
        "chapter_id": None,
        "task_type": task_type,
        "instructions": "只发现高影响设定缺口，不替作者回答。",
        "token_budget": 12_000,
        "expected_revision": None,
    }
    return store.enqueue(
        command,
        key,
        lambda session: prepare_preparation_job(session, store.project_id, task_type, identity),
    )


def test_preparation_has_dedicated_prompt_version_and_operation(client, project) -> None:
    assert PREPARATION_PROMPT_VERSION == "project-preparation-v1"
    assert prompt_version_for("preparation_analysis") == PREPARATION_PROMPT_VERSION
    assert prompt_version_for("preparation_followup") == PREPARATION_PROMPT_VERSION

    database, store = _store(client, project)
    identity = {"output_token_budget": 1024, "context_capacity": 32768}
    with database.session_scope() as session:
        initialize_preparation(session, project["id"])
    job = _enqueue(store, "preparation_analysis", "same-key", identity)
    assert job["task_type"] == "preparation_analysis"

    writing = store.enqueue(
        {
            "project_id": project["id"],
            "chapter_id": None,
            "task_type": "chat",
            "instructions": "讨论开篇",
            "token_budget": 12_000,
            "expected_revision": None,
        },
        "same-key",
        lambda session: {
            "source_snapshot": {},
            "provider_identity": identity,
            "embedding_identity": None,
        },
    )
    assert writing["id"] != job["id"]


def test_preparation_read_reconnects_to_latest_actionable_job(client, project) -> None:
    database, store = _store(client, project)
    identity = {"output_token_budget": 1024, "context_capacity": 32768}
    with database.session_scope() as session:
        initialize_preparation(session, project["id"])

    job = _enqueue(store, "preparation_analysis", "discover-after-refresh", identity)

    with database.session_scope() as session:
        refreshed = read_preparation(session, project["id"])
        assert refreshed.actionable_job_id == job["id"]


def test_failed_preparation_job_uses_preparation_source_when_resumed(client, project) -> None:
    database = _store(client, project)[0]
    base = f"/api/v1/projects/{project['id']}"
    client.post(f"{base}/preparation/initialize")
    queued = client.post(
        f"{base}/preparation/analyze",
        headers={"Idempotency-Key": "resume-preparation"},
        json={},
    ).json()
    with database.job_session_scope() as session:
        session.get(AIJob, queued["id"]).status = "failed"
        control = session.get(AIJobControl, queued["id"])
        control.recovery_reason = "stage_failed"
        control.control_revision += 1

    failed = client.get(f"{base}/ai/jobs/{queued['id']}").json()
    resumed = client.post(
        f"{base}/ai/jobs/{queued['id']}/resume",
        headers={"Idempotency-Key": "resume-preparation-action"},
        json={
            "expected_control_revision": failed["control_revision"],
            "confirm_unknown": False,
        },
    )
    assert resumed.status_code == 202
    assert resumed.json()["status"] == "queued"


def test_demo_analysis_merges_only_remaining_initial_slots(client, project) -> None:
    from novel_harness.ai.demo import DemoProvider

    database, store = _store(client, project)
    identity = {"output_token_budget": 1024, "context_capacity": 32768}
    with database.session_scope() as session:
        initial = initialize_preparation(session, project["id"])
        assert len(initial.questions) <= 5
        source = capture_preparation_source(session, project["id"], "preparation_analysis")
        assert len(source["questions"]) <= 13
        assert "allowed_reference_ids" in source

    job = _enqueue(store, "preparation_analysis", "analysis-demo", identity)
    fence = store.claim(job["id"], "test-epoch")
    generate_preparation(store, fence, lambda observer: DemoProvider())

    with database.session_scope() as session:
        result = read_preparation(session, project["id"])
        assert len(result.questions) <= 8
        assert any(question.origin == "ai" for question in result.questions)
        saved_job = store.serialize_in_session(session, job["id"])
        assert saved_job["status"] == "succeeded"


def test_invalid_provider_answer_publishes_no_questions(client, project) -> None:
    database, store = _store(client, project)
    identity = {"output_token_budget": 1024, "context_capacity": 32768}
    with database.session_scope() as session:
        before = initialize_preparation(session, project["id"])

    job = _enqueue(store, "preparation_analysis", "invalid-answer", identity)
    fence = store.claim(job["id"], "test-epoch")
    with pytest.raises(ValidationError):
        generate_preparation(store, fence, lambda observer: InvalidAnsweringProvider(observer))

    with database.session_scope() as session:
        after = read_preparation(session, project["id"])
        assert after.questions == before.questions


def test_followup_requires_resolved_initial_round_and_can_run_only_once(client, project) -> None:
    from novel_harness.ai.demo import DemoProvider

    database, store = _store(client, project)
    identity = {"output_token_budget": 1024, "context_capacity": 32768}
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
        assert state.status == "completed"

    job = _enqueue(store, "preparation_followup", "followup-one", identity)
    fence = store.claim(job["id"], "test-epoch")
    generate_preparation(store, fence, lambda observer: DemoProvider())

    with database.session_scope() as session:
        state = read_preparation(session, project["id"])
        assert state.round == 2
        assert len([question for question in state.questions if question.round == 2]) <= 5

    with pytest.raises(Exception) as error:
        _enqueue(store, "preparation_followup", "followup-two", identity)
    assert error.value.detail["code"] == "PREPARATION_FOLLOWUP_ALREADY_RUN"


def test_followup_rejects_provider_batch_over_five_questions(client, project) -> None:
    database, store = _store(client, project)
    identity = {"output_token_budget": 1024, "context_capacity": 32768}
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
        before = state.questions

    job = _enqueue(store, "preparation_followup", "followup-too-many", identity)
    fence = store.claim(job["id"], "test-epoch")
    with pytest.raises(ValueError, match="maximum of 5"):
        generate_preparation(
            store,
            fence,
            lambda observer: TooManyQuestionsProvider(observer),
        )

    with database.session_scope() as session:
        after = read_preparation(session, project["id"])
        assert after.questions == before
        assert after.round == 1
