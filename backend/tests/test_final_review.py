from job_helpers import run_job

from novel_harness.ai.demo import DemoProvider


def test_changed_candidate_gets_one_combined_final_review_without_rewrite_loop(
    client, project, seeded_chapter
):
    calls = []

    class Provider(DemoProvider):
        def generate_structured(self, request, schema):
            calls.append((request.task, request.user_prompt, request.developer_instruction))
            result = super().generate_structured(request, schema)
            if request.task == "style_review":
                result.data["issues"] = [
                    {
                        "code": "style",
                        "severity": "warning",
                        "message": "初稿需要保守修复",
                        "evidence": [request.user_prompt[:8]],
                        "related_entity_ids": [],
                    }
                ]
            if request.task == "final_review":
                result.data["issues"] = [
                    {
                        "code": "final",
                        "severity": "warning",
                        "message": "请作者确认",
                        "evidence": [request.user_prompt[:8]],
                        "related_entity_ids": [],
                    }
                ]
            return result

    client.app.state.ai_provider = Provider()
    result = run_job(
        client,
        f"/api/v1/projects/{project['id']}/ai/jobs",
        json={
            "project_id": project["id"],
            "chapter_id": seeded_chapter,
            "task_type": "full_chapter",
        },
    ).json()
    assert result["status"] == "succeeded"
    assert calls[-1][0:2] == ("final_review", result["result"]["candidate_text"])
    assert "连续性" in calls[-1][2] and "文风" in calls[-1][2]
    assert sum(task == "final_review" for task, _, _ in calls) == 1
    assert result["result"]["stage_order"] == [
        "plan",
        "draft",
        "continuity_review",
        "style_review",
        "resolve",
        "rewrite",
        "final_review",
    ]
    assert result["accepted_version_id"] is None
    assert result["result"]["final_review"]["issues"]
