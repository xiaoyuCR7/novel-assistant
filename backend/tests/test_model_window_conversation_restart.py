"""Long conversation continuity across actual process death within the model window."""

import json

from test_conversation_process_restart import finished, running_backend


def send_chat(client, base, project_id, text, key, chapter_id=None):
    settings = client.get("/api/v1/settings/model").json()
    command = {
        "project_id": project_id,
        "task_type": "chat",
        "instructions": text,
        "token_budget": settings["context_capacity"] - settings["output_token_budget"],
    }
    if chapter_id:
        command.update(
            chapter_id=chapter_id,
            expected_revision=client.get(base + f"/chapters/{chapter_id}").json()["revision"],
        )
    response = client.post(base + "/ai/jobs", headers={"Idempotency-Key": key}, json=command)
    assert response.status_code == 202, response.text
    job = finished(client, response.json()["status_url"])
    assert job["status"] == "succeeded", job
    assert job["token_budget"] == command["token_budget"]
    assert job["result"]["stage_order"] == ["chat"]
    assert set(job["result"]["execution"]["stages"]) == {"chat"}
    return job


def remembered_turns(job):
    conversation = next(
        fragment
        for fragment in job["context_snapshot"]["fragments"]
        if fragment["source_type"] == "conversation"
    )
    return json.loads(conversation["content"])


def test_restart_retains_all_uncompressed_turns_and_uses_changed_model_window(tmp_path):
    root = tmp_path / "long-conversation-vault"
    texts = [
        "第一轮，首部暗号是北门铜铃。\n"
        + "雨幕沿钟楼屋檐落下，邮差等待守钟人的来信。" * 120
        + "\n第一轮尾部暗号是长桥晚灯，必须完整保留。",
        *[
            f"第{index + 2}轮："
            + "请保留本轮正文讨论，承接北门铜铃与长桥晚灯的伏笔。" * 24
            + f"第{index + 2}轮独立结尾。"
            for index in range(8)
        ],
    ]
    assert len(texts) > 8 and len(texts[0]) > 2000 and sum(map(len, texts)) > 6000
    with running_backend(root) as (client, first_pid):
        configured = client.put(
            "/api/v1/settings/model",
            json={"mode": "demo", "context_capacity": 32768, "output_token_budget": 4096},
        )
        assert configured.status_code == 200, configured.text
        project = client.post("/api/v1/projects", json={"title": "长窗口重启验证"}).json()
        base = f"/api/v1/projects/{project['id']}"
        chapter = client.post(base + "/nodes", json={"kind": "chapter", "title": "独立章节"}).json()
        scoped = send_chat(
            client,
            base,
            project["id"],
            "章节专用消息，不应进入全书对话。",
            "chapter-only",
            chapter["id"],
        )
        history = [
            send_chat(client, base, project["id"], text, f"long-turn-{index}")
            for index, text in enumerate(texts)
        ]
        assert history[-1]["context_snapshot"]["execution_limits"]["context_capacity"] == 32768

    with running_backend(root) as (client, restarted_pid):
        assert restarted_pid != first_pid
        for old in [*history, scoped]:
            assert client.get(old["status_url"]).json() == old
        resumed = send_chat(client, base, project["id"], "重启后承接全部九轮讨论。", "restart-turn")
        remembered = remembered_turns(resumed)
        assert [turn["author"] for turn in remembered] == texts
        assert [turn["assistant"] for turn in remembered] == [
            job["result"]["reply"] for job in history
        ]
        assert scoped["instructions"] not in [turn["author"] for turn in remembered]
        assert resumed["context_snapshot"]["execution_limits"]["context_capacity"] == 32768

        changed = client.put(
            "/api/v1/settings/model",
            json={"mode": "demo", "context_capacity": 65536, "output_token_budget": 4096},
        )
        assert changed.status_code == 200, changed.text
        continued = send_chat(
            client, base, project["id"], "窗口变更后继续完整讨论。", "changed-window"
        )
        assert continued["token_budget"] == 61440
        assert continued["context_snapshot"]["execution_limits"]["context_capacity"] == 65536
        assert [turn["author"] for turn in remembered_turns(continued)] == [
            *texts,
            resumed["instructions"],
        ]
        assert [turn["assistant"] for turn in remembered_turns(continued)] == [
            *(job["result"]["reply"] for job in history),
            resumed["result"]["reply"],
        ]
        # New capacity applies to new work; old transcripts and snapshots stay intact.
        for old in [*history, scoped, resumed]:
            assert client.get(old["status_url"]).json() == old
