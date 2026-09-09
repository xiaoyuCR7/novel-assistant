from job_helpers import create_chapter_version, save_chapter


def test_working_drafts_not_indexed_but_explicit_search_can_quote_saved_versions(
    client, project, seeded_chapter
):
    base = f"/api/v1/projects/{project['id']}"
    url = base + f"/chapters/{seeded_chapter}"
    save_chapter(client, url, json={"content": "仅存在于草稿的紫色沙漏"})
    assert (
        client.get(
            base + "/library/search", params={"q": "紫色沙漏", "include_manuscripts": True}
        ).json()["items"]
        == []
    )
    created = create_chapter_version(
        client, url + "/versions", json={"content": "已经确认的紫色沙漏原文"}
    )
    assert created.status_code == 201
    default = client.get(base + "/library/search", params={"q": "紫色沙漏"}).json()["items"]
    assert not any(item["type"] == "manuscript" for item in default)
    explicit = client.get(
        base + "/library/search", params={"q": "紫色沙漏", "include_manuscripts": True}
    ).json()["items"]
    assert any(item["type"] == "manuscript" and "已经确认" in item["content"] for item in explicit)
    client.delete(base + f"/library/node/{seeded_chapter}")
    assert (
        client.get(
            base + "/library/search", params={"q": "紫色沙漏", "include_manuscripts": True}
        ).json()["items"]
        == []
    )
