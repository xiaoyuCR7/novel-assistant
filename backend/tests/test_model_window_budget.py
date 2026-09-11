"""Large configured model windows keep numeric, idempotent task receipts."""

import pytest

from novel_harness.db.job_models import AIJobControl


@pytest.mark.parametrize("kind", ["chat", "quality"])
def test_model_window_budget_above_old_limit_is_frozen_before_retry(
    client, project, seeded_chapter, kind
):
    limits = {"mode": "demo", "context_capacity": 262144, "output_token_budget": 4096}
    assert client.put("/api/v1/settings/model", json=limits).status_code == 200
    base = f"/api/v1/projects/{project['id']}"
    budget = limits["context_capacity"] - limits["output_token_budget"]
    if kind == "chat":
        url = base + "/ai/jobs"
        command = {
            "project_id": project["id"],
            "task_type": "chat",
            "instructions": "承接之前的讨论。",
            "token_budget": budget,
        }
        report = client.post(url + "/preflight", json=command)
        assert report.status_code == 200, report.text
        assert report.json()["effective_input_limit"] == budget
        assert report.json()["can_fit"] is True
    else:
        url = base + "/quality/runs"
        revision = client.get(base + f"/chapters/{seeded_chapter}").json()["revision"]
        command = {
            "mode": "collaborate",
            "chapters": [{"chapter_id": seeded_chapter, "expected_revision": revision}],
            "token_budget": budget,
        }
    headers = {"Idempotency-Key": "model-window-frozen"}
    first = client.post(url, json=command, headers=headers)
    assert first.status_code == 202, first.text
    job_id = first.json()["id"]
    assert first.json()["token_budget"] == budget
    assert (
        client.put(
            "/api/v1/settings/model",
            json={
                "mode": "demo",
                "context_capacity": 524288,
                "output_token_budget": 8192,
            },
        ).status_code
        == 200
    )
    retry = client.post(url, json=command, headers=headers)
    assert retry.status_code == 202, retry.text
    assert retry.json()["id"] == job_id
    assert retry.json()["token_budget"] == budget
    database = client.app.state.vault_registry.require(project["id"]).database
    with database.job_session_scope() as session:
        control = session.get(AIJobControl, job_id)
        assert control.provider_identity["context_capacity"] == limits["context_capacity"]
        assert control.provider_identity["output_token_budget"] == limits["output_token_budget"]
        assert control.command["token_budget"] == budget


@pytest.mark.parametrize("kind", ["chat", "quality"])
def test_task_budget_does_not_exceed_supported_model_window(client, project, seeded_chapter, kind):
    base = f"/api/v1/projects/{project['id']}"
    command = {"project_id": project["id"], "task_type": "chat", "token_budget": 1048577}
    url = base + "/ai/jobs"
    if kind == "quality":
        url = base + "/quality/runs"
        command = {
            "mode": "collaborate",
            "token_budget": 1048577,
            "chapters": [{"chapter_id": seeded_chapter, "expected_revision": 1}],
        }
    result = client.post(url, json=command, headers={"Idempotency-Key": "too-large-window"})
    assert result.status_code == 422
