from sqlalchemy import event

from novel_harness.db.models import AIJob


def test_job_page_is_bounded_and_does_not_load_snapshots(client, project):
    database = client.app.state.vault_registry.require(project["id"]).database
    with database.job_session_scope() as session:
        for i in range(100):
            session.add(
                AIJob(
                    project_id=project["id"],
                    task_type="chat",
                    status="succeeded",
                    prompt_version="legacy",
                    instructions=f"turn{i}",
                    context_snapshot={"secret_reference": "x" * 20000},
                    result={"reply": "r" * 3000},
                )
            )
    statements = []

    def record(conn, cursor, statement, params, context, many):
        statements.append(statement)

    event.listen(database.engine, "before_cursor_execute", record)
    try:
        response = client.get(f"/api/v1/projects/{project['id']}/ai/jobs/page?limit=50")
    finally:
        event.remove(database.engine, "before_cursor_execute", record)
    assert response.status_code == 200
    page = response.json()
    assert len(page["items"]) == 50
    assert page["next_cursor"]
    assert len(statements) <= 8
    assert len(response.content) < 100_000
    assert all("context_snapshot" not in item and "result" not in item for item in page["items"])
    next_page = client.get(
        f"/api/v1/projects/{project['id']}/ai/jobs/page",
        params={
            "limit": 50,
            "before": page["next_cursor"],
        },
    ).json()
    assert not (
        {item["id"] for item in page["items"]} & {item["id"] for item in next_page["items"]}
    )


def test_active_job_cursor_survives_anchor_transition_to_terminal_status(client, project):
    database = client.app.state.vault_registry.require(project["id"]).database
    with database.job_session_scope() as session:
        session.add_all(
            AIJob(
                project_id=project["id"],
                task_type="chat",
                status="queued",
                prompt_version="test",
                instructions=f"active-{index}",
            )
            for index in range(51)
        )

    first = client.get(
        f"/api/v1/projects/{project['id']}/ai/jobs/page",
        params={"active_only": True, "limit": 50},
    )
    assert first.status_code == 200
    cursor = first.json()["next_cursor"]
    assert cursor

    with database.job_session_scope() as session:
        anchor = session.get(AIJob, cursor)
        assert anchor is not None
        anchor.status = "succeeded"

    second = client.get(
        f"/api/v1/projects/{project['id']}/ai/jobs/page",
        params={"active_only": True, "limit": 50, "before": cursor},
    )
    assert second.status_code == 200
    assert len(second.json()["items"]) == 1
