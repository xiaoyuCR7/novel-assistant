"""Manual, small real-provider audit. Never run as part of the offline test suite.

Run from backend: .venv/Scripts/python.exe scripts/smoke_deepseek.py --label baseline
The key is read with getpass, saved only by the application's DPAPI settings API,
and removed in finally. Uses only a new synthetic novel in a separate data root.
"""

import argparse
import getpass
import io
import json
import os
import subprocess
import sys
import time
import zipfile
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", default="baseline", choices=["baseline", "verified"])
    parser.add_argument("--port", default=8011, type=int)
    parser.add_argument("--summary-only", action="store_true",
                        help="Test summary with a tiny fixed manuscript; generate no prose")
    parser.add_argument("--review-only", action="store_true",
                        help="Review a tiny fixed manuscript and stop without generating prose")
    args = parser.parse_args()
    key = getpass.getpass("DeepSeek API key (hidden): ").strip()
    if not key:
        raise SystemExit("A key is required")
    folder = ROOT / ".verification" / f"deepseek-{args.label}-{uuid4().hex[:8]}"
    folder.mkdir(parents=True)
    data = folder / "data"
    report = {"model": "deepseek-v4-flash", "label": args.label, "checks": {}, "jobs": []}
    settings = dict(
        mode="api", base_url="https://api.deepseek.com", model="deepseek-v4-flash",
        external_consent=True, output_token_budget=2048, context_capacity=32768,
        deadline_seconds=90, thinking_mode="disabled", output_parameter="max_tokens",
    )
    local = httpx.Client(base_url=f"http://127.0.0.1:{args.port}", timeout=30, trust_env=False)
    remote = httpx.Client(
        base_url="https://api.deepseek.com", timeout=20, trust_env=False,
        headers={"Authorization": "Bearer " + key}, follow_redirects=False,
    )

    def balance():
        response = remote.get("/user/balance")
        response.raise_for_status()
        items = response.json()["balance_infos"]
        return Decimal(next(item["total_balance"] for item in items if item["currency"] == "CNY"))

    def api(method, path, *, status=200, **kwargs):
        response = local.request(method, "/api/v1" + path, **kwargs)
        assert response.status_code == status, (method, path, response.status_code)
        assert key not in response.text
        return response.json()

    initial = balance()
    report["balance_before_cny"] = str(initial)
    print("Initial CNY balance:", initial, "Evidence:", folder, flush=True)
    env = {**os.environ, "PYTHONPATH": str(BACKEND / "src"),
           "NOVEL_DATA_DIR": str(data), "NOVEL_STATIC_DIR": str(BACKEND / "static"),
           "NOVEL_AI_PROVIDER": "demo", "NOVEL_LOCAL_EMBEDDING_MODEL": ""}
    log = (folder / "server.log").open("w", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "novel_harness.main:app", "--host", "127.0.0.1",
         "--port", str(args.port), "--no-access-log"],
        cwd=BACKEND, env=env, stdout=log, stderr=log,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    active = []
    try:
        for _ in range(100):
            if process.poll() is not None:
                raise RuntimeError("Isolated server failed to start; inspect server.log")
            try:
                if api("GET", "/health")["status"] == "ok":
                    break
            except httpx.ConnectError:
                time.sleep(0.2)
        else:
            raise RuntimeError("Server startup timeout")
        assert local.get("/").status_code == 200
        report["checks"]["full_app_static_and_api"] = True
        public = api("PUT", "/settings/model", json={**settings, "api_key": key})
        assert public["has_api_key"] and "api_key" not in public
        report["checks"]["settings_redacted"] = True
        api("POST", "/settings/model/test")
        report["checks"]["connection_test"] = True
        project = api("POST", "/projects", status=201, json={
            "title": "DeepSeek 验收样本", "premise": "邮差在雨夜收到一封写给明天的信。",
            "genre": "悬疑", "target_words": 1000, "daily_goal": 100,
        })
        project_id = project["id"]
        base = f"/projects/{project_id}"
        report["project_id"] = project_id
        chapter = api("POST", base + "/nodes", status=201, json={
            "kind": "chapter", "title": "雨夜来信", "order_index": 1, "target_words": 120,
        })
        chapter_path = base + "/chapters/" + chapter["id"]
        document = api("GET", chapter_path)
        document = api("PUT", chapter_path, json={
            "revision": document["revision"], "content": "林舟收起湿伞，发现门缝里塞着一封信。",
            "contract": {"purpose": "发现异常日期并决定保留信件", "target_words": 120,
                         "forbidden_revelations": ["寄信人的真实身份"]},
        })

        def budget():
            spent = initial - balance()
            assert spent < Decimal("0.35"), "Budget reserve reached; no more jobs"

        def finish(receipt, name):
            path = base + "/ai/jobs/" + receipt["id"]
            active.append(path)
            deadline = time.monotonic() + 420
            seen_preview = False
            while time.monotonic() < deadline:
                job = api("GET", path)
                if job["status"] not in {"queued", "running", "cancel_requested"}:
                    active.remove(path)
                    report["jobs"].append({"name": name, "job": job, "preview_seen": seen_preview})
                    print(name, job["status"], "error:", job.get("error_code"), flush=True)
                    (folder / "evidence.json").write_text(
                        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    return job
                seen_preview |= bool(api("GET", path + "/preview").get("text"))
                time.sleep(0.5)
            raise RuntimeError("Job deadline reached; cancelling without retry")

        def writing(task, instructions):
            budget()
            command = dict(project_id=project_id, chapter_id=chapter["id"], task_type=task,
                           instructions=instructions, token_budget=10000,
                           expected_revision=api("GET", chapter_path)["revision"])
            assert api("POST", base + "/ai/jobs/preflight", json=command)["can_fit"]
            idem = {"Idempotency-Key": uuid4().hex}
            receipt = api("POST", base + "/ai/jobs", status=202, json=command, headers=idem)
            duplicate = api("POST", base + "/ai/jobs", status=202, json=command, headers=idem)
            assert receipt["id"] == duplicate["id"]
            report["checks"]["idempotent_receipt"] = True
            return finish(receipt, task)

        if args.summary_only or args.review_only:
            saved = api("PUT", chapter_path, json={
                "revision": document["revision"], "contract": document["contract"],
                "content": "林舟收起湿伞，发现门缝里塞着一封信。"
                "他借着灯光看清邮戳，上面印着明天的日期。"
                "林舟没有拆信，将它放进抽屉，锁好后把钥匙收入口袋。",
            })
            report["checks"]["summary_only_no_prose_generation"] = True
            if args.review_only:
                reviewed = writing("review", "检查当前正文的连续性，只报告有原文证据的问题。")
                report["passed"] = reviewed["status"] == "succeeded"
                report["checks"]["review_only_no_prose_generation"] = True
                return 0 if report["passed"] else 1
        else:
            chat = writing("chat", "请用一句不超过30字的话给出雨夜来信的悬念建议，不写正文。")
            assert chat["status"] == "succeeded", "Chat failed"
            job = writing("full_chapter", "只规划一个场景，正文限100至150个汉字，最多三段。"
                          "保持林舟的第三人称有限视角，发现信封的邮戳是明天，并把信收入抽屉。"
                          "不揭示寄信人，不添加额外设定。各阶段回答简洁。")
            assert job["status"] == "succeeded", "Full pipeline failed"
            candidate = job["result"]["candidate_text"]
            report["candidate_characters"] = len(candidate)
            assert api("GET", chapter_path)["content"] == document["content"]
            report["checks"]["candidate_does_not_overwrite"] = True
            version = api("POST", base + "/ai/jobs/" + job["id"] + "/accept", status=201,
                          json={"confirmed": True})
            again = api("POST", base + "/ai/jobs/" + job["id"] + "/accept", status=201,
                        json={"confirmed": True})
            assert version["id"] == again["id"]
            saved = api("GET", chapter_path)
            assert saved["content"] == candidate
            report["checks"]["accept_once_versioned"] = True
        assert local.put("/api/v1" + chapter_path, json={
            "revision": document["revision"], "content": "stale overwrite", "contract": {},
        }).status_code == 409
        report["checks"]["stale_write_rejected"] = True
        budget()
        receipt = api("POST", chapter_path + "/complete", status=202,
                      headers={"Idempotency-Key": uuid4().hex},
                      json={"expected_revision": saved["revision"]})
        summary_job = finish(receipt, "chapter_summary")
        assert summary_job["status"] == "succeeded", "Chapter summary failed"
        summary = api("GET", chapter_path + "/summary")
        assert summary and not summary["ledger_pending"]
        report["checks"]["summary_and_ledger"] = True
        export = local.get("/api/v1" + base + "/export")
        assert export.status_code == 200
        with zipfile.ZipFile(io.BytesIO(export.content)) as archive:
            assert all(".settings" not in name for name in archive.namelist())
            assert all(key.encode() not in archive.read(name) for name in archive.namelist())
        report["checks"]["backup_has_no_key"] = True
        report["passed"] = True
    except Exception as exc:
        # Do not format exceptions from remote libraries or include request objects.
        report["passed"] = False
        report["failure_type"] = type(exc).__name__
        print("Audit stopped:", type(exc).__name__, flush=True)
    finally:
        for path in active:
            try:
                api("POST", path + "/cancel")
            except Exception:
                pass
        try:
            api("PUT", "/settings/model", json={"mode": "demo", "clear_api_key": True})
            report["checks"]["key_removed"] = not api("GET", "/settings/model")["has_api_key"]
        except Exception:
            # The isolated settings file belongs to this run only.
            (data / ".settings" / "model.json").unlink(missing_ok=True)
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        log.close()
        try:
            final = balance()
            report["balance_after_cny"] = str(final)
            report["spent_cny"] = str(initial - final)
        except Exception:
            report["balance_after_cny"] = "unavailable"
        local.close()
        remote.close()
        encoded = json.dumps(report, ensure_ascii=False, indent=2)
        assert key not in encoded
        (folder / "evidence.json").write_text(encoded, encoding="utf-8")
        print("Passed:", report.get("passed"), "Spent CNY:", report.get("spent_cny"), flush=True)
        print("Evidence:", folder / "evidence.json", flush=True)
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
