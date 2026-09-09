import json
from pathlib import Path

from novel_harness.services.context import ContextAssembler, ContextFragment
from novel_harness.services.continuity import ContinuityChecker, ContinuityInput


def test_fixed_story_packet_preserves_hard_context_and_stable_conflicts():
    fixture = Path(__file__).parent / "fixtures" / "story_packet.json"
    packet = json.loads(fixture.read_text(encoding="utf-8"))
    context = ContextAssembler().pack(
        "draft",
        [
            ContextFragment(
                "chapter_contract", "chapter-4", "章节契约", 100, str(packet["contract"]), True
            ),
            ContextFragment(
                "canon_fact", "fact-age", "确认事实", 100, str(packet["canon_facts"][0]), True
            ),
        ],
        [ContextFragment("old-scene", "chapter-1", "较远章节", 10, "旧场景" * 500)],
        token_budget=20,
    )
    assert [item.source_type for item in context.fragments] == [
        "chapter_contract",
        "canon_fact",
    ]
    assert context.over_budget is True

    findings = ContinuityChecker().check(
        ContinuityInput(
            draft="命运的齿轮响了。秦婆推门而入。",
            chapter_id="chapter-4",
            current_node_order=4,
            actual_pov_entity_id="lin-du",
            contract=packet["contract"],
            canon_facts=packet["canon_facts"],
            draft_fact_candidates=[
                {"subject_entity_id": "lin-du", "predicate": "age", "value": 31}
            ],
            entities=packet["entities"],
            acting_entity_ids=["qin-po"],
            plot_threads=packet["plot_threads"],
        )
    )
    assert {item.code for item in findings} == {
        "CANON_CONTRADICTION",
        "CHARACTER_STATE",
        "FORBIDDEN_PHRASE",
        "FORESHADOWING_OVERDUE",
    }
