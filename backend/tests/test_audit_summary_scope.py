from uuid import uuid4

from novel_harness.ai.demo import DemoProvider
from novel_harness.db.models import CanonFact


def test_unrelated_canon_does_not_invalidate_summary(client, project, seeded_chapter):
    base = f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}"
    revision = client.get(base).json()["revision"]
    saved = client.put(
        base, json={"content": "Only this chapter is the input.", "revision": revision}
    )
    database = client.app.state.vault_registry.require(project["id"]).database

    class Concurrent(DemoProvider):
        def generate_structured(self, request, schema):
            with database.job_session_scope() as session:
                session.add(
                    CanonFact(project_id=project["id"], predicate="Elsewhere", value="unrelated")
                )
            return super().generate_structured(request, schema)

    client.app.state.ai_provider = Concurrent()
    receipt = client.post(
        base + "/complete",
        json={"expected_revision": saved.json()["revision"]},
        headers={"Idempotency-Key": uuid4().hex},
    ).json()
    assert client.app.state.job_executor.run_once()
    job = client.get(receipt["status_url"]).json()
    assert job["status"] == "succeeded"
    assert client.get(base + "/summary").json()["status"] == "valid"


def test_summary_requests_always_enforce_input_budget(client, project, seeded_chapter):
    requests = []

    class Recorder(DemoProvider):
        def generate_structured(self, request, schema):
            requests.append(request)
            return super().generate_structured(request, schema)

    client.app.state.ai_provider = Recorder()
    base = f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}"
    revision = client.get(base).json()["revision"]
    saved = client.put(base, json={"content": "A chapter.", "revision": revision})
    receipt = client.post(
        base + "/complete",
        json={"expected_revision": saved.json()["revision"]},
        headers={"Idempotency-Key": uuid4().hex},
    ).json()
    assert client.app.state.job_executor.run_once()
    assert client.get(receipt["status_url"]).json()["status"] == "succeeded"
    assert requests and all(request.token_budget == 12000 for request in requests)


def test_concurrent_confirmed_fact_is_checked_against_verified_observations(
    client, project, seeded_chapter
):
    base = f"/api/v1/projects/{project['id']}"
    chapter_url = base + f"/chapters/{seeded_chapter}"
    source = "林渡明确站在钟楼外。"
    revision = client.get(chapter_url).json()["revision"]
    saved = client.put(chapter_url, json={"content": source, "revision": revision})
    database = client.app.state.vault_registry.require(project["id"]).database

    class ConcurrentConflict(DemoProvider):
        def generate_structured(self, request, schema):
            with database.job_session_scope() as session:
                session.add(
                    CanonFact(
                        project_id=project["id"],
                        predicate="location",
                        value="钟楼内",
                    )
                )
            result = super().generate_structured(request, schema)
            result.data["content_observations"] = [
                {
                    "kind": "fact",
                    "entity_id": None,
                    "predicate": "location",
                    "value": "钟楼外",
                    "evidence_quote": "林渡明确站在钟楼外",
                    "reference_ids": [],
                }
            ]
            return result

    client.app.state.ai_provider = ConcurrentConflict()
    receipt = client.post(
        chapter_url + "/complete",
        json={"expected_revision": saved.json()["revision"]},
        headers={"Idempotency-Key": uuid4().hex},
    ).json()
    assert client.app.state.job_executor.run_once()
    job = client.get(receipt["status_url"]).json()
    assert job["status"] == "recovery_required"
    assert job["recovery_reason"] == "content_review_required"
    assert any(
        item["code"] == "CONFIRMED_FACT_CONFLICT"
        for item in client.get(base + "/conflicts?status=open").json()
    )
