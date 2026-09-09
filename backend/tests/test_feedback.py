from sqlalchemy import event


def test_feedback_creates_candidate_only_and_confirmed_rule_is_reversible(
    client, project, seeded_chapter
):
    response = client.post(
        f"/api/v1/projects/{project['id']}/feedback",
        json={
            "project_id": project["id"],
            "chapter_id": seeded_chapter,
            "rating": 2,
            "tags": ["啰嗦", "对白"],
            "original_text": "林渡点了点头，他用点头表示自己已经明白。",
            "corrected_text": "林渡点头。",
            "comment": "不要在动作后解释动作。",
        },
    )

    assert response.status_code == 201
    created = response.json()
    assert created["feedback"]["rating"] == 2
    assert created["feedback"]["tags"] == ["啰嗦", "对白"]
    candidate = created["preference_candidate"]
    assert candidate["status"] == "candidate"
    assert candidate["source_feedback_ids"] == [created["feedback"]["id"]]
    assert candidate["diff_summary"]

    before = client.get(f"/api/v1/projects/{project['id']}/styles/active-context")
    assert before.status_code == 200
    assert before.json()["rules"] == []

    confirmed = client.post(
        f"/api/v1/projects/{project['id']}/feedback/preferences/{candidate['id']}/confirm"
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "confirmed"

    active = client.get(f"/api/v1/projects/{project['id']}/styles/active-context").json()
    assert len(active["rules"]) == 1
    assert active["rules"][0]["source_feedback_ids"] == [created["feedback"]["id"]]

    disabled = client.post(
        f"/api/v1/projects/{project['id']}/feedback/preferences/{candidate['id']}/disable"
    )
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "disabled"
    assert (
        client.get(f"/api/v1/projects/{project['id']}/styles/active-context").json()["rules"] == []
    )

    feedback = client.get(f"/api/v1/projects/{project['id']}/feedback/{created['feedback']['id']}")
    assert feedback.status_code == 200
    assert feedback.json()["corrected_text"] == "林渡点头。"


def test_conflict_resolution_offers_three_traceable_strategies(client, project, seeded_chapter):
    response = client.post(
        f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}/conflicts",
        json={
            "code": "MOTIVATION_GAP",
            "severity": "warning",
            "message": "林渡缺少冒险进入第七码头的充分动机。",
            "evidence": ["上一章仍明确拒绝涉险", "本章直接进入码头"],
            "related_entity_ids": [],
        },
    )
    assert response.status_code == 201
    conflict = response.json()

    options_response = client.post(
        f"/api/v1/projects/{project['id']}/conflicts/{conflict['id']}/suggest-options"
    )
    assert options_response.status_code == 201
    options = options_response.json()
    assert {option["mode"] for option in options} == {"conservative", "balanced", "radical"}
    assert all(option["benefit"] for option in options)
    assert all(option["risk"] for option in options)
    assert all(option["ripple_effects"] for option in options)
    assert all("affected_entity_ids" in option for option in options)

    selected = client.post(
        f"/api/v1/projects/{project['id']}/conflicts/{conflict['id']}/decide",
        json={"option_id": options[1]["id"], "note": "采用平衡方案，但保留原场景顺序。"},
    )
    assert selected.status_code == 200
    assert selected.json()["status"] == "decided"
    assert selected.json()["selected_option_id"] == options[1]["id"]


def test_project_lists_preference_candidates_and_conflicts_with_options(
    client, project, seeded_chapter
):
    candidate = client.post(
        f"/api/v1/projects/{project['id']}/feedback",
        json={
            "project_id": project["id"],
            "chapter_id": seeded_chapter,
            "rating": 3,
            "tags": ["节奏"],
            "comment": "场景结尾要留下明确的新问题。",
        },
    ).json()["preference_candidate"]
    conflict = client.post(
        f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}/conflicts",
        json={
            "code": "WEAK_HOOK",
            "severity": "warning",
            "message": "章节结尾缺少推进下一章的问题。",
        },
    ).json()
    options = client.post(
        f"/api/v1/projects/{project['id']}/conflicts/{conflict['id']}/suggest-options"
    ).json()

    preferences_response = client.get(f"/api/v1/projects/{project['id']}/preferences")
    conflicts_response = client.get(f"/api/v1/projects/{project['id']}/conflicts")

    assert preferences_response.status_code == 200
    assert preferences_response.json()[0]["id"] == candidate["id"]
    assert conflicts_response.status_code == 200
    listed_conflict = conflicts_response.json()[0]
    assert listed_conflict["id"] == conflict["id"]
    assert [item["id"] for item in listed_conflict["options"]] == [item["id"] for item in options]


def test_conflict_list_defaults_to_open_and_can_request_decided_history(
    client, project, seeded_chapter
):
    base = f"/api/v1/projects/{project['id']}"
    conflict = client.post(
        f"{base}/chapters/{seeded_chapter}/conflicts",
        json={"code": "HISTORY_ONLY", "severity": "warning", "message": "history"},
    ).json()
    option = client.post(f"{base}/conflicts/{conflict['id']}/suggest-options").json()[0]
    decided = client.post(
        f"{base}/conflicts/{conflict['id']}/decide",
        json={"option_id": option["id"], "note": "resolved"},
    )
    assert decided.status_code == 200

    assert all(item["id"] != conflict["id"] for item in client.get(f"{base}/conflicts").json())
    history = client.get(f"{base}/conflicts", params={"status": "decided"})
    assert history.status_code == 200
    assert [item["id"] for item in history.json()] == [conflict["id"]]


def test_conflict_list_batches_options_with_constant_select_count(
    client, project, seeded_chapter
):
    base = f"/api/v1/projects/{project['id']}"
    database = client.app.state.vault_registry.require(project["id"]).database

    def add_conflicts(start: int, count: int):
        for index in range(start, start + count):
            conflict = client.post(
                f"{base}/chapters/{seeded_chapter}/conflicts",
                json={
                    "code": f"COUNT_{index}",
                    "severity": "warning",
                    "message": f"conflict {index}",
                },
            ).json()
            client.post(f"{base}/conflicts/{conflict['id']}/suggest-options")

    def select_count():
        statements = []

        def record(_connection, _cursor, statement, _parameters, _context, _many):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        event.listen(database.engine, "before_cursor_execute", record)
        try:
            response = client.get(f"{base}/conflicts")
        finally:
            event.remove(database.engine, "before_cursor_execute", record)
        assert response.status_code == 200
        return len(statements)

    add_conflicts(0, 1)
    small = select_count()
    add_conflicts(1, 24)
    large = select_count()
    assert small == large


def test_entity_state_conflict_list_batches_across_chapters(client, project, seeded_chapter):
    base = f"/api/v1/projects/{project['id']}"
    database = client.app.state.vault_registry.require(project["id"]).database

    def add_state_conflict(chapter_id: str, suffix: str):
        response = client.post(
            f"{base}/chapters/{chapter_id}/conflicts",
            json={
                "code": "ENTITY_STATE_CONFLICT",
                "severity": "warning",
                "message": f"state conflict {suffix}",
                "evidence": [],
            },
        )
        assert response.status_code == 201

    def select_count():
        statements = []

        def record(_connection, _cursor, statement, _parameters, _context, _many):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        event.listen(database.engine, "before_cursor_execute", record)
        try:
            response = client.get(f"{base}/conflicts")
        finally:
            event.remove(database.engine, "before_cursor_execute", record)
        assert response.status_code == 200
        return len(statements)

    add_state_conflict(seeded_chapter, "seed")
    small = select_count()
    for index in range(8):
        chapter = client.post(
            f"{base}/nodes",
            json={
                "kind": "chapter",
                "title": f"Conflict chapter {index}",
                "order_index": index + 10,
            },
        )
        assert chapter.status_code == 201
        add_state_conflict(chapter.json()["id"], str(index))
    large = select_count()

    assert small == large
