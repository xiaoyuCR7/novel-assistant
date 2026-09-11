"""Author-selected quality changes, against isolated demo API projects."""

import pytest

from novel_harness.ai.base import TextResult
from novel_harness.ai.demo import DemoProvider

BASE = "第一段旧句。\n\n保留的过渡句。\n\n末段旧句。\n"
CANDIDATE = "第一段新句。\n\n保留的过渡句。\n\n末段新句。\n"


def prepared(client, project, chapter_id, mode="polish"):
    class Editor(DemoProvider):
        def generate_text(self, request):
            if request.task == "quality_rewrite":
                return TextResult(text=CANDIDATE, provider=self.name, model=self.model)
            return super().generate_text(request)

        def generate_structured(self, request, schema):
            result = super().generate_structured(request, schema)
            if request.task == "quality_review":
                result.data["scores"] = dict.fromkeys(result.data["scores"], 60)
            return result

    client.app.state.ai_provider = Editor()
    chapter_url = f"/api/v1/projects/{project['id']}/chapters/{chapter_id}"
    document = client.get(chapter_url).json()
    saved = client.put(chapter_url, json={
        "content": BASE, "contract": {}, "revision": document["revision"],
    })
    assert saved.status_code == 200, saved.text
    base = f"/api/v1/projects/{project['id']}/quality/runs"
    response = client.post(base, headers={"Idempotency-Key": "partial-test"}, json={
        "mode": mode,
        "chapters": [{"chapter_id": chapter_id, "expected_revision": saved.json()["revision"]}],
    })
    assert response.status_code == 202, response.text
    assert client.app.state.job_executor.run_once()
    url = base + "/" + response.json()["id"]
    result = client.get(url).json()
    assert result["status"] == "succeeded", result
    return url, chapter_url, result


def changes(client, url, chapter_id):
    response = client.get(f"{url}/chapters/{chapter_id}/diff")
    assert response.status_code == 200, response.text
    return response.json()["hunks"]


def test_selected_hunks_build_a_version_on_server_without_altering_quality_report(
    client, project, seeded_chapter,
):
    url, chapter_url, original = prepared(client, project, seeded_chapter)
    hunks = changes(client, url, seeded_chapter)
    assert len(hunks) == 2
    command = {"chapter_id": seeded_chapter, "selected_hunk_ids": [hunks[0]["id"]]}
    response = client.post(url + "/accept", json=command)
    assert response.status_code == 200, response.text
    expected = BASE.replace("第一段旧句。", "第一段新句。")
    assert response.json()["content"] == expected
    assert client.get(chapter_url).json()["content"] == expected
    assert "局部" in response.json()["summary"]
    after = client.get(url).json()
    assert after["result"] == original["result"]
    assert after["effects"]["accepted_selections"][seeded_chapter] == [hunks[0]["id"]]
    retried = client.post(url + "/accept", json=command)
    assert retried.status_code == 200
    assert retried.json()["id"] == response.json()["id"]
    different = client.post(url + "/accept", json={
        "chapter_id": seeded_chapter, "selected_hunk_ids": [hunks[1]["id"]],
    })
    assert different.status_code == 409


@pytest.mark.parametrize("selection", [[], ["unknown-hunk"], ["duplicate", "duplicate"]])
def test_invalid_or_empty_selection_never_saves(client, project, seeded_chapter, selection):
    url, chapter_url, _ = prepared(client, project, seeded_chapter)
    response = client.post(url + "/accept", json={
        "chapter_id": seeded_chapter, "selected_hunk_ids": selection,
    })
    assert response.status_code in {409, 422}, response.text
    assert client.get(chapter_url).json()["content"] == BASE
    assert not client.get(url).json()["effects"].get("accepted_chapters")


def test_stale_source_revision_is_rejected_for_partial_acceptance(client, project, seeded_chapter):
    url, chapter_url, _ = prepared(client, project, seeded_chapter)
    hunks = changes(client, url, seeded_chapter)
    document = client.get(chapter_url).json()
    saved = client.put(chapter_url, json={
        "content": "作者已经改过", "contract": {}, "revision": document["revision"],
    })
    assert saved.status_code == 200
    response = client.post(url + "/accept", json={
        "chapter_id": seeded_chapter, "selected_hunk_ids": [hunks[0]["id"]],
    })
    assert response.status_code == 409
    assert client.get(chapter_url).json()["content"] == "作者已经改过"


def test_collaboration_cannot_partially_accept_a_handoff(client, project, seeded_chapter):
    url, chapter_url, _ = prepared(client, project, seeded_chapter, mode="collaborate")
    hunks = changes(client, url, seeded_chapter)
    response = client.post(url + "/accept", json={
        "chapter_id": seeded_chapter, "selected_hunk_ids": [hunks[0]["id"]],
    })
    assert response.status_code == 422
    assert client.get(chapter_url).json()["content"] == BASE


def test_arbitrary_client_text_is_rejected(client, project, seeded_chapter):
    url, chapter_url, _ = prepared(client, project, seeded_chapter)
    response = client.post(
        url + "/accept", json={"chapter_id": seeded_chapter, "content": "注入任意正文"}
    )
    assert response.status_code == 422
    assert client.get(chapter_url).json()["content"] == BASE


@pytest.mark.parametrize(("original", "candidate"), [
    ("甲\n乙\n", "新增\n甲\n乙\n"),
    ("甲\n待删\n乙\n", "甲\n乙\n"),
    ("甲\r\n\r\n乙", "甲\r\n\r\n新乙\r\n"),
    ("𠮷\n相同\n尾", "😀\n相同\n新尾"),
    ("甲\n" * 2100, "乙\n" * 2100),
], ids=["insert", "delete", "crlf", "unicode", "bounded"])
def test_selecting_all_hunks_preserves_exact_candidate_text(original, candidate):
    from novel_harness.services.quality import _quality_diff_parts, _selected_quality_text

    *_, hunks, coarse = _quality_diff_parts(original, candidate)
    assert _selected_quality_text(original, candidate, [h["id"] for h in hunks]) == candidate
    assert coarse is (original.count("\n") > 2048)
