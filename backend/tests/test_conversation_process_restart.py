"""HTTP proofs of conversation durability across real, abrupt process restarts."""

import json
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest


@contextmanager
def running_backend(data_root):
    """Use only a pytest-owned vault, an ephemeral loopback port and offline demo."""
    data_root.mkdir(parents=True, exist_ok=True)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    source = Path(__file__).resolve().parents[1] / "src"
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("NOVEL_", "OPENAI_", "DEEPSEEK_"))
    }
    env.update(
        NOVEL_DATA_DIR=str(data_root),
        NOVEL_STATIC_DIR=str(data_root / "no-static-files"),
        NOVEL_AI_PROVIDER="demo",
        NOVEL_LOCAL_EMBEDDING_MODEL="",
        PYTHONPATH=str(source),
    )
    log_path = data_root / "process.log"
    with log_path.open("ab", buffering=0) as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "novel_harness.main:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--no-access-log",
            ],
            cwd=data_root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}", timeout=5, trust_env=False
            ) as client:
                deadline = time.monotonic() + 20
                while True:
                    assert process.poll() is None, log_path.read_text(errors="replace")[-3000:]
                    try:
                        health = client.get("/api/v1/health")
                        if health.status_code == 200:
                            assert health.json() == {
                                "status": "ok",
                                "database": "ok",
                                "ai_provider": "demo",
                            }
                            break
                    except httpx.TransportError:
                        pass
                    assert time.monotonic() < deadline, log_path.read_text(errors="replace")[-3000:]
                    time.sleep(0.05)
                yield client, process.pid
        finally:
            # Deliberately bypass graceful shutdown: only committed SQLite data may survive.
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)


def finished(client, url):
    deadline = time.monotonic() + 20
    while True:
        response = client.get(url)
        assert response.status_code == 200, response.text
        job = response.json()
        if job["status"] not in {"queued", "running", "cancel_requested"}:
            return job
        assert time.monotonic() < deadline, job
        time.sleep(0.05)


def chat(client, base, project_id, instructions, key, chapter_id=None):
    command = {"project_id": project_id, "task_type": "chat", "instructions": instructions}
    if chapter_id:
        command.update(
            chapter_id=chapter_id,
            expected_revision=client.get(base + f"/chapters/{chapter_id}").json()["revision"],
        )
    response = client.post(base + "/ai/jobs", headers={"Idempotency-Key": key}, json=command)
    assert response.status_code == 202, response.text
    job = finished(client, response.json()["status_url"])
    assert job["status"] == "succeeded", job
    return job


@pytest.mark.parametrize("quality_target", [75, 90], ids=["completed-quality", "paused-quality"])
def test_abrupt_restart_preserves_full_chat_quality_handoffs_and_next_turn_memory(
    tmp_path, quality_target
):
    data_root = tmp_path / "isolated-vault"
    instructions = [
        "第一轮：记住暗号“北门铜铃”，请完整保存以下讨论。\n"
        + "码头钥匙由邮差保管，等待下一章交接。" * 75,
        "第二轮：我们继续讨论北门铜铃，与上一轮保持联系。",
    ]
    with running_backend(data_root) as (client, first_pid):
        response = client.post("/api/v1/projects", json={"title": "真实重启会话验证"})
        assert response.status_code == 201, response.text
        project_id = response.json()["id"]
        base = f"/api/v1/projects/{project_id}"
        chapters = []
        for index in range(2):
            response = client.post(
                base + "/nodes",
                json={"kind": "chapter", "title": f"第{index + 1}章", "order_index": index},
            )
            assert response.status_code == 201, response.text
            chapters.append(response.json()["id"])
        chats = [
            chat(client, base, project_id, text, f"book-{index}")
            for index, text in enumerate(instructions)
        ]
        chapter_chat = chat(
            client, base, project_id, "本章独立讨论，不应混进全书会话。", "chapter-1", chapters[0]
        )
        response = client.post(
            base + "/quality/runs",
            headers={"Idempotency-Key": "paired-quality"},
            json={
                "mode": "collaborate",
                "quality_target": quality_target,
                "chapters": [
                    {
                        "chapter_id": cid,
                        "expected_revision": client.get(base + f"/chapters/{cid}").json()[
                            "revision"
                        ],
                    }
                    for cid in chapters
                ],
            },
        )
        assert response.status_code == 202, response.text
        quality_url = base + f"/quality/runs/{response.json()['id']}"
        quality = finished(client, quality_url)
        if quality_target == 90:
            assert quality["status"] == "recovery_required", quality
            approval = client.post(
                quality_url + "/approve",
                json={
                    "chapter_id": chapters[0],
                    "expected_control_revision": quality["control_revision"],
                    "confirmed": True,
                },
            )
            assert approval.status_code == 200, approval.text
            resumed = client.post(
                quality["status_url"] + "/resume",
                headers={"Idempotency-Key": "approve-first"},
                json={"expected_control_revision": approval.json()["control_revision"]},
            )
            assert resumed.status_code == 202, resumed.text
            quality = finished(client, quality_url)
        assert quality["status"] == (
            "succeeded" if quality_target == 75 else "recovery_required"
        ), quality
        assert [item["chapter_id"] for item in quality["result"]["chapters"]] == chapters
        assert [message["role"] for message in quality["result"]["messages"]] == [
            "writer",
            "optimizer",
            "writer",
            "optimizer",
        ]
        assert all(
            item["draft"]
            and item["candidate_text"]
            and item["before"]
            and item["after"]
            and item["handoff"]["next_guidance"]
            for item in quality["result"]["chapters"]
        )
        assert chats[0]["instructions"] == instructions[0]
        assert len(chats[0]["instructions"]) > 1000

    with running_backend(data_root) as (client, second_pid):
        assert second_pid != first_pid
        assert project_id in [item["id"] for item in client.get("/api/v1/projects").json()]
        for previous in [*chats, chapter_chat]:
            assert client.get(previous["status_url"]).json() == previous
        assert client.get(quality_url).json() == quality
        ordinary = client.get(base + "/ai/jobs").json()
        assert [item["id"] for item in ordinary] == [item["id"] for item in chats]
        scoped = client.get(base + "/ai/jobs", params={"chapter_id": chapters[0]}).json()
        assert [item["id"] for item in scoped] == [chapter_chat["id"]]
        first_page = client.get(base + "/ai/jobs/page", params={"limit": 1}).json()
        second_page = client.get(
            base + "/ai/jobs/page", params={"limit": 1, "before": first_page["next_cursor"]}
        ).json()
        assert [first_page["items"][0]["id"], second_page["items"][0]["id"]] == [
            chats[1]["id"],
            chats[0]["id"],
        ]
        assert second_page["next_cursor"] is None
        assert [item["id"] for item in client.get(base + "/quality/runs").json()["items"]] == [
            quality["id"]
        ]
        for cid in chapters:
            assert client.get(base + f"/chapters/{cid}").json()["content"] == ""
        continued = chat(
            client, base, project_id, "进程重启后，请承接原来的两轮讨论。", "after-restart"
        )
        conversation = next(
            fragment
            for fragment in continued["context_snapshot"]["fragments"]
            if fragment["source_type"] == "conversation"
        )
        remembered = json.loads(conversation["content"])
        assert [turn["author"] for turn in remembered] == instructions
        assert [turn["assistant"] for turn in remembered] == [
            item["result"]["reply"] for item in chats
        ]
        # Reading history and continuing ordinary chat must not restart a paused quality job.
        assert client.get(quality_url).json() == quality
