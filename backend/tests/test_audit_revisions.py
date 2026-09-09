from job_helpers import create_chapter_version


def test_chapter_update_requires_observed_revision(client, project, seeded_chapter):
    url = f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}"
    revision = client.get(url).json()["revision"]
    assert client.put(url, json={"content": "blind overwrite"}).status_code == 422
    assert client.put(url, json={"content": "first", "revision": revision}).status_code == 200
    assert client.put(url, json={"content": "stale", "revision": revision}).status_code == 409
    assert client.get(url).json()["content"] == "first"


def test_restore_requires_current_revision(client, project, seeded_chapter):
    url = f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}"
    version = create_chapter_version(
        client, url + "/versions", json={"content": "old version"}
    ).json()
    revision = client.get(url).json()["revision"]
    restore = url + "/versions/" + version["id"] + "/restore"
    assert client.post(restore).status_code == 422
    assert client.put(url, json={"content": "new draft", "revision": revision}).status_code == 200
    assert client.post(restore, json={"expected_revision": revision}).status_code == 409
    assert client.get(url).json()["content"] == "new draft"
    current = client.get(url).json()["revision"]
    assert client.post(restore, json={"expected_revision": current}).status_code == 201
    assert client.get(url).json()["content"] == "old version"


def test_summary_identity_required_and_checked(client, project, seeded_chapter):
    from job_helpers import complete_summary, save_chapter

    url = f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}"
    save_chapter(client, url, json={"content": "正文"})
    summary = complete_summary(client, url).json()
    assert (
        client.patch(
            url + "/summary", json={"revision": summary["revision"], "recap": "old"}
        ).status_code
        == 422
    )
    assert (
        client.patch(
            url + "/summary",
            json={
                "summary_id": "other",
                "revision": summary["revision"],
                "recap": "old",
            },
        ).status_code
        == 409
    )
