"""Regression probes for quality checkpoint replay and interrupted result projection."""

from copy import deepcopy

import pytest

from novel_harness.ai.demo import DemoProvider
from novel_harness.services.job_store import JobStore


@pytest.mark.parametrize("interrupt_at", range(1, 8))
def test_cancel_during_resume_keeps_completed_candidates_and_reports(
    client, project, seeded_chapter, monkeypatch, interrupt_at
):
    base = f"/api/v1/projects/{project['id']}"
    later = client.post(
        base + "/nodes", json={"kind": "chapter", "title": "第二章", "order_index": 10}
    ).json()["id"]

    class PausingSecondChapter(DemoProvider):
        def __init__(self):
            self.calls = []

        def generate_text(self, request):
            self.calls.append((request.task, request.context.get("chapter_id")))
            return super().generate_text(request)

        def generate_structured(self, request, schema):
            self.calls.append((request.task, request.context.get("chapter_id")))
            result = super().generate_structured(request, schema)
            if request.context.get("chapter_id") == later:
                result.data["scores"] = dict.fromkeys(result.data["scores"], 60)
            return result

    provider = PausingSecondChapter()
    client.app.state.ai_provider = provider
    response = client.post(
        base + "/quality/runs",
        headers={"Idempotency-Key": "replay-then-cancel"},
        json={
            "mode": "collaborate",
            "chapters": [
                {
                    "chapter_id": cid,
                    "expected_revision": client.get(base + f"/chapters/{cid}").json()["revision"],
                }
                for cid in [seeded_chapter, later]
            ],
        },
    )
    assert response.status_code == 202, response.text
    job_id = response.json()["id"]
    url = base + f"/quality/runs/{job_id}"
    assert client.app.state.job_executor.run_once()
    paused = client.get(url).json()
    assert paused["status"] == "recovery_required"
    assert [chapter["status"] for chapter in paused["result"]["chapters"]] == [
        "ready",
        "needs_review",
    ]
    saved = deepcopy(paused["result"])
    completed_calls = provider.calls[:]
    assert len(completed_calls) == 6
    approval = client.post(
        url + "/approve",
        json={
            "chapter_id": later,
            "confirmed": True,
            "expected_control_revision": paused["control_revision"],
        },
    )
    assert approval.status_code == 200, approval.text
    resumed = client.post(
        base + f"/ai/jobs/{job_id}/resume",
        headers={"Idempotency-Key": "resume-once"},
        json={
            "expected_control_revision": approval.json()["control_revision"],
        },
    )
    assert resumed.status_code == 202, resumed.text

    publish = JobStore.publish
    cancelled = False
    publish_count = 0

    def cancel_during_replay(store, fence, apply, *, terminal=True):
        nonlocal cancelled, publish_count
        publish(store, fence, apply, terminal=terminal)
        if fence.job_id != job_id:
            return
        publish_count += 1
        if publish_count == interrupt_at:
            cancelled = True
            store.cancel(job_id)

    monkeypatch.setattr(JobStore, "publish", cancel_during_replay)
    assert client.app.state.job_executor.run_once()
    interrupted = client.get(url).json()
    assert cancelled
    assert interrupted["status"] == "cancelled"
    assert interrupted["result"]["chapters"] == saved["chapters"]
    assert interrupted["result"]["messages"] == saved["messages"]
    assert interrupted["result"]["completed_chapters"] == 1
    assert provider.calls == completed_calls
    accepted = client.post(url + "/accept", json={"chapter_id": seeded_chapter})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["content"] == saved["chapters"][0]["candidate_text"]
