"""Regressions from the opt-in September DeepSeek live audit; zero network calls."""

import json

import httpx
import pytest
from job_helpers import run_job
from sqlalchemy import select

from novel_harness.ai.base import AITextRequest, ProviderExecutionError
from novel_harness.ai.compatible_provider import CompatibleProvider
from novel_harness.ai.demo import DemoProvider
from novel_harness.db.job_models import AIJobStageAttempt
from novel_harness.schemas.stages import PlanOutput
from novel_harness.services.pipeline import _compact_output_schema
from novel_harness.services.summary_generation import _compact_schema


@pytest.mark.parametrize("compact", [_compact_output_schema, _compact_schema])
def test_schema_compaction_preserves_property_names_and_literal_defaults(compact):
    schema = PlanOutput.model_json_schema()
    assert compact(schema)["$defs"]["Scene"]["properties"]["title"] == {"type": "string"}
    custom = {"type": "object", "properties": {
        "default": {"type": "string", "default": "x", "title": "Label"},
        "payload": {"const": {"title": "literal", "default": "literal"}},
    }}
    result = compact(custom)
    assert result["properties"]["default"] == {"type": "string"}
    assert result["properties"]["payload"]["const"] == {"title": "literal", "default": "literal"}
    assert schema == PlanOutput.model_json_schema()


def _provider_response(status=200, data=None, stream=False):
    seen = []

    def send(request):
        seen.append(json.loads(request.content))
        if stream:
            payload = seen[-1]
            usage = {"prompt_tokens": 3, "completion_tokens": 2}
            frames = [{"choices": [{"delta": {"content": "OK"}, "finish_reason": "stop"}]}]
            if payload.get("stream_options", {}).get("include_usage"):
                frames.append({"choices": [], "usage": usage})
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                                 content="".join("data: " + json.dumps(f) + "\n\n" for f in frames))
        return httpx.Response(status, json=data or {"error": {"message": "fake-secret"}})

    return CompatibleProvider("https://example.com/v1", "model", "fake-secret",
                              transport=httpx.MockTransport(send)), seen


def test_stream_requests_usage_without_extra_generation():
    provider, seen = _provider_response(stream=True)
    result = provider.generate_text(AITextRequest(
        task="chat", developer_instruction="", user_prompt="test", stream_preview=True,
    ))
    assert result.usage_source == "provider"
    assert result.usage == {"input_tokens": 3, "output_tokens": 2}
    assert len(seen) == 1


def test_later_failure_reports_a_successfully_resumed_stage_as_success(queued_job):
    from novel_harness.services.job_stages import StageRunner

    store, job_id = queued_job
    fence = store.claim(job_id, "worker")

    def fail(observer):
        observer.before_send()
        raise ProviderExecutionError("invalid", outcome="known", code="INVALID_STAGE_OUTPUT")

    with pytest.raises(ProviderExecutionError):
        StageRunner(store, fence).run("style_review.repair", {}, fail, dict)
    paused = store.read(job_id)
    store.resume(job_id, "retry", paused["control_revision"], False)
    fence = store.claim(job_id, "worker")

    def success(observer):
        observer.before_send()
        return {"text": "ok", "usage": {"input_tokens": 3, "output_tokens": 2}}

    runner = StageRunner(store, fence)
    runner.run("style_review.repair", {}, success, dict)
    with pytest.raises(ProviderExecutionError):
        runner.run("resolve", {}, fail, dict)
    metrics = store.read(job_id)["result"]["execution"]["stages"]["style_review.repair"]
    assert metrics["status"] == "succeeded"
    assert metrics["error_code"] is None


@pytest.mark.parametrize("status,code", [
    (401, "PROVIDER_AUTH_FAILED"), (402, "PROVIDER_BALANCE_INSUFFICIENT"),
    (429, "PROVIDER_RATE_LIMITED"), (503, "PROVIDER_UNAVAILABLE"),
])
def test_http_failures_are_actionable_redacted_and_never_auto_retried(status, code):
    provider, seen = _provider_response(status)
    with pytest.raises(ProviderExecutionError) as caught:
        provider.generate_text(AITextRequest(
            task="chat", developer_instruction="", user_prompt="x",
        ))
    assert caught.value.code == code
    assert caught.value.outcome == "known"
    assert "fake-secret" not in str(caught.value)
    assert len(seen) == 1


@pytest.mark.parametrize("reason", ["length", "content_filter"])
def test_incomplete_outputs_have_specific_failure_codes(reason):
    provider, _ = _provider_response(data={
        "choices": [{"message": {"content": "partial"}, "finish_reason": reason}],
    })
    with pytest.raises(ProviderExecutionError) as caught:
        provider.generate_text(AITextRequest(
            task="draft", developer_instruction="", user_prompt="x",
        ))
    assert caught.value.code == ("OUTPUT_TRUNCATED" if reason == "length" else "CONTENT_FILTERED")


@pytest.mark.parametrize("failure", ["null_predicate", "invalid_evidence"])
def test_review_repairs_once_and_reuses_the_draft(client, project, seeded_chapter, failure):
    calls = []

    class Provider(DemoProvider):
        def generate_text(self, request):
            calls.append(request.task)
            return super().generate_text(request)

        def generate_structured(self, request, schema):
            calls.append(request.task)
            result = super().generate_structured(request, schema)
            if request.task == "continuity_review" and calls.count(request.task) == 1:
                result.data["observations"] = [{
                    "kind": "action", "evidence": request.user_prompt[:6]
                    if failure == "null_predicate" else "MISSING_PRIVATE_QUOTE",
                    "entity_id": None, "predicate": None if failure == "null_predicate" else "",
                    "value": None,
                }]
            return result

    client.app.state.ai_provider = Provider()
    job = run_job(client, f"/api/v1/projects/{project['id']}/ai/jobs", json={
        "project_id": project["id"], "chapter_id": seeded_chapter, "task_type": "full_chapter",
    }).json()
    assert job["status"] == "succeeded"
    assert calls.count("continuity_review") == 2
    assert calls.count("draft") == calls.count("plan") == 1
    stages = job["result"]["execution"]["stages"]
    assert stages["continuity_review"]["status"] == "failed"
    assert stages["continuity_review.repair"]["status"] == "succeeded"
    assert stages["continuity_review"]["diagnostic_id"]
    assert stages["continuity_review"]["duration_ms"] >= 0
    assert "MISSING_PRIVATE_QUOTE" not in json.dumps(job["result"])


def test_review_timeout_is_not_repaired_and_retains_prior_stage_metrics(
    client, project, seeded_chapter,
):
    calls = []

    class Provider(DemoProvider):
        def generate_structured(self, request, schema):
            calls.append(request.task)
            if request.task == "continuity_review":
                raise ProviderExecutionError("unknown", code="PROVIDER_RESULT_UNKNOWN")
            return super().generate_structured(request, schema)

    client.app.state.ai_provider = Provider()
    job = run_job(client, f"/api/v1/projects/{project['id']}/ai/jobs", json={
        "project_id": project["id"], "chapter_id": seeded_chapter, "task_type": "full_chapter",
    }).json()
    assert job["status"] == "recovery_required"
    assert calls.count("continuity_review") == 1
    assert job["replacement_requires_confirmation"]
    stages = job["result"]["execution"]["stages"]
    assert {"plan", "draft", "continuity_review"} <= stages.keys()
    assert stages["continuity_review"]["diagnostic_id"]
    assert stages["continuity_review"]["usage_source"] == "unavailable"
    database = client.app.state.vault_registry.require(project["id"]).database
    with database.job_session_scope() as session:
        attempts = list(session.scalars(select(AIJobStageAttempt).where(
            AIJobStageAttempt.job_id == job["id"],
            AIJobStageAttempt.stage_key == "continuity_review",
        )))
        assert len(attempts) == 1 and attempts[0].status == "result_unknown"


def test_summary_repaired_checkpoint_survives_restart_without_model_resend(queued_job):
    from novel_harness.services import summary_generation
    from novel_harness.services.job_stages import StageRunner

    store, job_id = queued_job
    calls = []

    def invoke(request, observer, schema):
        observer.before_send()
        calls.append(request)
        result = DemoProvider().generate_structured(request, schema).model_dump()
        if len(calls) == 1:
            result["data"]["evidence"][0]["quote"] = "fabricated evidence"
        return result

    result = summary_generation.generate(
        "林舟收起信封。", 12000, StageRunner(store, store.claim(job_id, "first")), invoke,
    )
    assert len(calls) == 2
    store.recover("restarted")
    restored = summary_generation.generate(
        "林舟收起信封。", 12000, StageRunner(store, store.claim(job_id, "restarted")), invoke,
    )
    assert restored["data"] == result["data"]
    assert len(calls) == 2


def test_short_summary_request_explicitly_limits_optional_expansion(queued_job):
    from novel_harness.services import summary_generation
    from novel_harness.services.job_stages import StageRunner

    store, job_id = queued_job
    captured = []

    def invoke(request, observer, schema):
        observer.before_send()
        captured.append(request)
        return DemoProvider().generate_structured(request, schema).model_dump()

    summary_generation.generate(
        "林舟收起信封。", 12000, StageRunner(store, store.claim(job_id, "first")), invoke,
    )
    assert "短章节" in captured[0].developer_instruction
    assert "最多3项" in captured[0].developer_instruction


def test_complete_invalid_json_is_known_repairable_and_retains_usage():
    provider, seen = _provider_response(data={
        "choices": [{"message": {"content": "not json"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3},
    })
    with pytest.raises(ProviderExecutionError) as caught:
        provider.generate_structured(AITextRequest(
            task="review", developer_instruction="", user_prompt="x",
        ), {"type": "object"})
    assert caught.value.code == "INVALID_STRUCTURED_OUTPUT"
    assert caught.value.outcome == "known"
    assert caught.value.metadata["usage"] == {"input_tokens": 5, "output_tokens": 3}
    assert len(seen) == 1
