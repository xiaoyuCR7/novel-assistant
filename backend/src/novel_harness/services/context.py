"""Token-budgeted context packing for long-form writing tasks."""

from __future__ import annotations

from dataclasses import dataclass, field


def estimate_tokens(text: str) -> int:
    # Conservative estimate for mixed CJK prose; not a model-specific tokenizer.
    cjk = sum(1 for char in text if "\u3400" <= char <= "\u9fff")
    return max(1, cjk * 2 + (len(text) - cjk + 3) // 4)


@dataclass(frozen=True, slots=True)
class ContextFragment:
    source_type: str
    source_id: str
    reason: str
    priority: int
    content: str
    hard: bool = False
    channel: str = "constraint"
    score: float = 0.0
    citation: dict = field(default_factory=dict)
    estimated_tokens: int = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "estimated_tokens", estimate_tokens(self.content))


@dataclass(frozen=True, slots=True)
class ContextPacket:
    task: str
    fragments: list[ContextFragment]
    token_budget: int
    total_estimated_tokens: int
    dropped_source_ids: list[str]
    over_budget: bool


class ContextAssembler:
    """Packs immutable hard constraints before relevance-ranked soft context."""

    def pack(
        self,
        task: str,
        hard_fragments: list[ContextFragment],
        soft_fragments: list[ContextFragment],
        token_budget: int,
    ) -> ContextPacket:
        selected = list(hard_fragments)
        total = sum(fragment.estimated_tokens for fragment in selected)
        dropped: list[str] = []

        for fragment in sorted(soft_fragments, key=lambda item: item.priority, reverse=True):
            if total + fragment.estimated_tokens <= token_budget:
                selected.append(fragment)
                total += fragment.estimated_tokens
            else:
                dropped.append(fragment.source_id)

        return ContextPacket(
            task=task,
            fragments=selected,
            token_budget=token_budget,
            total_estimated_tokens=total,
            dropped_source_ids=dropped,
            over_budget=total > token_budget,
        )
