from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from novel_harness.main import create_app


@pytest.fixture
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("NOVEL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NOVEL_AI_PROVIDER", "demo")
    with TestClient(create_app(start_executor=False)) as test_client:
        yield test_client


@pytest.fixture
def project(client: TestClient) -> dict:
    response = client.post(
        "/api/v1/projects",
        json={
            "title": "雾城来信",
            "premise": "失忆的邮差替死者送出最后一批信。",
            "genre": "奇幻悬疑",
            "target_words": 300000,
            "daily_goal": 1800,
        },
    )
    assert response.status_code == 201
    return response.json()


@pytest.fixture
def seeded_chapter(client: TestClient, project: dict) -> str:
    volume = client.post(
        f"/api/v1/projects/{project['id']}/nodes",
        json={"kind": "volume", "title": "第一卷", "order_index": 1},
    ).json()
    chapter = client.post(
        f"/api/v1/projects/{project['id']}/nodes",
        json={
            "kind": "chapter",
            "parent_id": volume["id"],
            "title": "第一章",
            "order_index": 1,
            "target_words": 3000,
        },
    ).json()
    return chapter["id"]


@pytest.fixture
def queued_job(client, project):
    from novel_harness.services.job_store import JobStore

    vault = client.app.state.vault_registry.require(project['id'])
    store = JobStore(vault.database, project['id'])
    command = dict(project_id=project['id'], chapter_id=None, task_type='chat',
                   instructions='讨论开场', token_budget=12000, expected_revision=None)
    job = store.enqueue(command, 'fixture-request', lambda session: {
        'source_snapshot': {}, 'provider_identity': {'mode': 'demo'},
        'embedding_identity': None,
    })
    return store, job['id']
