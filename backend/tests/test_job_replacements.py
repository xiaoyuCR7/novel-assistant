"""Explicit linked replacements keep old commands and author decisions intact."""

import pytest
from sqlalchemy import event, func, select

from novel_harness.db.job_models import AIJobControl
from novel_harness.db.models import AIJob
from novel_harness.services.job_state import command_hash
from novel_harness.services.job_store import JobStore


def setup_job(client, project, chapter_id=None, task_type="chat"):
    base = f"/api/v1/projects/{project['id']}"
    command = dict(
        project_id=project["id"],
        chapter_id=chapter_id,
        task_type=task_type,
        instructions="完整原始指令" * 500,
        token_budget=8192,
        expected_revision=None,
    )
    if chapter_id:
        command["expected_revision"] = client.get(base + "/chapters/" + chapter_id).json()[
            "revision"
        ]
    response = client.post(base + "/ai/jobs", json=command, headers={"Idempotency-Key": "original"})
    assert response.status_code == 202, response.text
    store = JobStore(client.app.state.vault_registry.require(project["id"]).database, project["id"])
    return base, store, command, response.json()


def set_state(store, job_id, status, reason=None, effects=None):
    with store.write() as session:
        session.get(AIJob, job_id).status = status
        control = session.get(AIJobControl, job_id)
        control.recovery_reason = reason
        if effects is not None:
            control.effects = effects


def replace(client, base, command, source, **extra):
    return client.post(
        base + "/ai/jobs",
        json={**command, "replaces_job_id": source, **extra},
        headers={"Idempotency-Key": "replacement"},
    )


@pytest.mark.parametrize("status", ["failed", "recovery_required", "cancelled"])
def test_replacement_uses_current_settings_retains_source_and_links_lightweight_page(
    client,
    project,
    seeded_chapter,
    status,
):
    base, store, command, original = setup_job(client, project, seeded_chapter, "rewrite")
    set_state(store, original["id"], status)
    before = store.read(original["id"])
    assert (
        client.put(
            "/api/v1/settings/model", json={"mode": "demo", "output_token_budget": 8192}
        ).status_code
        == 200
    )
    if status != "cancelled":
        resumed = client.post(
            base + f"/ai/jobs/{original['id']}/resume",
            json={"expected_control_revision": before["control_revision"]},
            headers={"Idempotency-Key": "resume"},
        )
        assert resumed.status_code == 409
        assert resumed.json()["detail"]["code"] == "PROVIDER_CHANGED"
    chapter_url = base + "/chapters/" + seeded_chapter
    document = client.get(chapter_url).json()
    saved = client.put(
        chapter_url,
        json={"content": "当前已保存正文", "contract": {}, "revision": document["revision"]},
    )
    assert saved.status_code == 200
    command["expected_revision"] = saved.json()["revision"]
    result = replace(client, base, command, original["id"])
    assert result.status_code == 202, result.text
    new = result.json()
    assert new["id"] != original["id"]
    assert new["replaces_job_id"] == original["id"]
    assert store.read(original["id"]) == before
    with store.database.job_session_scope() as session:
        control = session.get(AIJobControl, new["id"])
        assert control.command["replaces_job_id"] == original["id"]
        assert control.command["instructions"] == command["instructions"]
        assert control.provider_identity["output_token_budget"] == 8192
        assert control.source_snapshot["revision"] == saved.json()["revision"]
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(store.database.engine, "before_cursor_execute", record)
    try:
        page = client.get(base + f"/ai/jobs/page?chapter_id={seeded_chapter}").json()
    finally:
        event.remove(store.database.engine, "before_cursor_execute", record)
    item = next(row for row in page["items"] if row["id"] == new["id"])
    assert item["replaces_job_id"] == original["id"]
    assert "command" not in item and "context_snapshot" not in item and "result" not in item
    assert any("json_extract(ai_job_controls.command" in sql for sql in statements)
    assert not any(
        "SELECT ai_job_controls.command" in sql or ", ai_job_controls.command" in sql
        for sql in statements
    )
    # A lost receipt replays even if the original is no longer replaceable.
    set_state(store, original["id"], "succeeded")
    assert replace(client, base, command, original["id"]).json()["id"] == new["id"]


@pytest.mark.parametrize("status", ["queued", "running", "cancel_requested", "succeeded"])
def test_replacement_rejects_ineligible_source_states(client, project, status):
    base, store, command, original = setup_job(client, project)
    set_state(store, original["id"], status)
    response = replace(client, base, command, original["id"])
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "JOB_NOT_REPLACEABLE"


@pytest.mark.parametrize("scope", ["project", "chapter", "type"])
def test_replacement_rejects_cross_scope_source(client, project, seeded_chapter, scope):
    base, store, command, original = setup_job(client, project, seeded_chapter)
    set_state(store, original["id"], "failed")
    if scope == "project":
        other = client.post("/api/v1/projects", json={"title": "另一部小说"}).json()
        base = f"/api/v1/projects/{other['id']}"
        command.update(project_id=other["id"], chapter_id=None, expected_revision=None)
    elif scope == "chapter":
        command.update(chapter_id=None, expected_revision=None)
    else:
        command["task_type"] = "rewrite"
    response = replace(client, base, command, original["id"])
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "JOB_NOT_FOUND"


@pytest.mark.parametrize("cancelled", [False, True])
def test_unknown_replacement_requires_backend_confirmation_even_after_cancel(
    client, project, cancelled
):
    base, store, command, original = setup_job(client, project)
    set_state(store, original["id"], "recovery_required", "result_unknown")
    if cancelled:
        assert client.post(base + f"/ai/jobs/{original['id']}/cancel").status_code == 200
    before = store.read(original["id"])
    response = replace(client, base, command, original["id"])
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "UNKNOWN_RESULT_CONFIRMATION_REQUIRED"
    with store.database.job_session_scope() as session:
        assert session.scalar(select(func.count()).select_from(AIJob)) == 1
    response = replace(client, base, command, original["id"], confirm_unknown=True)
    assert response.status_code == 202
    assert store.read(original["id"]) == before


def test_unlinked_optional_fields_do_not_change_legacy_command_digest(client, project):
    base, store, command, original = setup_job(client, project)
    with store.database.job_session_scope() as session:
        control = session.get(AIJobControl, original["id"])
        assert control.command == command
        assert control.request_hash == command_hash(command)
    response = client.post(
        base + "/ai/jobs",
        json={**command, "replaces_job_id": None, "confirm_unknown": False},
        headers={"Idempotency-Key": "original"},
    )
    assert response.status_code == 202
    assert response.json()["id"] == original["id"]


@pytest.mark.parametrize("source", ["", "x" * 65])
def test_replacement_identifier_is_bounded(client, project, source):
    base, _, command, _ = setup_job(client, project)
    assert replace(client, base, command, source).status_code == 422


def setup_summary(client, project, chapter_id):
    base = f"/api/v1/projects/{project['id']}"
    chapter_url = base + "/chapters/" + chapter_id
    document = client.get(chapter_url).json()
    saved = client.put(
        chapter_url,
        json={"content": "总结的当前正文", "contract": {}, "revision": document["revision"]},
    ).json()
    payload = {"expected_revision": saved["revision"]}
    original = client.post(
        chapter_url + "/complete", json=payload, headers={"Idempotency-Key": "original"}
    )
    assert original.status_code == 202, original.text
    store = JobStore(client.app.state.vault_registry.require(project["id"]).database, project["id"])
    return chapter_url, store, payload, original.json()


@pytest.mark.parametrize("status", ["failed", "recovery_required"])
def test_summary_replacement_requires_explicit_cancel_and_retains_link(
    client, project, seeded_chapter, status
):
    url, store, payload, original = setup_summary(client, project, seeded_chapter)
    payload = {"expected_revision": client.get(url).json()["revision"]}
    set_state(store, original["id"], status, "result_unknown")
    replacement = {**payload, "replaces_job_id": original["id"], "confirm_unknown": True}
    response = client.post(
        url + "/complete", json=replacement, headers={"Idempotency-Key": "new-summary"}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "SUMMARY_REPLACEMENT_CANCEL_REQUIRED"
    assert store.read(original["id"])["status"] == status
    store.cancel(original["id"])
    before = store.read(original["id"])
    response = client.post(
        url + "/complete", json=replacement, headers={"Idempotency-Key": "new-summary"}
    )
    assert response.status_code == 202, response.text
    assert response.json()["replaces_job_id"] == original["id"]
    assert store.read(original["id"]) == before
    assert (
        client.post(
            url + "/complete", json=replacement, headers={"Idempotency-Key": "new-summary"}
        ).json()["id"]
        == response.json()["id"]
    )


def test_published_summary_is_repair_only_and_active_summary_blocks_replacement(
    client, project, seeded_chapter
):
    url, store, payload, original = setup_summary(client, project, seeded_chapter)
    payload = {"expected_revision": client.get(url).json()["revision"]}
    set_state(
        store,
        original["id"],
        "cancelled",
        effects={"summary_id": "published", "ledger_pending": True},
    )
    replacement = {**payload, "replaces_job_id": original["id"]}
    response = client.post(
        url + "/complete", json=replacement, headers={"Idempotency-Key": "new-summary"}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "SUMMARY_ALREADY_PUBLISHED"
    set_state(store, original["id"], "cancelled", effects={})
    other = client.post(
        url + "/complete", json=payload, headers={"Idempotency-Key": "other-summary"}
    )
    assert other.status_code == 202
    response = client.post(
        url + "/complete", json=replacement, headers={"Idempotency-Key": "new-summary"}
    )
    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "SUMMARY_TASK_ACTIVE",
        "job_id": other.json()["id"],
    }


def test_summary_unlinked_optional_fields_preserve_legacy_digest(client, project, seeded_chapter):
    url, store, payload, original = setup_summary(client, project, seeded_chapter)
    with store.database.job_session_scope() as session:
        command = session.get(AIJobControl, original["id"]).command
        assert "replaces_job_id" not in command and "confirm_unknown" not in command
    response = client.post(
        url + "/complete",
        json={**payload, "replaces_job_id": None},
        headers={"Idempotency-Key": "original"},
    )
    assert response.status_code == 202
    assert response.json()["id"] == original["id"]


def test_legacy_without_control_requires_unknown_confirmation_for_linked_new_job(client, project):
    base, store, command, original = setup_job(client, project)
    with store.write() as session:
        session.delete(session.get(AIJobControl, original["id"]))
        session.get(AIJob, original["id"]).status = "recovery_required"
    response = replace(client, base, command, original["id"])
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "UNKNOWN_RESULT_CONFIRMATION_REQUIRED"
    legacy = store.read(original["id"])
    assert "replace" in legacy["allowed_actions"]
    assert legacy["replacement_requires_confirmation"] is True
    response = replace(client, base, command, original["id"], confirm_unknown=True)
    assert response.status_code == 202
    assert response.json()["replaces_job_id"] == original["id"]
    assert store.read(original["id"]) == legacy


def test_cancelled_dispatch_still_requires_unknown_confirmation(client, project):
    base, store, command, original = setup_job(client, project)
    fence = store.claim(original["id"], "worker")
    attempt = store.begin_attempt(fence, "chat", {})
    store.dispatch(fence, "chat", attempt)
    store.cancel(original["id"])
    store.finish_cancellation(original["id"])
    response = replace(client, base, command, original["id"])
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "UNKNOWN_RESULT_CONFIRMATION_REQUIRED"
    assert store.read(original["id"])["replacement_requires_confirmation"] is True


def test_legacy_summary_can_be_explicitly_cancelled_before_linked_replacement(
    client, project, seeded_chapter
):
    url, store, _, original = setup_summary(client, project, seeded_chapter)
    with store.write() as session:
        session.delete(session.get(AIJobControl, original["id"]))
        session.get(AIJob, original["id"]).status = "recovery_required"
    assert "cancel" in store.read(original["id"])["allowed_actions"]
    assert store.cancel(original["id"])["status"] == "cancelled"
    response = client.post(
        url + "/complete",
        headers={"Idempotency-Key": "replacement"},
        json={
            "expected_revision": client.get(url).json()["revision"],
            "replaces_job_id": original["id"],
            "confirm_unknown": True,
        },
    )
    assert response.status_code == 202
    assert response.json()["replaces_job_id"] == original["id"]
