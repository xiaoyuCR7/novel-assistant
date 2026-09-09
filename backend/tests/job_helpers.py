"""Exercise the actual async protocol; never synthesize HTTP responses."""

from uuid import uuid4


def save_chapter(client, url, *, json):
    """Read the revision like an author client; explicit stale revisions stay stale."""
    payload = dict(json)
    if 'revision' not in payload:
        current = client.get(url)
        assert current.status_code == 200, current.text
        payload['revision'] = current.json()['revision']
    return client.put(url, json=payload)


def create_chapter_version(client, url, *, json):
    """Publish with the revision observed by this author client."""
    payload = dict(json)
    if 'expected_revision' not in payload:
        current = client.get(url.removesuffix('/versions'))
        assert current.status_code == 200, current.text
        payload['expected_revision'] = current.json()['revision']
    return client.post(url, json=payload)


def finish_job(client, receipt):
    assert receipt.status_code == 202, receipt.text
    path = receipt.json()["status_url"]
    for _ in range(100):
        response = client.get(path)
        if response.json()["status"] not in {"queued", "running", "cancel_requested"}:
            return response
        assert client.app.state.job_executor.run_once()
    raise AssertionError("Test task did not reach a terminal/paused state")


def run_job(client, url, *, json):
    command = dict(json)
    if command.get("chapter_id"):
        base = url.removesuffix("/ai/jobs")
        command["expected_revision"] = client.get(
            base + "/chapters/" + command["chapter_id"],
        ).json()["revision"]
    receipt = client.post(url, json=command, headers={"Idempotency-Key": uuid4().hex})
    return finish_job(client, receipt) if receipt.status_code == 202 else receipt


def complete_job(client, chapter_url):
    revision = client.get(chapter_url).json()["revision"]
    receipt = client.post(
        chapter_url + "/complete",
        json={"expected_revision": revision},
        headers={"Idempotency-Key": uuid4().hex},
    )
    return finish_job(client, receipt)


def complete_summary(client, chapter_url):
    result = complete_job(client, chapter_url)
    assert result.json()["status"] == "succeeded", result.text
    return client.get(chapter_url + "/summary")
