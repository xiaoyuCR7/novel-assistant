from novel_harness.services.continuity import ContinuityChecker, ContinuityInput


def test_conflicting_confirmed_facts_are_not_silently_last_write_wins():
    facts = [
        {
            "id": "a",
            "subject_entity_id": "hero",
            "predicate": "age",
            "value": 20,
            "status": "confirmed",
        },
        {
            "id": "b",
            "subject_entity_id": "hero",
            "predicate": "age",
            "value": 21,
            "status": "confirmed",
        },
    ]

    def check(items):
        return ContinuityChecker().check(
            ContinuityInput(
                draft="主角21岁",
                chapter_id="chapter",
                canon_facts=items,
                draft_fact_candidates=[
                    {"subject_entity_id": "hero", "predicate": "age", "value": 21}
                ],
            )
        )

    forward, reverse = check(facts), check(list(reversed(facts)))
    assert forward and reverse
    assert [finding.model_dump() for finding in forward] == [
        finding.model_dump() for finding in reverse
    ]
    assert forward[0].related_ids == ["a"]
