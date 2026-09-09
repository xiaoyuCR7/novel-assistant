from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from job_helpers import complete_summary, create_chapter_version, save_chapter


@pytest.mark.parametrize("expected_revision", [None, 0, -1])
def test_version_create_requires_observed_positive_revision(
    client, project, seeded_chapter, expected_revision,
):
    url = f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}"
    before = client.get(url).json()
    payload = {"content": "blind overwrite"}
    if expected_revision is not None:
        payload["expected_revision"] = expected_revision

    response = client.post(url + "/versions", json=payload)

    assert response.status_code == 422
    assert client.get(url).json() == before
    assert client.get(url + "/versions").json() == []


@pytest.mark.parametrize("stale_content", ["old A", "[[ref:entity:missing]]"])
@pytest.mark.parametrize("complete_latest", [False, True])
def test_version_create_rejects_stale_writer_without_changing_document_or_summary(
    client, project, seeded_chapter, stale_content, complete_latest,
):
    url = f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}"
    save_chapter(client, url, json={"content": "old A"})
    complete_summary(client, url)
    observed_revision = client.get(url).json()["revision"]
    save_chapter(client, url, json={"content": "latest unsnapshotted B"})
    if complete_latest:
        complete_summary(client, url)
    current = client.get(url).json()
    versions = client.get(url + "/versions").json()
    summary = client.get(url + "/summary").json()
    assert summary["status"] == ("valid" if complete_latest else "stale")

    response = client.post(url + "/versions", json={
        "content": stale_content, "expected_revision": observed_revision,
    })

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "revision_conflict"
    assert response.json()["detail"]["current"] == current
    assert response.json()["detail"]["message"]
    assert client.get(url).json() == current
    assert client.get(url + "/versions").json() == versions
    assert client.get(url + "/summary").json() == summary


def test_concurrent_version_writers_only_publish_one_observed_revision(
    client, project, seeded_chapter,
):
    url = f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}"
    observed_revision = client.get(url).json()["revision"]
    ready = Barrier(2)

    def publish(content):
        ready.wait(timeout=5)
        return client.post(url + "/versions", json={
            "content": content, "expected_revision": observed_revision,
        })

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(publish, ["writer A", "writer B"]))

    assert sorted(response.status_code for response in responses) == [201, 409]
    winner = next(response.json() for response in responses if response.status_code == 201)
    rejected = next(response.json() for response in responses if response.status_code == 409)
    current = client.get(url).json()
    assert current["content"] == winner["content"]
    assert current["current_version_id"] == winner["id"]
    assert current["revision"] == observed_revision + 1
    assert rejected["detail"]["current"] == current
    assert client.get(url + "/versions").json() == [winner]


def test_chapter_contract_draft_versions_diff_and_restore(client, project, seeded_chapter):
    contract = {
        "purpose": "让主角收到来自未来的信",
        "pov_entity_id": None,
        "tense": "past",
        "opening_state": "主角不相信预言",
        "ending_state": "主角决定赴约",
        "required_entity_ids": [],
        "required_plot_ids": [],
        "forbidden_revelations": ["寄信人的真实身份"],
        "forbidden_phrases": ["命运的齿轮"],
        "target_words": 3000,
    }
    updated = save_chapter(
        client,
        f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}",
        json={"content": "雨落在无人签收的信封上。", "contract": contract},
    )
    assert updated.status_code == 200
    assert updated.json()["contract"]["purpose"] == contract["purpose"]

    first = create_chapter_version(
        client,
        f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}/versions",
        json={"content": "第一稿\n雨落在信封上。", "source": "manual", "summary": "初稿"},
    )
    assert first.status_code == 201
    first_version = first.json()

    second = create_chapter_version(
        client,
        f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}/versions",
        json={"content": "第二稿\n雪落在信封上。", "source": "manual", "summary": "改写天气"},
    )
    assert second.status_code == 201
    second_version = second.json()
    assert second_version["parent_version_id"] == first_version["id"]
    assert second_version["word_count"] > 0

    listed = client.get(f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}/versions")
    assert listed.status_code == 200
    assert [version["id"] for version in listed.json()] == [
        second_version["id"],
        first_version["id"],
    ]

    diff = client.get(
        f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}/versions/{first_version['id']}/diff/{second_version['id']}"
    )
    assert diff.status_code == 200
    assert "-第一稿" in diff.json()["diff"]
    assert "+第二稿" in diff.json()["diff"]

    restored = client.post(
        f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}/versions/{first_version['id']}/restore",
        json={
            "expected_revision": client.get(
                f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}",
            ).json()["revision"]
        },
    )
    assert restored.status_code == 201
    restored_version = restored.json()
    assert restored_version["content"] == "第一稿\n雨落在信封上。"
    assert restored_version["parent_version_id"] == second_version["id"]
    assert restored_version["restored_from_version_id"] == first_version["id"]

    unchanged = client.get(
        f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}/versions/{second_version['id']}"
    )
    assert unchanged.status_code == 200
    assert unchanged.json()["content"] == "第二稿\n雪落在信封上。"

    chapter = client.get(f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}").json()
    assert chapter["content"] == restored_version["content"]
    assert chapter["current_version_id"] == restored_version["id"]


def test_restore_rejects_version_from_another_chapter(client, project, seeded_chapter):
    other = client.post(
        f"/api/v1/projects/{project['id']}/nodes",
        json={"kind": "chapter", "title": "第二章", "order_index": 2},
    ).json()
    foreign_version = create_chapter_version(
        client,
        f"/api/v1/projects/{project['id']}/chapters/{other['id']}/versions",
        json={"content": "别章正文", "source": "manual"},
    ).json()

    response = client.post(
        f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}/versions/{foreign_version['id']}/restore",
        json={
            "expected_revision": client.get(
                f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}",
            ).json()["revision"]
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "CROSS_CHAPTER_VERSION"
