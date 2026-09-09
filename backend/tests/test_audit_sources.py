from uuid import uuid4

from novel_harness.db.models import Entity
from novel_harness.services import writing_context


def test_reference_edited_between_retrieval_and_freeze_cannot_publish(
    client, project, seeded_chapter, monkeypatch,
):
    base = f"/api/v1/projects/{project['id']}"
    entity = client.post(base + '/entities', json={
        'kind': 'character', 'name': 'NightCourier', 'summary': 'OLD_REFERENCE',
    }).json()
    database = client.app.state.vault_registry.require(project['id']).database
    original = writing_context.search

    def concurrent_edit(*args, **kwargs):
        result = original(*args, **kwargs)
        with database.job_session_scope() as other:
            record = other.get(Entity, entity['id'])
            record.summary = 'NEW_REFERENCE'
            record.revision += 1
        return result

    monkeypatch.setattr(writing_context, 'search', concurrent_edit)
    revision = client.get(base + '/chapters/' + seeded_chapter).json()['revision']
    receipt = client.post(base + '/ai/jobs', json={
        'project_id': project['id'], 'chapter_id': seeded_chapter,
        'expected_revision': revision, 'task_type': 'chat', 'instructions': 'NightCourier',
    }, headers={'Idempotency-Key': uuid4().hex})
    assert receipt.status_code == 202
    assert client.app.state.job_executor.run_once()
    job = client.get(receipt.json()['status_url']).json()
    assert job['status'] == 'recovery_required'
    assert job['recovery_reason'] == 'SOURCE_CHANGED'
    assert job['result'] == {}


def test_explicit_reference_edited_after_resolution_cannot_publish(
    client, project, seeded_chapter, monkeypatch,
):
    from novel_harness.services import references

    base = f"/api/v1/projects/{project['id']}"
    entity = client.post(base + '/entities', json={
        'kind': 'character', 'name': 'Explicit', 'summary': 'OLD_REFERENCE',
    }).json()
    database = client.app.state.vault_registry.require(project['id']).database
    original = references.resolve_references

    def concurrent_edit(*args, **kwargs):
        result = original(*args, **kwargs)
        if result:
            with database.job_session_scope() as other:
                record = other.get(Entity, entity['id'])
                record.summary = 'NEW_REFERENCE'
                record.revision += 1
        return result

    monkeypatch.setattr(references, 'resolve_references', concurrent_edit)
    revision = client.get(base + '/chapters/' + seeded_chapter).json()['revision']
    receipt = client.post(base + '/ai/jobs', json={
        'project_id': project['id'], 'chapter_id': seeded_chapter,
        'expected_revision': revision, 'task_type': 'chat',
        'instructions': f"[[ref:entity:{entity['id']}]]",
    }, headers={'Idempotency-Key': uuid4().hex})
    assert receipt.status_code == 202
    assert client.app.state.job_executor.run_once()
    job = client.get(receipt.json()['status_url']).json()
    assert job['status'] == 'recovery_required'
    assert job['recovery_reason'] == 'SOURCE_CHANGED'
