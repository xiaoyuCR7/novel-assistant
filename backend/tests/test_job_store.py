from concurrent.futures import ThreadPoolExecutor
from importlib import import_module
from threading import Barrier

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from novel_harness.db.models import AIJob


@pytest.fixture
def store_command(client, project):
    vault = client.app.state.vault_registry.require(project["id"])
    store = import_module("novel_harness.services.job_store").JobStore(
        vault.database, project["id"]
    )
    command = dict(
        project_id=project["id"],
        chapter_id=None,
        task_type="chat",
        instructions="讨论开场",
        token_budget=12000,
        expected_revision=None,
    )
    return store, command


def prepare(session):
    return dict(source_snapshot={}, provider_identity={"mode": "demo"}, embedding_identity=None)


def test_replay_precedes_validation_and_commits_before_response(store_command):
    store, command = store_command
    first = store.enqueue(command, "request-1", prepare)
    with store.database.job_session_scope() as session:
        assert session.scalar(select(func.count()).select_from(AIJob)) == 1

    def must_not_prepare(session):
        raise AssertionError("replay must not prepare")

    second = store.enqueue(command, "request-1", must_not_prepare)
    assert first["id"] == second["id"]
    assert store.read(first["id"])["status"] == "queued"
    with pytest.raises(HTTPException) as error:
        store.enqueue({**command, "instructions": "不同"}, "request-1", prepare)
    assert error.value.status_code == 409


def test_concurrent_replay_has_one_job(store_command):
    store, command = store_command
    barrier = Barrier(2)

    def submit(_):
        barrier.wait(timeout=5)
        return store.enqueue(command, "concurrent", prepare)["id"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert len(set(pool.map(submit, range(2)))) == 1
    with store.database.job_session_scope() as session:
        assert session.scalar(select(func.count()).select_from(AIJob)) == 1


def test_prepare_failure_rolls_back(store_command):
    store, command = store_command

    def fail(session):
        session.add(
            AIJob(
                project_id=store.project_id,
                task_type="chat",
                prompt_version="test",
                status="queued",
            )
        )
        session.flush()
        raise ValueError("preparation failed")

    with pytest.raises(ValueError):
        store.enqueue(command, "rollback", fail)
    with store.database.job_session_scope() as session:
        assert session.scalar(select(func.count()).select_from(AIJob)) == 0


def test_summary_and_writing_jobs_have_independent_prompt_versions(store_command):
    store, command = store_command
    writing = store.enqueue(command, "writing-version", prepare)
    summary = store.enqueue(
        {
            **command,
            "task_type": "chapter_summary",
            "instructions": "生成章节总结并检查正文",
        },
        "summary-version",
        prepare,
    )

    assert writing["prompt_version"] != summary["prompt_version"]


@pytest.mark.parametrize("key", ["", "中文", "a b", "x" * 129])
def test_invalid_keys_rejected(store_command, key):
    store, command = store_command
    with pytest.raises(HTTPException) as error:
        store.enqueue(command, key, prepare)
    assert error.value.status_code == 422


def test_vault_isolation(client, store_command):
    store, command = store_command
    other = client.post("/api/v1/projects", json={"title": "独立小说"}).json()
    database = client.app.state.vault_registry.require(other["id"]).database
    other_store = type(store)(database, other["id"])
    first = store.enqueue(command, "same", prepare)
    second = other_store.enqueue({**command, "project_id": other["id"]}, "same", prepare)
    assert first["id"] != second["id"]
    with pytest.raises(HTTPException) as error:
        other_store.read(first["id"])
    assert error.value.status_code == 404
