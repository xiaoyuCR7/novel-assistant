from novel_harness.services.continuity import ContinuityChecker, ContinuityInput


def test_continuity_checker_reports_stable_codes_for_long_novel_conflicts():
    payload = ContinuityInput(
        draft="命运的齿轮转动起来。已经死去的秦婆推门走进来。",
        chapter_id="chapter-8",
        current_node_order=8,
        actual_pov_entity_id="outsider",
        current_timeline_sort_key=100,
        previous_timeline_sort_key=200,
        contract={
            "pov_entity_id": "lin-du",
            "forbidden_phrases": ["命运的齿轮"],
        },
        canon_facts=[
            {
                "id": "fact-age",
                "subject_entity_id": "lin-du",
                "predicate": "age",
                "value": 24,
                "status": "confirmed",
            }
        ],
        draft_fact_candidates=[{"subject_entity_id": "lin-du", "predicate": "age", "value": 31}],
        entities=[{"id": "qin-po", "name": "秦婆", "state": {"alive": False}}],
        acting_entity_ids=["qin-po"],
        plot_threads=[
            {
                "id": "foreshadow-1",
                "kind": "foreshadowing",
                "title": "钟楼裂纹",
                "status": "planted",
                "due_order": 6,
            }
        ],
    )

    findings = ContinuityChecker().check(payload)
    codes = {finding.code for finding in findings}

    assert codes == {
        "CANON_CONTRADICTION",
        "CHARACTER_STATE",
        "TIMELINE_ORDER",
        "POV_MISMATCH",
        "FORBIDDEN_PHRASE",
        "FORESHADOWING_OVERDUE",
    }
    assert all(finding.severity in {"warning", "error"} for finding in findings)
    assert all(finding.evidence for finding in findings)
    assert all(finding.source == "rule" for finding in findings)
