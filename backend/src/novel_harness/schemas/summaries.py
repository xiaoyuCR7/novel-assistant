from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from novel_harness.schemas.memory import MemoryCandidateList

SummaryItem = Annotated[str, Field(min_length=1, max_length=1000)]
ContentDimension = Literal[
    "theme_alignment",
    "chapter_purpose",
    "plot_progression",
    "character_motivation",
    "pov_and_voice",
    "continuity",
    "pacing_and_focus",
]
ObservationKind = Literal["pov", "action", "fact", "timeline"]
JsonScalar = str | int | float | bool | None


class ContentFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    dimension: ContentDimension
    severity: Literal["info", "warning"]
    message: str = Field(min_length=1, max_length=500)
    evidence_quote: str = Field(min_length=1, max_length=1000)
    suggestion: str = Field(min_length=1, max_length=1000)
    reference_ids: list[str] = Field(default_factory=list, max_length=8)


class ContentObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: ObservationKind
    entity_id: str | None = Field(default=None, max_length=160)
    predicate: str = Field(min_length=1, max_length=160)
    value: JsonScalar
    evidence_quote: str = Field(min_length=1, max_length=1000)
    reference_ids: list[str] = Field(default_factory=list, max_length=8)


class SummaryContent(BaseModel):
    recap: str = Field(min_length=1, max_length=8000)
    plot_changes: list[SummaryItem] = Field(max_length=32)
    character_states: list[SummaryItem] = Field(max_length=32)
    knowledge_boundaries: list[SummaryItem] = Field(max_length=32)
    world_changes: list[SummaryItem] = Field(max_length=32)
    open_threads: list[SummaryItem] = Field(max_length=32)
    end_state: str = Field(max_length=2000)
    fact_candidates: list[SummaryItem] = Field(max_length=32)


class SummaryEvidence(BaseModel):
    field: Literal['recap', 'plot_changes', 'character_states', 'knowledge_boundaries',
                   'world_changes', 'open_threads', 'end_state', 'fact_candidates']
    index: int = Field(ge=0)
    quote: str = Field(min_length=1, max_length=1000)


class GeneratedSummary(SummaryContent):
    """New model output only; stored legacy/author summaries remain readable."""
    model_config = ConfigDict(extra="forbid", strict=True)

    evidence: list[SummaryEvidence] = Field(min_length=1, max_length=200)
    content_findings: list[ContentFinding] = Field(max_length=20)
    content_observations: list[ContentObservation] = Field(max_length=60)
    memory_candidates: MemoryCandidateList | None = None


class SummaryPatch(BaseModel):
    summary_id: str = Field(min_length=1, max_length=64)
    revision: int = Field(ge=1)
    recap: str = Field(min_length=1, max_length=100_000)
    details: SummaryContent | None = None
