"""Offline proofs of the two-role handoff, quality gate and durable replay."""

from copy import deepcopy

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from novel_harness.ai.base import ContextBudgetError, StructuredResult, TextResult
from novel_harness.ai.demo import DemoProvider
from novel_harness.db.job_models import AIJobControl
from novel_harness.db.models import ChapterDocument, StoryNode
from novel_harness.services.job_context import capture_source
from novel_harness.services.job_state import command_hash
from novel_harness.services.job_store import JobStore
from novel_harness.services.quality import capture_quality_context
from novel_harness.services.versions import get_or_create_document


def review(score=85, *, issues=None, preserves=True):
    return {
        "scores": dict.fromkeys(
            ["readability", "engagement", "pacing", "clarity", "consistency"], score
        ),
        "summary": "叙事清楚，保持人物选择的因果联系。",
        "issues": issues or [],
        "next_guidance": "承接上一章留下的信件。",
        "preserves_story": preserves,
    }


def enqueue_quality(client, project, *, contents=("", ""), mode="collaborate", contract=None):
    database = client.app.state.vault_registry.require(project["id"]).database
    store = JobStore(database, project["id"])
    with database.session_scope() as session:
        chapters = []
        for index, content in enumerate(contents):
            node = StoryNode(
                project_id=project["id"],
                kind="chapter",
                title=f"第{index + 1}章",
                order_index=index,
            )
            session.add(node)
            session.flush()
            document = get_or_create_document(session, node.id)
            document.content = content
            if contract:
                document.contract = deepcopy(contract)
            chapters.append(node.id)

    def prepare(session):
        source = {
            "kind": "quality",
            "project_id": project["id"],
            "mode": mode,
            "instructions": "保持人物动机并加强可读性",
            "token_budget": 12000,
            "quality_target": 75,
            "chapters": [],
        }
        for chapter_id in chapters:
            node = session.get(StoryNode, chapter_id)
            source["chapters"].append(
                {
                    "chapter_id": chapter_id,
                    "title": node.title,
                    "source": capture_source(session, project["id"], chapter_id),
                    "context": capture_quality_context(
                        session,
                        project["id"],
                        chapter_id,
                        source["instructions"],
                    ),
                }
            )
        return {
            "source_snapshot": source,
            "provider_identity": {"mode": "demo"},
            "embedding_identity": None,
        }

    job = store.enqueue(
        dict(
            project_id=project["id"],
            chapter_id=None,
            task_type="quality_workflow",
            instructions="",
            token_budget=12000,
            expected_revision=None,
        ),
        "quality-worker",
        prepare,
    )
    return store, job["id"], chapters


class RecordingProvider(DemoProvider):
    def __init__(self, *, score=85, final_score=88):
        self.calls = []
        self.score = score
        self.final_score = final_score

    def generate_text(self, request):
        self.calls.append(deepcopy(request))
        text = "邮差拆开了来信，决定明早去钟楼寻找寄信人。"
        if request.task == "quality_rewrite":
            text = "邮差拆开来信，指尖停在那枚熟悉的印记上。明早，他必须去钟楼。"
        return TextResult(text=text, provider=self.name, model=self.model)

    def generate_structured(self, request, schema):
        self.calls.append(deepcopy(request))
        score = self.final_score if request.task == "quality_final_review" else self.score
        return StructuredResult(data=review(score), provider=self.name, model=self.model)


def test_quality_request_bounds_and_unique_chapters():
    from novel_harness.schemas.quality import QualityRunCreate

    payload = {"mode": "polish", "chapters": [{"chapter_id": "one", "expected_revision": 1}]}
    assert QualityRunCreate.model_validate(payload).quality_target == 75
    with pytest.raises(ValidationError):
        QualityRunCreate.model_validate({**payload, "chapters": payload["chapters"] * 2})
    with pytest.raises(ValidationError):
        QualityRunCreate.model_validate({**payload, "quality_target": 100})


def test_two_roles_alternate_and_writer_receives_optimized_candidate(client, project):
    from novel_harness.services.quality_generation import generate_quality

    store, job_id, chapters = enqueue_quality(client, project)
    provider = RecordingProvider(score=60)
    generate_quality(store, store.claim(job_id, "owner"), lambda observer: provider)
    assert [request.task for request in provider.calls] == [
        "quality_write",
        "quality_review",
        "quality_rewrite",
        "quality_final_review",
        "quality_write",
        "quality_review",
        "quality_rewrite",
        "quality_final_review",
    ]
    result = store.read(job_id)
    assert result["status"] == "succeeded"
    assert result["result"]["completed_chapters"] == 2
    candidate = result["result"]["chapters"][0]["candidate_text"]
    assert candidate in provider.calls[4].user_prompt
    assert "承接上一章留下的信件" in provider.calls[4].user_prompt
    assert [message["role"] for message in result["result"]["messages"]] == [
        "writer",
        "optimizer",
        "writer",
        "optimizer",
    ]
    with store.database.session_scope() as session:
        assert all(session.get(ChapterDocument, chapter).content == "" for chapter in chapters)


def test_quality_gate_stops_next_writer_and_approval_replays_paid_checkpoints(client, project):
    from novel_harness.services.quality_generation import generate_quality

    store, job_id, chapters = enqueue_quality(client, project)
    provider = RecordingProvider(score=55, final_score=60)
    fence = store.claim(job_id, "owner")
    with pytest.raises(HTTPException) as error:
        generate_quality(store, fence, lambda observer: provider)
    assert error.value.detail["code"] == "QUALITY_REVIEW_REQUIRED"
    assert len(provider.calls) == 4
    result = store.read(job_id)["result"]
    assert result["chapters"][0]["status"] == "needs_review"
    assert result["completed_chapters"] == 0
    assert len(result["chapters"]) == 1
    with store.write() as session:
        control = session.get(AIJobControl, job_id)
        control.effects = {
            "quality_approvals": {
                chapters[0]: command_hash({"content": result["chapters"][0]["candidate_text"]})
            }
        }
    provider.score, provider.final_score = 85, 90
    generate_quality(store, fence, lambda observer: provider)
    assert len(provider.calls) == 6
    assert store.read(job_id)["result"]["completed_chapters"] == 2


def test_good_existing_prose_only_needs_one_review(client, project):
    from novel_harness.services.quality_generation import generate_quality

    content = "邮差拆开信封，终于认出了寄信人的笔迹。"
    store, job_id, _ = enqueue_quality(client, project, contents=(content,), mode="polish")
    provider = RecordingProvider()
    generate_quality(store, store.claim(job_id, "owner"), lambda observer: provider)
    assert [request.task for request in provider.calls] == ["quality_review"]
    assert store.read(job_id)["result"]["chapters"][0]["candidate_text"] == content


def test_invalid_evidence_is_rejected_before_rewrite(client, project):
    from novel_harness.services.quality_generation import generate_quality

    store, job_id, _ = enqueue_quality(client, project, contents=("一封来信。",), mode="polish")

    class Invalid(RecordingProvider):
        def generate_structured(self, request, schema):
            result = super().generate_structured(request, schema)
            result.data["issues"] = [
                {
                    "dimension": "clarity",
                    "severity": "warning",
                    "quote": "不存在的内容",
                    "reason": "含混",
                    "suggestion": "写清楚",
                }
            ]
            return result

    provider = Invalid()
    with pytest.raises(ValueError, match="evidence"):
        generate_quality(store, store.claim(job_id, "owner"), lambda observer: provider)
    assert len(provider.calls) == 1
    assert store.read(job_id)["status"] == "failed"


def test_provider_guard_prevents_any_send(client, project):
    from novel_harness.services.quality_generation import generate_quality

    store, job_id, _ = enqueue_quality(client, project)
    provider = RecordingProvider()

    def guard():
        raise HTTPException(409, detail={"code": "PROVIDER_CHANGED"})

    with pytest.raises(HTTPException):
        generate_quality(
            store, store.claim(job_id, "owner"), lambda observer: provider, validate_provider=guard
        )
    assert provider.calls == []


def test_restart_after_review_reuses_writer_and_review_artifacts(client, project):
    from novel_harness.services.quality_generation import generate_quality

    class Crash(BaseException):
        pass

    store, job_id, _ = enqueue_quality(client, project, contents=("",))
    provider = RecordingProvider(score=60)

    def first_provider(observer):
        if observer.stage_key == "quality.0.rewrite":
            raise Crash()
        return provider

    with pytest.raises(Crash):
        generate_quality(store, store.claim(job_id, "first"), first_provider)
    assert [request.task for request in provider.calls] == ["quality_write", "quality_review"]
    store.recover("second")
    generate_quality(store, store.claim(job_id, "second"), lambda observer: provider)
    assert [request.task for request in provider.calls] == [
        "quality_write",
        "quality_review",
        "quality_rewrite",
        "quality_final_review",
    ]
    result = store.read(job_id)["result"]
    assert result["execution"]["stages"]["quality.0.write"]["cached"] is True
    assert result["execution"]["stages"]["quality.0.review"]["cached"] is True


def test_changed_source_after_model_return_prevents_publishing_prose(client, project):
    from novel_harness.services.quality_generation import generate_quality

    store, job_id, chapters = enqueue_quality(client, project, contents=("",))

    class ChangedSource(RecordingProvider):
        def generate_text(self, request):
            result = super().generate_text(request)
            with store.database.session_scope() as session:
                document = session.get(ChapterDocument, chapters[0])
                document.content = "作者正在编辑的新版本。"
                document.revision += 1
            return result

    provider = ChangedSource()
    with pytest.raises(HTTPException) as error:
        generate_quality(store, store.claim(job_id, "owner"), lambda observer: provider)
    assert error.value.detail["code"] == "QUALITY_SOURCE_CHANGED"
    assert len(provider.calls) == 1
    assert not store.read(job_id)["result"]["chapters"][0]["candidate_text"]


def test_budget_exhaustion_never_sends_or_truncates_manuscript(client, project):
    from novel_harness.services.quality_generation import generate_quality

    store, job_id, _ = enqueue_quality(client, project, contents=("重复正文。" * 1000,))
    with store.write() as session:
        control = session.get(AIJobControl, job_id)
        control.source_snapshot = {**control.source_snapshot, "token_budget": 256}
    provider = RecordingProvider()
    with pytest.raises(ContextBudgetError):
        generate_quality(store, store.claim(job_id, "owner"), lambda observer: provider)
    assert provider.calls == []


@pytest.mark.parametrize(
    "text", ["   \n", "正文\x00破损", "字" * 50001], ids=["blank", "nul", "too_long"]
)
def test_invalid_prose_is_never_saved_as_candidate(text):
    from novel_harness.services.quality_generation import validate_text

    with pytest.raises(ValueError):
        validate_text({"text": text, "provider": "demo", "model": "demo"})


@pytest.mark.parametrize(
    "scores",
    [
        dict.fromkeys(["readability", "engagement", "pacing", "clarity", "consistency"], True),
        {"readability": 100},
    ],
)
def test_review_requires_all_five_numeric_dimensions(scores):
    from novel_harness.schemas.quality import QualityReview

    with pytest.raises(ValidationError):
        QualityReview.model_validate({**review(), "scores": scores})


def test_contract_revelation_cannot_pass_by_high_model_scores(client, project):
    from novel_harness.services.quality_generation import generate_quality

    store, job_id, chapters = enqueue_quality(
        client, project, contract={"forbidden_revelations": ["钟楼"]}
    )
    provider = RecordingProvider(score=90, final_score=95)
    fence = store.claim(job_id, "owner")
    with pytest.raises(HTTPException) as error:
        generate_quality(store, fence, lambda observer: provider)
    assert error.value.detail["code"] == "QUALITY_REVIEW_REQUIRED"
    assert len(provider.calls) == 4
    result = store.read(job_id)["result"]
    assert len(result["chapters"]) == 1
    assert "禁止揭示" in result["chapters"][0]["handoff"]["optimizer_message"]
    with store.write() as session:
        control = session.get(AIJobControl, job_id)
        control.effects = {
            "quality_approvals": {
                chapters[0]: command_hash({"content": result["chapters"][0]["candidate_text"]})
            }
        }
    with pytest.raises(HTTPException):
        generate_quality(store, fence, lambda observer: provider)
    result = store.read(job_id)["result"]
    assert result["completed_chapters"] == 1
    assert result["chapters"][1]["status"] == "needs_review"
    assert len(provider.calls) == 8
