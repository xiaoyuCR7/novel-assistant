from sqlalchemy import func, select

from novel_harness.db.models import ProjectPreparation


def _url(project, suffix=""):
    return f"/api/v1/projects/{project['id']}/preparation{suffix}"


def test_get_is_read_only_and_initialize_is_idempotent(client, project) -> None:
    database = client.app.state.vault_registry.require(project["id"]).database
    first = client.get(_url(project))
    assert first.status_code == 200
    assert first.json()["status"] == "not_started"
    with database.session_scope() as session:
        assert session.scalar(select(func.count(ProjectPreparation.project_id))) == 0

    one = client.post(_url(project, "/initialize"))
    two = client.post(_url(project, "/initialize"))
    assert one.status_code == 200
    assert one.json()["questions"] == two.json()["questions"]
    with database.session_scope() as session:
        assert session.scalar(select(func.count(ProjectPreparation.project_id))) == 1


def test_answer_skip_resume_and_revision_conflict(client, project) -> None:
    state = client.post(_url(project, "/initialize")).json()
    question = state["questions"][0]
    answered = client.patch(
        _url(project, f"/questions/{question['id']}"),
        json={
            "revision": state["revision"],
            "action": "answer",
            "answer": "绝不让亡者的信件被活人伪造。",
        },
    )
    assert answered.status_code == 200
    current = answered.json()
    assert current["answered_count"] == 1

    stale = client.post(_url(project, "/skip"), json={"revision": state["revision"]})
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "revision_conflict"

    skipped = client.post(_url(project, "/skip"), json={"revision": current["revision"]})
    assert skipped.json()["status"] == "skipped"
    resumed = client.post(_url(project, "/resume"), json={"revision": skipped.json()["revision"]})
    assert resumed.status_code == 200, resumed.json()
    assert resumed.json()["status"] == "in_progress"


def test_answer_endpoint_rejects_text_and_list_shape_mismatches(client, project) -> None:
    state = client.post(_url(project, "/initialize")).json()
    text_question = next(
        question for question in state["questions"] if question["answer_format"] == "short_text"
    )
    list_question = next(
        question
        for question in state["questions"]
        if question["answer_format"] == "ordered_list"
    )

    list_for_text = client.patch(
        _url(project, f"/questions/{text_question['id']}"),
        json={"revision": state["revision"], "action": "answer", "answer": ["错误类型"]},
    )
    text_for_list = client.patch(
        _url(project, f"/questions/{list_question['id']}"),
        json={"revision": state["revision"], "action": "answer", "answer": "错误类型"},
    )
    assert list_for_text.status_code == 422
    assert list_for_text.json()["detail"]["code"] == "INVALID_PREPARATION_ANSWER_FORMAT"
    assert text_for_list.status_code == 422
    assert text_for_list.json()["detail"]["code"] == "INVALID_PREPARATION_ANSWER_FORMAT"


def test_ai_analysis_requires_idempotency_key_and_is_explicit(client, project) -> None:
    client.post(_url(project, "/initialize"))
    missing = client.post(_url(project, "/analyze"), json={})
    assert missing.status_code == 422

    response = client.post(
        _url(project, "/analyze"),
        json={},
        headers={"Idempotency-Key": "prep-analysis-1"},
    )
    assert response.status_code == 202
    assert response.json()["task_type"] == "preparation_analysis"
    replay = client.post(
        _url(project, "/analyze"),
        json={},
        headers={"Idempotency-Key": "prep-analysis-1"},
    )
    assert replay.status_code == 202
    assert replay.json()["id"] == response.json()["id"]

    assert client.app.state.job_executor.run_once() is True
    completed = client.get(_url(project)).json()
    assert len(completed["questions"]) <= 8
    assert any(question["origin"] == "ai" for question in completed["questions"])
