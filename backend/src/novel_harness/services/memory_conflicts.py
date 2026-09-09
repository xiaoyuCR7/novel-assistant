"""Deterministic conflicts only; free-form prose is never labelled proven contradiction."""

from collections import defaultdict
from itertools import combinations


def detect_memory_conflicts(styles, facts, positions):
    conflicts = []
    active = [s for s in styles if s["is_active"]]
    for first, second in combinations(active, 2):
        different = [
            key
            for key in first["config"].keys() & second["config"].keys()
            if key in {"pov", "distance", "rhythm", "tense"}
            and first["config"][key] != second["config"][key]
        ]
        if different:
            conflicts.append(
                {
                    "code": "STYLE_CONFLICT",
                    "ids": [first["id"], second["id"]],
                    "fields": sorted(different),
                    "requires_author_decision": True,
                    "message": "启用的风格方案有不同取值，请明确组合或停用其中一个。",
                }
            )
    groups = defaultdict(list)
    for fact in facts:
        if fact["status"] == "confirmed":
            groups[(fact["subject_entity_id"], fact["predicate"])].append(fact)
    for group in groups.values():
        for first, second in combinations(group, 2):

            def bounds(fact):
                start, end = fact.get("valid_from_node_id"), fact.get("valid_to_node_id")
                if (start and start not in positions) or (end and end not in positions):
                    return None
                return positions.get(start, -1), positions.get(end, float("inf"))

            a, b = bounds(first), bounds(second)
            if a and b and max(a[0], b[0]) <= min(a[1], b[1]) and first["value"] != second["value"]:
                conflicts.append(
                    {
                        "code": "CANON_OVERLAP",
                        "ids": [first["id"], second["id"]],
                        "requires_author_decision": True,
                        "message": "同主体、同属性在重叠范围内存在不同确认值，请作者核对。",
                    }
                )
    return conflicts
