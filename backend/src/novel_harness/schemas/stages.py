"""Validated model outputs; they describe candidates, never authorize writes."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from novel_harness.schemas.memory import MemoryCandidateList


class StageOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Scene(StageOutput):
    title: str
    goal: str
    obstacle: str
    turn: str
    state_change: str


class PlanOutput(StageOutput):
    scenes: list[Scene] = Field(min_length=1, max_length=50)


class ReviewIssue(StageOutput):
    code: str = Field(min_length=1, max_length=80)
    severity: Literal["info", "warning", "error", "severe"]
    message: str = Field(min_length=1)
    evidence: list[str] = Field(min_length=1)
    related_entity_ids: list[str] = Field(default_factory=list)


class Observation(StageOutput):
    kind: Literal["pov", "action", "fact", "timeline"]
    evidence: str = Field(min_length=1)
    entity_id: str | None = None
    predicate: str = ""
    value: Any = None


class ReviewOutput(StageOutput):
    issues: list[ReviewIssue] = Field(max_length=100)
    summary: str = ""
    observations: list[Observation] = Field(default_factory=list, max_length=100)
    memory_candidates: MemoryCandidateList | None = None


class ResolutionOption(StageOutput):
    mode: Literal["conservative", "balanced", "radical"]
    benefit: str
    risk: str
    title: str = ""
    description: str = ""
    ripple_effects: list[str] = Field(default_factory=list)
    affected_entity_ids: list[str] = Field(default_factory=list)


class ResolutionOutput(StageOutput):
    options: list[ResolutionOption] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def distinct_modes(self):
        if {option.mode for option in self.options} != {"conservative", "balanced", "radical"}:
            raise ValueError("Each resolution mode is required exactly once")
        return self
