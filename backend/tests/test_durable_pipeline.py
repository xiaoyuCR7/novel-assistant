import pytest
from sqlalchemy import select, text

from novel_harness.ai.base import AITextRequest, ProviderExecutionError
from novel_harness.ai.demo import DemoProvider
from novel_harness.db.models import ChapterDocument, Conflict
from novel_harness.services.pipeline import CreationPipeline


class Crash(BaseException):
    pass


@pytest.mark.parametrize("method, task", [("_text", "chat"), ("_structured", "plan")])
def test_pipeline_rejects_inference_without_checkpoints(method, task):
    pipeline = CreationPipeline(DemoProvider())
    with pytest.raises(RuntimeError, match="CHECKPOINT_REQUIRED"):
        getattr(pipeline, method)(
            AITextRequest(task=task, developer_instruction="", user_prompt="")
        )


def enqueue(client, project, chapter_id, task="full_chapter"):
    from novel_harness.services.job_context import capture_source
    from novel_harness.services.job_store import JobStore

    database = client.app.state.vault_registry.require(project["id"]).database
    store = JobStore(database, project["id"])
    command = dict(
        project_id=project["id"],
        chapter_id=chapter_id,
        task_type=task,
        instructions="推进故事",
        token_budget=12000,
        expected_revision=1,
    )
    job = store.enqueue(
        command,
        "pipeline-test",
        lambda session: {
            "source_snapshot": capture_source(session, project["id"], chapter_id),
            "provider_identity": {"mode": "demo"},
            "embedding_identity": None,
        },
    )
    return store, job["id"]


def test_checkpointed_pipeline_resume_keeps_author_in_control(client, project, seeded_chapter):
    store, job_id = enqueue(client, project, seeded_chapter)
    calls = []

    class Counted(DemoProvider):
        def generate_text(self, request):
            calls.append(request.task)
            return super().generate_text(request)

        def generate_structured(self, request, schema):
            calls.append(request.task)
            return super().generate_structured(request, schema)

    def factory(observer):
        if observer.stage_key == "continuity_review":
            raise Crash()
        return Counted()

    with pytest.raises(Crash):
        CreationPipeline(DemoProvider()).run_durable(
            store,
            store.claim(job_id, "first"),
            factory,
        )
    store.recover("second")
    CreationPipeline(DemoProvider()).run_durable(
        store,
        store.claim(job_id, "second"),
        lambda observer: Counted(),
    )
    assert calls == [
        "plan",
        "draft",
        "continuity_review",
        "style_review",
    ]
    job = store.read(job_id)
    assert job["status"] == "succeeded"
    assert job["result"]["candidate_text"]
    assert job["result"]["execution"]["provider"] == "demo"
    with store.database.job_session_scope() as session:
        assert session.get(ChapterDocument, seeded_chapter).content == ""
        assert (
            session.scalar(
                text("SELECT count(*) FROM search_documents WHERE source_type='checkpoint'")
            )
            == 0
        )


def test_invalid_review_rolls_back_all_alerts(client, project, seeded_chapter):
    store, job_id = enqueue(client, project, seeded_chapter)

    class Invalid(DemoProvider):
        def generate_structured(self, request, schema):
            result = super().generate_structured(request, schema)
            if request.task == "style_review":
                result.data["issues"] = [
                    {
                        "code": "bad",
                        "severity": "warning",
                        "message": "不存在的证据",
                        "evidence": ["unavailable"],
                        "related_entity_ids": [],
                    }
                ]
            return result

    with pytest.raises(ProviderExecutionError):
        CreationPipeline(DemoProvider()).run_durable(
            store,
            store.claim(job_id, "owner"),
            lambda observer: Invalid(),
        )
    with store.database.job_session_scope() as session:
        assert list(session.scalars(select(Conflict))) == []


def test_full_pipeline_with_valid_issues_uses_exactly_six_calls(client, project, seeded_chapter):
    store, job_id = enqueue(client, project, seeded_chapter)
    calls = []

    class Reviewed(DemoProvider):
        def generate_structured(self, request, schema):
            calls.append(request.task)
            result = super().generate_structured(request, schema)
            if request.task == "continuity_review":
                result.data["issues"] = [
                    {
                        "code": "REVIEW",
                        "severity": "warning",
                        "message": "检查开场",
                        "evidence": [request.user_prompt[:8]],
                        "related_entity_ids": [],
                    }
                ]
            return result

        def generate_text(self, request):
            calls.append(request.task)
            return super().generate_text(request)

    CreationPipeline(None).run_durable(
        store, store.claim(job_id, "owner"), lambda observer: Reviewed()
    )
    assert calls == [
        "plan",
        "draft",
        "continuity_review",
        "style_review",
        "resolve",
        "rewrite",
        "final_review",
    ]
    assert store.read(job_id)["status"] == "succeeded"
