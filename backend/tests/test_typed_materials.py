def test_typed_edits_and_active_style_conflicts(client, project):
    base = f"/api/v1/projects/{project['id']}"
    first = client.post(
        base + "/styles",
        json={
            "name": "冷静",
            "is_active": True,
            "config": {"pov": "first"},
        },
    ).json()
    second = client.post(
        base + "/styles",
        json={
            "name": "热烈",
            "is_active": True,
            "config": {"pov": "third"},
        },
    ).json()
    assert client.get(base + "/workspace").json()["memory_conflicts"][0]["requires_author_decision"]
    result = client.patch(
        base + f"/library/style/{second['id']}",
        json={
            "revision": second["revision"],
            "fields": {"is_active": False},
        },
    )
    assert result.status_code == 200
    assert result.json()["record"]["is_active"] is False
    assert client.get(base + "/workspace").json()["memory_conflicts"] == []
    assert first["is_active"]


def test_typed_fields_validate_cross_project_and_unknown_keys(client, project, seeded_chapter):
    base = f"/api/v1/projects/{project['id']}"
    plot = client.post(base + "/library/plot", json={"title": "线索"}).json()
    url = base + f"/library/plot/{plot['id']}"
    assert (
        client.patch(
            url, json={"revision": 1, "fields": {"status": "resolved", "payoff": "已回收"}}
        ).json()["record"]["status"]
        == "resolved"
    )
    assert (
        client.patch(url, json={"revision": 2, "fields": {"project_id": "other"}}).status_code
        == 422
    )
    assert client.patch(
        url, json={"revision": 2, "fields": {"due_node_id": "foreign"}}
    ).status_code in {404, 422}
    node = client.get(base + "/workspace").json()["nodes"][-1]
    assert (
        client.patch(
            base + f"/library/node/{seeded_chapter}",
            json={
                "revision": node["revision"],
                "fields": {"parent_id": seeded_chapter},
            },
        ).status_code
        == 422
    )


def test_overlapping_confirmed_facts_report_conflict(client, project):
    base = f"/api/v1/projects/{project['id']}"
    for value in ["晴", "雨"]:
        assert (
            client.post(base + "/canon", json={"predicate": "天气", "value": value}).status_code
            == 201
        )
    assert any(
        c["code"] == "CANON_OVERLAP"
        for c in client.get(base + "/workspace").json()["memory_conflicts"]
    )
