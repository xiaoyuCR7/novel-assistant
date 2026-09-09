from datetime import datetime, timedelta


def test_library_revision_trash_restore_and_expiry(client, project):
    base = f"/api/v1/projects/{project['id']}"
    result = client.post(
        base + "/library/entity",
        json={
            "title": "林渡",
            "content": "雾城的邮差，害怕钟声。",
            "kind": "character",
        },
    )
    assert result.status_code == 201
    item = result.json()
    url = base + f"/library/entity/{item['id']}"
    assert client.patch(url, json={"revision": 1, "title": "林渡邮差"}).status_code == 200
    assert client.patch(url, json={"revision": 1, "title": "过期"}).status_code == 409
    assert client.get(base + "/library/search", params={"q": "钟声"}).json()["items"]
    assert client.delete(url).status_code == 200
    assert not client.get(base + "/library/search", params={"q": "钟声"}).json()["items"]
    assert not client.get(base + "/workspace").json()["entities"]
    trash = base + f"/trash/entity/{item['id']}"
    assert client.delete(trash + "/purge").status_code == 422
    assert client.post(trash + "/restore").status_code == 200
    assert client.get(base + "/library/search", params={"q": "钟声"}).json()["items"]
    client.delete(url)
    from novel_harness.db.models import Entity

    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        entity = session.get(Entity, item["id"], execution_options={"include_deleted": True})
        entity.purge_after = datetime.now() - timedelta(days=1)
    assert client.delete(trash + "/purge").status_code == 200
    assert client.get(base + "/trash").json() == []


def test_chinese_fts_channels_and_cross_vault_queries(client, project):
    base = f"/api/v1/projects/{project['id']}"
    entity = client.post(
        base + "/entities",
        json={
            "kind": "character",
            "name": "林渡",
            "summary": "只有在雾城才有的蓝色秘密",
        },
    ).json()
    response = client.get(base + "/library/search", params={"q": "蓝色秘密"}).json()
    assert response["items"][0]["id"] == entity["id"]
    assert "fts" in response["items"][0]["channels"]
    assert response["items"][0]["reason"]
    other = client.post("/api/v1/projects", json={"title": "另一部书"}).json()
    assert not client.get(
        f"/api/v1/projects/{other['id']}/library/search", params={"q": "蓝色秘密"}
    ).json()["items"]
    health = client.get(base + "/rag/health").json()
    assert health["fts"] == "ready"
    assert health["vectors"] == "disabled"
