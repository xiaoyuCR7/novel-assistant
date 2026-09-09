from job_helpers import complete_job, complete_summary, finish_job, run_job, save_chapter

from novel_harness.ai.base import ProviderExecutionError


def test_complete_is_idempotent_and_ledger_is_searchable(client, project, seeded_chapter):
    base = f"/api/v1/projects/{project['id']}"
    chapter_url = base + f"/chapters/{seeded_chapter}"
    save_chapter(client, chapter_url, json={"content": "林渡把蓝色信封留在钟楼。", "contract": {}})
    result = complete_summary(client, chapter_url)
    assert result.status_code == 200
    summary = result.json()
    assert summary["origin"] == "ai_generated"
    assert client.get(chapter_url).json()["status"] == "completed"
    assert complete_summary(client, chapter_url).json()["id"] == summary["id"]
    vault = client.app.state.vault_registry.require(project["id"])
    ledger = vault.root / "rag/chapter-continuity-ledger.md"
    assert "AI 总结" in ledger.read_text(encoding="utf-8")
    assert seeded_chapter in ledger.read_text(encoding="utf-8")
    found = client.get(base + "/library/search", params={"q": "蓝色信封"}).json()["items"]
    assert any(i["type"] == "summary" and i["constraint"] == "soft" for i in found)
    save_chapter(client, chapter_url, json={"content": "正文改写，信封被烧毁。", "contract": {}})
    assert client.get(chapter_url + "/summary").json()["status"] == "stale"
    assert not any(
        i["type"] == "summary"
        for i in client.get(base + "/library/search", params={"q": "蓝色信封"}).json()["items"]
    )


def test_summary_failure_preserves_version_and_can_retry(client, project, seeded_chapter):
    class Failing:
        def generate_structured(self, request, schema):
            raise ProviderExecutionError("offline")

    url = f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}"
    save_chapter(client, url, json={"content": "必须保留的正文。", "contract": {}})
    provider = client.app.state.ai_provider
    client.app.state.ai_provider = Failing()
    response = complete_job(client, url)
    assert response.json()["status"] == "failed"
    assert client.get(url).json()["content"] == "必须保留的正文。"
    assert client.get(url).json()["status"] == "summary_pending"
    assert len(client.get(url + "/versions").json()) == 1
    client.app.state.ai_provider = provider
    receipt = client.post(
        url + "/summary/retry",
        headers={"Idempotency-Key": "retry"},
        json={
            "job_id": response.json()["id"],
            "expected_control_revision": response.json()["control_revision"],
            "confirm_unknown": False,
        },
    )
    assert finish_job(client, receipt).json()["status"] == "succeeded"
    assert client.get(url).json()["status"] == "completed"


def test_known_summary_evidence_failure_gets_one_durable_repair(
    client, project, seeded_chapter
):
    from novel_harness.ai.demo import DemoProvider

    calls = 0

    class RepairingProvider(DemoProvider):
        def generate_structured(self, request, schema):
            nonlocal calls
            calls += 1
            result = super().generate_structured(request, schema)
            if calls == 1:
                result.data["plot_changes"] = ["没有来源证据的变化"]
            return result

    client.app.state.ai_provider = RepairingProvider()
    url = f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}"
    save_chapter(client, url, json={"content": "林渡在北塔交出钥匙。", "contract": {}})

    job = complete_job(client, url).json()

    assert job["status"] == "succeeded"
    assert calls == 2
    stages = job["result"]["execution"]["stages"]
    assert list(stages) == ["chapter_summary", "chapter_summary.evidence_repair"]
    assert stages["chapter_summary"]["status"] == "failed"
    assert stages["chapter_summary.evidence_repair"]["status"] == "succeeded"
    assert client.get(url + "/summary").json()["status"] == "valid"


def test_next_chapter_uses_prior_summaries_not_future_text(client, project, seeded_chapter):
    base = f"/api/v1/projects/{project['id']}"
    first = base + f"/chapters/{seeded_chapter}"
    save_chapter(client, first, json={"content": "林渡在钟楼等待。", "contract": {}})
    complete_summary(client, first)
    second = client.post(
        base + "/nodes", json={"kind": "chapter", "title": "第二章", "order_index": 2}
    ).json()
    result = run_job(
        client,
        base + "/ai/jobs",
        json={
            "project_id": project["id"],
            "chapter_id": second["id"],
            "task_type": "draft",
            "token_budget": 12000,
        },
    ).json()
    fragments = result["context_snapshot"]["fragments"]
    assert any(f["source_type"] == "chapter_summary" and not f["hard"] for f in fragments)
    assert not any(f["source_type"] == "chapter_manuscript" for f in fragments)


def test_ledger_failure_keeps_summary_pending_and_retry_repairs_projection(
    client, project, seeded_chapter, monkeypatch
):
    from novel_harness.services import chapter_summaries

    url = f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}"
    save_chapter(client, url, json={"content": "已持久化的章节"})
    original = chapter_summaries.write_ledger

    def fail(session):
        raise OSError("test disk failure")

    monkeypatch.setattr(chapter_summaries, "write_ledger", fail)
    response = complete_job(client, url)
    assert response.json()["status"] == "recovery_required"
    assert response.json()["recovery_reason"] == "ledger_pending"
    assert client.get(url).json()["status"] == "summary_pending"
    summary_id = client.get(url + "/summary").json()["id"]
    monkeypatch.setattr(chapter_summaries, "write_ledger", original)
    assert client.post(url + "/summary/repair-ledger").json()["summary"]["id"] == summary_id
    assert len(client.get(url + "/versions").json()) == 1


def test_edit_during_summary_generation_rejects_old_result(client, project, seeded_chapter):
    from novel_harness.ai.demo import DemoProvider
    from novel_harness.services.versions import update_document

    vault = client.app.state.vault_registry.require(project["id"])
    url = f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}"
    save_chapter(client, url, json={"content": "旧稿"})

    class EditingProvider(DemoProvider):
        def generate_structured(self, request, schema):
            with vault.database.session_scope() as session:
                update_document(session, seeded_chapter, "生成时改写的新稿", {})
            return super().generate_structured(request, schema)

    client.app.state.ai_provider = EditingProvider()
    result = complete_job(client, url).json()
    assert result["status"] == "recovery_required"
    assert result["recovery_reason"] == "SOURCE_CHANGED"
    assert client.get(url).json()["content"] == "生成时改写的新稿"
    assert client.get(url).json()["status"] == "drafting"
    assert client.get(url + "/summary").json() is None
