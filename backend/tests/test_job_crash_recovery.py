import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from novel_harness.main import create_app


@pytest.mark.parametrize(
    "boundary",
    [
        "before_dispatch",
        "after_dispatch",
        "after_output",
        "after_summary_publish",
        "after_ledger_replace",
    ],
)
def test_process_death_preserves_recovery_boundary(tmp_path, monkeypatch, boundary):
    monkeypatch.setenv("NOVEL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NOVEL_AI_PROVIDER", "demo")
    summary = boundary.startswith("after_summary") or boundary == "after_ledger_replace"
    with TestClient(create_app(start_executor=False)) as client:
        project = client.post("/api/v1/projects", json={"title": "Crash test"}).json()
        base = f"/api/v1/projects/{project['id']}"
        if summary:
            chapter = client.post(
                base + "/nodes", json={"title": "Chapter", "kind": "chapter"}
            ).json()
            url = base + "/chapters/" + chapter["id"]
            document = client.put(url, json={
                "content": "Saved manuscript", "revision": client.get(url).json()['revision'],
            }).json()
            receipt = client.post(
                url + "/complete",
                json={"expected_revision": document["revision"]},
                headers={"Idempotency-Key": "crash"},
            ).json()
        else:
            receipt = client.post(
                base + "/ai/jobs",
                json={"project_id": project["id"], "task_type": "chat"},
                headers={"Idempotency-Key": "crash"},
            ).json()
    script = str(Path(__file__).with_name("job_crash_process.py"))
    result = subprocess.run(
        [sys.executable, script, str(tmp_path), boundary], capture_output=True, timeout=20
    )
    assert result.returncode == 31, result.stderr.decode(errors="replace")
    recovered = subprocess.run(
        [sys.executable, script, str(tmp_path), "recover"], capture_output=True, timeout=20
    )
    assert recovered.returncode == 0, recovered.stderr.decode(errors="replace")
    with TestClient(create_app(start_executor=False)) as client:
        job = client.get(receipt["status_url"]).json()
        assert job["status"] == (
            "recovery_required" if boundary == "after_dispatch" else "succeeded"
        )
        if boundary == "after_dispatch":
            assert job["recovery_reason"] == "result_unknown"
        if summary:
            assert job["effects"]["summary_id"]
            assert job["effects"]["ledger_pending"] is False
    audit = tmp_path / "fake-provider-calls.db"
    if boundary == "after_dispatch":
        assert not audit.exists()
    else:
        with closing(sqlite3.connect(audit)) as connection, connection:
            assert connection.execute("SELECT count(*) FROM calls").fetchone()[0] == 1
