"""Semantic review validation shared by checkpoints and domain publication."""

from novel_harness.ai.base import ProviderExecutionError


def validate_review(review, draft, references, valid_entities):
    sources = [draft, *references]
    for issue in review["issues"]:
        if not all(
            quote.strip() and any(quote in text for text in sources) for quote in issue["evidence"]
        ):
            raise ProviderExecutionError(
                "AI 审校证据无法在正文或本次参考中核对。",
                outcome="known",
                code="INVALID_REVIEW_EVIDENCE",
            )
        if not set(issue["related_entity_ids"]).issubset(valid_entities):
            raise ProviderExecutionError(
                "AI 审校引用了不可用人物。",
                outcome="known",
                code="INVALID_REVIEW_ENTITY",
            )
    for observation in review.get("observations", []):
        if not observation["evidence"].strip() or observation["evidence"] not in draft:
            raise ProviderExecutionError(
                "AI 审校观察必须引用本次正文原句。",
                outcome="known",
                code="INVALID_REVIEW_OBSERVATION",
            )
        if observation["entity_id"] and observation["entity_id"] not in valid_entities:
            raise ProviderExecutionError(
                "AI 审校观察引用了不可用人物。",
                outcome="known",
                code="INVALID_REVIEW_ENTITY",
            )
    for proposal in review.get("memory_candidates") or []:
        evidence = proposal["evidence"]
        if draft[evidence["start"] : evidence["end"]] != evidence["quote"]:
            raise ProviderExecutionError(
                "AI 记忆候选必须引用本次正文的精确位置。",
                outcome="known",
                code="INVALID_MEMORY_EVIDENCE",
            )
