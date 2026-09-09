import json

from job_helpers import complete_job, finish_job, run_job, save_chapter
from sqlalchemy import func, select

from novel_harness.ai.demo import DemoProvider
from novel_harness.db.models import Conflict, Project
from novel_harness.services import drift


def _database(client, project_id):
    return client.app.state.vault_registry.require(project_id).database


def test_local_content_rules_cover_hard_limits_and_heuristics(
    client, project, seeded_chapter
):
    url = f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}"
    body = "海港里的陌生人反复清点石头。" * 20 + "寄信人是守钟人。禁止套话"
    save_chapter(
        client,
        url,
        json={
            "content": body,
            "contract": {
                "purpose": "alpha beta",
                "forbidden_revelations": ["寄信人是守钟人"],
                "forbidden_phrases": ["禁止套话"],
            },
        },
    )
    database = _database(client, project["id"])
    with database.job_session_scope() as session:
        session.get(Project, project["id"]).premise = "gamma delta"
    with database.job_session_scope() as session:
        findings = drift.collect_local_findings(session, seeded_chapter)

    by_code = {item["code"]: item for item in findings}
    assert by_code["EARLY_REVELATION"]["severity"] == "severe"
    assert by_code["FORBIDDEN_PHRASE"]["severity"] == "warning"
    assert by_code["THEME_DISTANCE"]["severity"] == "info"
    assert by_code["PURPOSE_DISTANCE"]["severity"] == "warning"


def test_content_check_conflicts_are_job_revision_scoped_and_idempotent(
    client, project, seeded_chapter
):
    url = f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}"
    body = "林渡绕开钟楼。"
    document = save_chapter(client, url, json={"content": body, "contract": {}}).json()
    finding = {
        "dimension": "chapter_purpose",
        "severity": "warning",
        "message": "没有推进投递目标。",
        "evidence_quote": "绕开钟楼",
        "suggestion": "说明绕行如何服务于投递。",
        "reference_ids": ["project:core"],
        "start": body.index("绕开钟楼"),
        "end": body.index("绕开钟楼") + len("绕开钟楼"),
    }
    observation = {
        "kind": "fact",
        "entity_id": "entity:lin-du",
        "predicate": "location",
        "value": "钟楼外",
        "evidence_quote": "林渡绕开钟楼",
        "reference_ids": ["canon_fact:location"],
        "start": 0,
        "end": len("林渡绕开钟楼"),
    }
    context = {
        "hard_sources": [
            {
                "id": "canon_fact:location",
                "type": "canon_fact",
                "constraint": "hard",
                "content": json.dumps(
                    {
                        "subject_entity_id": "entity:lin-du",
                        "predicate": "location",
                        "value": "钟楼内",
                        "status": "confirmed",
                    }
                ),
            }
        ]
    }
    database = _database(client, project["id"])
    with database.job_session_scope() as session:
        first = drift.persist_content_check(
            session,
            seeded_chapter,
            "job-one",
            document["revision"],
            body,
            [finding],
            [observation],
            context,
        )
        second = drift.persist_content_check(
            session,
            seeded_chapter,
            "job-one",
            document["revision"],
            body,
            [finding],
            [observation],
            context,
        )
        count = session.scalar(select(func.count()).select_from(Conflict))
        blocked = drift.has_open_severe_content_conflicts(
            session, "job-one", document["revision"]
        )

    assert len(first) == 2
    assert [item.id for item in second] == [item.id for item in first]
    assert count == 2
    assert blocked is True
    assert {item.severity for item in first} == {"warning", "severe"}


def test_soft_or_ambiguous_continuity_difference_never_becomes_severe(
    client, project, seeded_chapter
):
    url = f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}"
    body = "林渡大概在钟楼外。"
    document = save_chapter(client, url, json={"content": body, "contract": {}}).json()
    observation = {
        "kind": "fact",
        "entity_id": "entity:lin-du",
        "predicate": "location",
        "value": "大概在钟楼外",
        "evidence_quote": body,
        "reference_ids": ["summary:old"],
        "start": 0,
        "end": len(body),
    }
    context = {
        "hard_sources": [],
        "recent_summaries": [
            {"id": "old", "constraint": "soft", "recap": "林渡在钟楼内。"}
        ],
    }
    database = _database(client, project["id"])
    with database.job_session_scope() as session:
        created = drift.persist_content_check(
            session,
            seeded_chapter,
            "job-soft",
            document["revision"],
            body,
            [],
            [observation],
            context,
        )

    assert all(item.severity != "severe" for item in created)


def test_severe_check_blocks_publish_and_resume_reuses_model_checkpoint(
    client, project, seeded_chapter
):
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
    calls = []

    class Counted(DemoProvider):
        def generate_structured(self, request, schema):
            calls.append(request.task)
            return super().generate_structured(request, schema)

    client.app.state.ai_provider = Counted()
    paused = complete_job(client, url).json()

    assert paused["status"] == "recovery_required"
    assert paused["recovery_reason"] == "content_review_required"
    assert client.get(url + "/summary").json() is None
    assert client.get(url).json()["status"] == "summary_pending"
    assert calls == ["chapter_summary"]

    rejected = client.post(
        base + f"/ai/jobs/{paused['id']}/resume",
        json={
            "expected_control_revision": paused["control_revision"],
            "confirm_unknown": False,
        },
        headers={"Idempotency-Key": "resume-before-content-review"},
    )
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "CONTENT_REVIEW_REQUIRED"
    assert calls == ["chapter_summary"]

    conflict = next(
        item
        for item in client.get(base + "/conflicts?status=open").json()
        if item["code"] == "EARLY_REVELATION"
    )
    decision = client.post(
        base + f"/conflicts/{conflict['id']}/decision",
        json={"decision": "dismiss"},
    )
    assert decision.status_code == 200
    receipt = client.post(
        base + f"/ai/jobs/{paused['id']}/resume",
        json={
            "expected_control_revision": paused["control_revision"],
            "confirm_unknown": False,
        },
        headers={"Idempotency-Key": "resume-after-content-review"},
    )
    completed = finish_job(client, receipt).json()

    assert completed["status"] == "succeeded"
    assert client.get(url + "/summary").json()["details"]["content_check"]["version"] == 1
    assert calls == ["chapter_summary"]


def test_author_summary_edit_preserves_internal_content_check_metadata(
    client, project, seeded_chapter
):
    url = f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}"
    save_chapter(client, url, json={"content": "林渡把蓝色信封留在钟楼。", "contract": {}})
    result = complete_job(client, url)
    assert result.json()["status"] == "succeeded"
    summary = client.get(url + "/summary").json()
    editable = {
        key: summary["details"][key]
        for key in (
            "recap",
            "plot_changes",
            "character_states",
            "knowledge_boundaries",
            "world_changes",
            "open_threads",
            "end_state",
            "fact_candidates",
        )
    }

    response = client.patch(
        url + "/summary",
        json={
            "summary_id": summary["id"],
            "revision": summary["revision"],
            "recap": "作者修订后的总结",
            "details": editable,
        },
    )

    assert response.status_code == 200
    assert response.json()["details"]["content_check"] == summary["details"]["content_check"]


def test_content_check_audit_text_never_enters_ledger_search_or_writing_context(
    client, project, seeded_chapter
):
    marker = "INTERNAL-CHECK-SUGGESTION-DO-NOT-RECALL"

    class FindingProvider(DemoProvider):
        def generate_structured(self, request, schema):
            result = super().generate_structured(request, schema)
            if request.task == "chapter_summary":
                quote = request.user_prompt[:4]
                result.data["content_findings"] = [
                    {
                        "dimension": "theme_alignment",
                        "severity": "warning",
                        "message": marker,
                        "evidence_quote": quote,
                        "suggestion": marker,
                        "reference_ids": [],
                    }
                ]
            return result

    client.app.state.ai_provider = FindingProvider()
    base = f"/api/v1/projects/{project['id']}"
    chapter_url = base + f"/chapters/{seeded_chapter}"
    save_chapter(client, chapter_url, json={"content": "林渡把信留在钟楼。", "contract": {}})
    assert complete_job(client, chapter_url).json()["status"] == "succeeded"

    vault = client.app.state.vault_registry.require(project["id"])
    assert marker not in (vault.root / "rag/chapter-continuity-ledger.md").read_text(
        encoding="utf-8"
    )
    assert not any(
        item["type"] == "summary"
        for item in client.get(base + "/library/search", params={"q": marker}).json()["items"]
    )

    second = client.post(
        base + "/nodes", json={"kind": "chapter", "title": "第二章", "order_index": 2}
    ).json()
    job = run_job(
        client,
        base + "/ai/jobs",
        json={
            "project_id": project["id"],
            "chapter_id": second["id"],
            "task_type": "draft",
            "token_budget": 12_000,
        },
    ).json()
    assert marker not in json.dumps(job["context_snapshot"], ensure_ascii=False)
