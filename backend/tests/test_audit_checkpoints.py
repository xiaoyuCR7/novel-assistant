import json
from uuid import uuid4

import pytest
from sqlalchemy import select

from novel_harness.ai.demo import DemoProvider
from novel_harness.db.job_models import AIJobStageAttempt


@pytest.mark.parametrize(
    "invalid", ["quote", "entity", "observation", "boundary", "unreferenced_entity"]
)
def test_invalid_review_fails_before_dependents_and_can_resume(
    client,
    project,
    seeded_chapter,
    invalid,
    monkeypatch,
):
    calls = []
    broken = True
    hidden_id = None
    if invalid == "unreferenced_entity":
        hidden_id = client.post(
            f"/api/v1/projects/{project['id']}/library/entity", json={"title": "未引用角色"}
        ).json()["id"]
        monkeypatch.setattr(
            "novel_harness.services.writing_context.search", lambda *args, **kwargs: {"items": []}
        )

    class Reviewed(DemoProvider):
        def generate_text(self, request):
            calls.append(request.task)
            return super().generate_text(request)

        def generate_structured(self, request, schema):
            calls.append(request.task)
            result = super().generate_structured(request, schema)
            if request.task == "style_review" and broken:
                result.data["issues"] = [
                    {
                        "code": "bad",
                        "severity": "warning",
                        "message": "review",
                        "evidence": [
                            "ABSENT_QUOTE" if invalid == "quote" else request.user_prompt[:8]
                        ],
                        "related_entity_ids": ["foreign-entity"] if invalid == "entity" else [],
                    }
                ]
                if hidden_id:
                    result.data["issues"][0]["related_entity_ids"] = [hidden_id]
                if invalid == "boundary":
                    fragment = request.context["context_packet"]["fragments"][-1]["content"]
                    result.data["issues"][0]["evidence"] = [
                        fragment[-8:]
                        + "\n"
                        + json.dumps(request.context["contract"], ensure_ascii=False),
                    ]
                if invalid == "observation":
                    result.data["observations"] = [
                        {
                            "kind": "fact",
                            "evidence": "ABSENT_OBSERVATION",
                            "entity_id": None,
                            "predicate": "knows",
                            "value": "secret",
                        }
                    ]
            return result

    client.app.state.ai_provider = Reviewed()
    base = f"/api/v1/projects/{project['id']}"
    revision = client.get(base + "/chapters/" + seeded_chapter).json()["revision"]
    receipt = client.post(
        base + "/ai/jobs",
        json={
            "project_id": project["id"],
            "chapter_id": seeded_chapter,
            "expected_revision": revision,
            "task_type": "full_chapter",
        },
        headers={"Idempotency-Key": uuid4().hex},
    ).json()
    assert client.app.state.job_executor.run_once()
    job = client.get(receipt["status_url"]).json()
    assert job["status"] == "failed"
    assert calls == ["plan", "draft", "continuity_review", "style_review", "style_review"]
    assert "cancel" in job["allowed_actions"]
    database = client.app.state.vault_registry.require(project["id"]).database
    with database.job_session_scope() as session:
        attempt = session.scalar(
            select(AIJobStageAttempt).where(
                AIJobStageAttempt.job_id == job["id"],
                AIJobStageAttempt.stage_key == "style_review.repair",
            )
        )
        assert attempt.status == "failed"
        assert attempt.artifact_id is None
    broken = False
    resumed = client.post(
        receipt["status_url"] + "/resume",
        json={
            "expected_control_revision": job["control_revision"],
            "confirm_unknown": False,
        },
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert resumed.status_code == 202
    assert client.app.state.job_executor.run_once()
    assert client.get(receipt["status_url"]).json()["status"] == "succeeded"
    assert calls.count("plan") == calls.count("draft") == calls.count("continuity_review") == 1
    assert calls.count("style_review") == 3
