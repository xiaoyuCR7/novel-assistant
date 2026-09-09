import pytest
from job_helpers import run_job, save_chapter

from novel_harness.ai.base import StructuredResult
from novel_harness.ai.demo import DemoProvider


def run(client, project, chapter, task):
    return run_job(
        client,
        f"/api/v1/projects/{project['id']}/ai/jobs",
        json={"project_id": project["id"], "chapter_id": chapter, "task_type": task},
    ).json()


@pytest.mark.parametrize("task", ["plan", "review", "suggest"])
def test_invalid_structured_stage_fails_closed(client, project, seeded_chapter, task):
    class Invalid(DemoProvider):
        def generate_structured(self, request, schema):
            return StructuredResult(data={"unexpected": "value"}, provider="fake", model="fake")

    client.app.state.ai_provider = Invalid()
    assert run(client, project, seeded_chapter, task)["status"] == "failed"


def test_review_persists_evidence_and_overdue_foreshadowing(client, project, seeded_chapter):
    base = f"/api/v1/projects/{project['id']}"
    later = client.post(
        base + "/nodes", json={"kind": "chapter", "title": "Later", "order_index": 10}
    ).json()["id"]
    client.post(
        base + "/plots",
        json={"kind": "foreshadowing", "title": "Unpaid", "due_node_id": seeded_chapter},
    )
    save_chapter(
        client, base + f"/chapters/{later}", json={"content": "The dead character opens the door."}
    )

    class Reviewer(DemoProvider):
        def generate_structured(self, request, schema):
            return StructuredResult(
                data={
                    "issues": [
                        {
                            "code": "CHARACTER_STATE",
                            "severity": "severe",
                            "message": "Dead character acts",
                            "evidence": ["The dead character opens the door."],
                        }
                    ]
                },
                provider="fake",
                model="fake",
            )

    client.app.state.ai_provider = Reviewer()
    assert run(client, project, later, "review")["status"] == "succeeded"
    conflicts = client.get(base + "/conflicts").json()
    assert {"CHARACTER_STATE", "FORESHADOWING_OVERDUE"} <= {c["code"] for c in conflicts}
    assert all(len(c["options"]) == 3 for c in conflicts)
    assert all(c["code"] in c["options"][0]["description"] for c in conflicts)


def test_unexpected_error_does_not_leave_running_job_or_echo_secrets(
    client, project, seeded_chapter
):
    class Broken(DemoProvider):
        def generate_text(self, request):
            raise RuntimeError("secret-should-not-be-disclosed")

    client.app.state.ai_provider = Broken()
    job = run(client, project, seeded_chapter, "chat")
    assert job["status"] == "recovery_required"
    assert job["recovery_reason"] == "result_unknown"
    assert "secret-should-not" not in str(job)


def test_text_jobs_reject_image_instead_of_running_review(client, project, seeded_chapter):
    response = run_job(
        client,
        f"/api/v1/projects/{project['id']}/ai/jobs",
        json={"project_id": project["id"], "chapter_id": seeded_chapter, "task_type": "image"},
    )
    assert response.status_code == 422


def test_drift_recurrence_creates_new_open_warning(client, project, seeded_chapter):
    base = f"/api/v1/projects/{project['id']}"
    url = base + f"/chapters/{seeded_chapter}"

    def write(content):
        save_chapter(
            client,
            url,
            json={"content": content, "contract": {"forbidden_revelations": ["SECRET"]}},
        )

    write("SECRET")
    first = client.post(url + "/drift-check").json()[0]
    client.post(base + f"/conflicts/{first['id']}/decision", json={"decision": "dismiss"})
    write("safe")
    client.post(url + "/drift-check")
    write("SECRET again")
    latest = client.post(url + "/drift-check").json()
    assert any(c["status"] == "open" and c["id"] != first["id"] for c in latest)


def test_empty_reviews_skip_only_unnecessary_resolver(client, project, seeded_chapter):
    calls = []

    class Recorder(DemoProvider):
        def generate_structured(self, request, schema):
            calls.append(request.task)
            return super().generate_structured(request, schema)

    client.app.state.ai_provider = Recorder()
    result = run(client, project, seeded_chapter, "full_chapter")
    assert result["status"] == "succeeded"
    assert "resolve" not in calls
    assert result["result"]["candidate_text"]


def test_invalid_observation_rolls_back_all_review_warnings(client, project, seeded_chapter):
    base = f"/api/v1/projects/{project['id']}"
    save_chapter(client, base + f"/chapters/{seeded_chapter}", json={"content": "verifiable text"})

    class Reviewer(DemoProvider):
        def generate_structured(self, request, schema):
            return StructuredResult(
                data={
                    "issues": [
                        {
                            "code": "TEST",
                            "severity": "warning",
                            "message": "issue",
                            "evidence": ["verifiable text"],
                        }
                    ],
                    "observations": [
                        {"kind": "pov", "entity_id": "missing", "evidence": "invented quote"}
                    ],
                },
                provider="fake",
                model="fake",
            )

    client.app.state.ai_provider = Reviewer()
    assert run(client, project, seeded_chapter, "review")["status"] == "failed"
    assert client.get(base + "/conflicts").json() == []


def test_continuation_checks_complete_candidate_without_extra_model_calls(
    client, project, seeded_chapter
):
    base = f"/api/v1/projects/{project['id']}"
    save_chapter(
        client,
        base + f"/chapters/{seeded_chapter}",
        json={"content": "OLD_PHRASE", "contract": {"forbidden_phrases": ["OLD_PHRASE"]}},
    )
    assert run(client, project, seeded_chapter, "continue")["status"] == "succeeded"
    assert any(c["code"] == "FORBIDDEN_PHRASE" for c in client.get(base + "/conflicts").json())
