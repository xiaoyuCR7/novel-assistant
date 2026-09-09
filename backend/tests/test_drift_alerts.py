from job_helpers import save_chapter


def test_severe_alert_requires_confirmation_and_never_edits_text(client, project, seeded_chapter):
    base = f"/api/v1/projects/{project['id']}"
    url = base + f"/chapters/{seeded_chapter}"
    save_chapter(
        client,
        url,
        json={
            "content": "寄信人是守钟人。",
            "contract": {"forbidden_revelations": ["寄信人是守钟人"]},
        },
    )
    response = client.post(url + "/drift-check")
    assert response.status_code == 200
    alert = next(item for item in response.json() if item["severity"] == "severe")
    decision = base + f"/conflicts/{alert['id']}/decision"
    assert client.post(decision, json={"decision": "accept"}).status_code == 409
    assert client.post(decision, json={"decision": "accept", "confirmed": True}).status_code == 200
    assert client.get(url).json()["content"] == "寄信人是守钟人。"
