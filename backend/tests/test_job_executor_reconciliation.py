import logging

import pytest

from novel_harness.ai.demo import DemoProvider
from novel_harness.services.job_store import JobStore


def _enqueue(client, project_id, key):
    response = client.post(
        f"/api/v1/projects/{project_id}/ai/jobs",
        json={"project_id": project_id, "task_type": "chat"},
        headers={"Idempotency-Key": key},
    )
    assert response.status_code == 202, response.text
    return response.json()


def test_next_run_reconciles_owned_job_when_exception_coordination_failed(
    client, project, monkeypatch
):
    receipt = _enqueue(client, project["id"], "coordination-failure")
    executor = client.app.state.job_executor
    monkeypatch.setattr(
        client.app.state.model_settings,
        "identity",
        lambda: (_ for _ in ()).throw(RuntimeError("execution failed")),
    )
    original_read = JobStore.read
    reads = 0

    def fail_first_read(self, job_id):
        nonlocal reads
        reads += 1
        if reads == 1:
            raise OSError("coordination unavailable")
        return original_read(self, job_id)

    monkeypatch.setattr(JobStore, "read", fail_first_read)

    with pytest.raises(OSError, match="coordination unavailable"):
        executor.run_once()
    assert client.get(receipt["status_url"]).json()["status"] == "running"

    assert executor.run_once()
    assert client.get(receipt["status_url"]).json()["status"] != "running"


def test_owned_dispatched_attempt_is_paused_without_calling_provider(
    client, project
):
    receipt = _enqueue(client, project["id"], "unknown-owned")
    executor = client.app.state.job_executor
    vault = client.app.state.vault_registry.require(project["id"], job_only=True)
    store = JobStore(vault.database, project["id"])
    fence = store.claim(receipt["id"], executor.epoch)
    number = store.begin_attempt(fence, "chat", {"input": "sent"})
    store.dispatch(fence, "chat", number)

    class CountingProvider(DemoProvider):
        calls = 0

        def generate_text(self, request):
            self.calls += 1
            return super().generate_text(request)

    provider = CountingProvider()
    client.app.state.ai_provider = provider

    assert executor.run_once() is False
    current = client.get(receipt["status_url"]).json()
    assert current["status"] == "recovery_required"
    assert current["recovery_reason"] == "result_unknown"
    assert provider.calls == 0


def test_reconciliation_failure_in_one_vault_does_not_block_another(
    client, project, monkeypatch, caplog
):
    other = client.post("/api/v1/projects", json={"title": "Other"}).json()
    receipt = _enqueue(client, other["id"], "other-vault")
    original = JobStore.reconcile_owned

    def fail_one_vault(self, epoch):
        if self.project_id == project["id"]:
            raise OSError("vault unavailable")
        return original(self, epoch)

    monkeypatch.setattr(JobStore, "reconcile_owned", fail_one_vault)

    with caplog.at_level(logging.WARNING, logger="novel_harness.services.job_executor"):
        assert client.app.state.job_executor.run_once()
    assert client.get(receipt["status_url"]).json()["status"] == "succeeded"
    failure = next(
        record for record in caplog.records if "vault unavailable" in record.getMessage()
    )
    assert project["id"] in failure.getMessage()
    assert failure.exc_info is not None
