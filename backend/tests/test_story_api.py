def test_story_workspace_round_trip(client):
    project_response = client.post(
        "/api/v1/projects",
        json={
            "title": "雾城来信",
            "premise": "失忆的邮差替死者送出最后一批信。",
            "genre": "奇幻悬疑",
            "target_words": 300000,
            "daily_goal": 1800,
        },
    )
    assert project_response.status_code == 201
    project = project_response.json()

    idea = client.post(
        f"/api/v1/projects/{project['id']}/ideas",
        json={
            "title": "不会抵达的第七码头",
            "content": "每逢雾潮，第七码头只对死者开放。",
            "tags": ["码头", "亡者", "谜题"],
            "source": "梦境记录",
            "status": "captured",
        },
    )
    assert idea.status_code == 201

    volume = client.post(
        f"/api/v1/projects/{project['id']}/nodes",
        json={"kind": "volume", "title": "第一卷：无主之信", "order_index": 1},
    ).json()
    chapter = client.post(
        f"/api/v1/projects/{project['id']}/nodes",
        json={
            "kind": "chapter",
            "parent_id": volume["id"],
            "title": "雾中投递",
            "summary": "林渡收到一封署名为自己的遗书。",
            "order_index": 1,
            "target_words": 4200,
            "status": "planned",
        },
    ).json()
    scene = client.post(
        f"/api/v1/projects/{project['id']}/nodes",
        json={
            "kind": "scene",
            "parent_id": chapter["id"],
            "title": "钟楼下的交接",
            "summary": "守钟人把无主邮袋交给林渡。",
            "order_index": 1,
        },
    ).json()

    character = client.post(
        f"/api/v1/projects/{project['id']}/entities",
        json={
            "kind": "character",
            "name": "林渡",
            "summary": "二十四岁的雾城邮差，缺失三年记忆。",
            "profile": {"age": 24, "voice": "寡言，回避直接承诺"},
            "state": {"location": "旧邮局", "alive": True, "knowledge": ["雾潮时刻"]},
        },
    ).json()
    location = client.post(
        f"/api/v1/projects/{project['id']}/entities",
        json={
            "kind": "location",
            "name": "旧邮局",
            "summary": "雾城唯一仍在夜间营业的邮局。",
            "profile": {"district": "钟楼区"},
            "state": {"condition": "半废弃"},
        },
    ).json()
    relation = client.post(
        f"/api/v1/projects/{project['id']}/relations",
        json={
            "source_entity_id": character["id"],
            "target_entity_id": location["id"],
            "relation_type": "works_at",
            "description": "林渡值守夜班。",
        },
    )
    assert relation.status_code == 201

    canon = client.post(
        f"/api/v1/projects/{project['id']}/canon",
        json={
            "subject_entity_id": character["id"],
            "predicate": "age",
            "value": 24,
            "source_note": "人物初始档案",
            "status": "confirmed",
        },
    )
    assert canon.status_code == 201

    timeline = client.post(
        f"/api/v1/projects/{project['id']}/timeline",
        json={
            "title": "收到遗书",
            "story_time": "雾历103年霜月初七 23:40",
            "sort_key": 10311072340,
            "chapter_id": chapter["id"],
            "description": "林渡在夜班分拣时发现署名为自己的信。",
        },
    )
    assert timeline.status_code == 201

    plot = client.post(
        f"/api/v1/projects/{project['id']}/plots",
        json={
            "kind": "main",
            "title": "寻找遗书来源",
            "promise": "遗书为何知道林渡失去的三年。",
            "status": "active",
            "start_node_id": chapter["id"],
        },
    )
    assert plot.status_code == 201
    foreshadowing = client.post(
        f"/api/v1/projects/{project['id']}/plots",
        json={
            "kind": "foreshadowing",
            "title": "邮戳上的第七码头",
            "promise": "现实地图不存在第七码头。",
            "status": "planted",
            "start_node_id": chapter["id"],
            "due_node_id": scene["id"],
        },
    )
    assert foreshadowing.status_code == 201

    style = client.post(
        f"/api/v1/projects/{project['id']}/styles",
        json={
            "name": "冷雾叙事",
            "is_active": True,
            "config": {
                "person": "third_limited",
                "tense": "past",
                "lyricism": 0.55,
                "dialogue_ratio": 0.25,
            },
        },
    )
    assert style.status_code == 201

    workspace_response = client.get(f"/api/v1/projects/{project['id']}/workspace")
    assert workspace_response.status_code == 200
    workspace = workspace_response.json()

    assert workspace["project"]["title"] == "雾城来信"
    assert workspace["ideas"][0]["tags"] == ["码头", "亡者", "谜题"]
    assert [node["kind"] for node in workspace["nodes"]] == ["volume", "chapter", "scene"]
    assert workspace["entities"][0]["profile"]["age"] == 24
    assert workspace["relations"][0]["relation_type"] == "works_at"
    assert workspace["canon_facts"][0]["value"] == 24
    assert workspace["timeline"][0]["sort_key"] == 10311072340
    assert {item["kind"] for item in workspace["plots"]} == {"main", "foreshadowing"}
    assert workspace["styles"][0]["config"]["person"] == "third_limited"


def test_rejects_cross_project_parent(client, project):
    other = client.post(
        "/api/v1/projects",
        json={"title": "另一部书", "premise": "边界测试", "genre": "测试"},
    ).json()
    foreign_parent = client.post(
        f"/api/v1/projects/{other['id']}/nodes",
        json={"kind": "volume", "title": "别人的卷", "order_index": 1},
    ).json()

    response = client.post(
        f"/api/v1/projects/{project['id']}/nodes",
        json={
            "kind": "chapter",
            "parent_id": foreign_parent["id"],
            "title": "越界章节",
            "order_index": 1,
        },
    )

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "REFERENCE_NOT_FOUND"
