"""Deterministic continuity checks with stable, auditable finding codes."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ContinuityInput(BaseModel):
    draft: str
    chapter_id: str
    current_node_order: int = 0
    actual_pov_entity_id: str | None = None
    current_timeline_sort_key: int | None = None
    previous_timeline_sort_key: int | None = None
    contract: dict[str, Any] = Field(default_factory=dict)
    canon_facts: list[dict[str, Any]] = Field(default_factory=list)
    draft_fact_candidates: list[dict[str, Any]] = Field(default_factory=list)
    entities: list[dict[str, Any]] = Field(default_factory=list)
    acting_entity_ids: list[str] = Field(default_factory=list)
    plot_threads: list[dict[str, Any]] = Field(default_factory=list)


class ConflictFinding(BaseModel):
    code: str
    severity: Literal["warning", "error"]
    message: str
    evidence: list[str]
    source: Literal["rule", "ai"] = "rule"
    related_ids: list[str] = Field(default_factory=list)


class ContinuityChecker:
    def check(self, payload: ContinuityInput) -> list[ConflictFinding]:
        findings: list[ConflictFinding] = []
        findings.extend(self._canon_conflicts(payload))
        findings.extend(self._character_state_conflicts(payload))

        if (
            payload.current_timeline_sort_key is not None
            and payload.previous_timeline_sort_key is not None
            and payload.current_timeline_sort_key < payload.previous_timeline_sort_key
        ):
            findings.append(
                ConflictFinding(
                    code="TIMELINE_ORDER",
                    severity="error",
                    message="当前事件发生时间早于已确定的前序事件。",
                    evidence=[
                        f"current={payload.current_timeline_sort_key}",
                        f"previous={payload.previous_timeline_sort_key}",
                    ],
                )
            )

        expected_pov = payload.contract.get("pov_entity_id")
        if (
            expected_pov
            and payload.actual_pov_entity_id is not None
            and payload.actual_pov_entity_id != expected_pov
        ):
            findings.append(
                ConflictFinding(
                    code="POV_MISMATCH",
                    severity="error",
                    message="实际视角人物与章节契约不一致。",
                    evidence=[f"expected={expected_pov}", f"actual={payload.actual_pov_entity_id}"],
                    related_ids=[expected_pov],
                )
            )

        for phrase in payload.contract.get("forbidden_phrases", []):
            if phrase and phrase in payload.draft:
                findings.append(
                    ConflictFinding(
                        code="FORBIDDEN_PHRASE",
                        severity="warning",
                        message=f"正文使用了禁用表达：{phrase}",
                        evidence=[phrase],
                    )
                )

        for thread in payload.plot_threads:
            due_order = thread.get("due_order")
            if (
                thread.get("kind") == "foreshadowing"
                and due_order is not None
                and due_order < payload.current_node_order
                and thread.get("status") not in {"resolved", "paid_off", "cancelled"}
            ):
                findings.append(
                    ConflictFinding(
                        code="FORESHADOWING_OVERDUE",
                        severity="warning",
                        message=f"伏笔“{thread.get('title', '未命名')}”已经超过计划回收章节。",
                        evidence=[
                            f"due_order={due_order}",
                            f"current={payload.current_node_order}",
                        ],
                        related_ids=[str(thread.get("id", ""))],
                    )
                )
        return findings

    def _canon_conflicts(self, payload: ContinuityInput) -> list[ConflictFinding]:
        confirmed = {}
        for fact in payload.canon_facts:
            if fact.get("status") == "confirmed":
                key = (fact.get("subject_entity_id"), fact.get("predicate"))
                confirmed.setdefault(key, []).append(fact)
        findings: list[ConflictFinding] = []
        for candidate in payload.draft_fact_candidates:
            key = (candidate.get("subject_entity_id"), candidate.get("predicate"))
            for fact in sorted(confirmed.get(key, []), key=lambda item: str(item.get('id', ''))):
                if fact.get("value") == candidate.get("value"):
                    continue
                findings.append(
                    ConflictFinding(
                        code="CANON_CONTRADICTION",
                        severity="error",
                        message="草稿候选事实与已确认事实冲突。",
                        evidence=[
                            f"confirmed={fact.get('value')}",
                            f"draft={candidate.get('value')}",
                        ],
                        related_ids=[str(fact.get("id", ""))],
                    )
                )
        return findings

    def _character_state_conflicts(self, payload: ContinuityInput) -> list[ConflictFinding]:
        entities = {str(entity.get("id")): entity for entity in payload.entities}
        findings: list[ConflictFinding] = []
        for entity_id in payload.acting_entity_ids:
            entity = entities.get(entity_id)
            if entity and entity.get("state", {}).get("alive") is False:
                entity_name = entity.get("name", entity_id)
                findings.append(
                    ConflictFinding(
                        code="CHARACTER_STATE",
                        severity="error",
                        message=f"已死亡人物“{entity_name}”在当前草稿中执行了行动。",
                        evidence=[f"entity={entity_id}", "alive=false"],
                        related_ids=[entity_id],
                    )
                )
        return findings
