"""Typed requests for author-managed temporal story memory."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema, model_validator

from novel_harness.schemas.imports import BoundedJson, Payload

EntityStateStatus = Literal["pending", "confirmed", "retracted"]
LegacyTransition = Literal["baseline", "retire"]


class EntityStateCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data: dict[str, Any] = Field(default_factory=dict)
    valid_from_node_id: str
    valid_to_node_id: str | None = None
    source_version_id: str | None = None
    status: EntityStateStatus = "confirmed"
    legacy_transition: LegacyTransition | None = None


class EntityStateEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=1)
    data: dict[str, Any] | None = None
    valid_from_node_id: str | None = None
    valid_to_node_id: str | None = None
    source_version_id: str | None = None
    status: EntityStateStatus | None = None
    legacy_transition: LegacyTransition | None = None


class CandidateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class MemoryEvidence(CandidateModel):
    quote: str = Field(min_length=1, max_length=4000)
    start: int = Field(ge=0, le=10_000_000)
    end: int = Field(gt=0, le=10_000_000)

    @model_validator(mode="after")
    def ordered_offsets(self):
        if self.end <= self.start:
            raise ValueError("evidence end must follow start")
        return self


class CanonCandidatePayload(CandidateModel):
    subject_entity_id: str | None = Field(default=None, min_length=1, max_length=64)
    predicate: str = Field(min_length=1, max_length=120)
    value: BoundedJson
    valid_from_node_id: str | None = Field(default=None, min_length=1, max_length=64)
    valid_to_node_id: str | None = Field(default=None, min_length=1, max_length=64)


class EntityStateCandidatePayload(CandidateModel):
    entity_id: str = Field(min_length=1, max_length=64)
    data: Payload
    valid_from_node_id: str = Field(min_length=1, max_length=64)
    valid_to_node_id: str | None = Field(default=None, min_length=1, max_length=64)
    legacy_transition: LegacyTransition | None = None


class TimelineCandidatePayload(CandidateModel):
    chapter_id: str | None = Field(default=None, min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=240)
    story_time: str = Field(default="", max_length=160)
    sort_key: int = 0
    description: str = Field(default="", max_length=16_000)


class PlotCandidatePayload(CandidateModel):
    kind: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=1, max_length=240)
    promise: str = Field(default="", max_length=16_000)
    start_node_id: str | None = Field(default=None, min_length=1, max_length=64)
    due_node_id: str | None = Field(default=None, min_length=1, max_length=64)
    payoff: str = Field(default="", max_length=16_000)


class CanonMemoryCandidateProposal(CandidateModel):
    kind: Literal["canon"]
    payload: CanonCandidatePayload
    evidence: MemoryEvidence


class EntityStateMemoryCandidateProposal(CandidateModel):
    kind: Literal["entity_state"]
    payload: EntityStateCandidatePayload
    evidence: MemoryEvidence


class TimelineMemoryCandidateProposal(CandidateModel):
    kind: Literal["timeline"]
    payload: TimelineCandidatePayload
    evidence: MemoryEvidence


class PlotMemoryCandidateProposal(CandidateModel):
    kind: Literal["plot"]
    payload: PlotCandidatePayload
    evidence: MemoryEvidence


MemoryCandidateProposal = Annotated[
    CanonMemoryCandidateProposal
    | EntityStateMemoryCandidateProposal
    | TimelineMemoryCandidateProposal
    | PlotMemoryCandidateProposal,
    Field(discriminator="kind"),
]

MemoryCandidatePayload = (
    CanonCandidatePayload
    | EntityStateCandidatePayload
    | TimelineCandidatePayload
    | PlotCandidatePayload
)
GeneratedMemoryCandidateStatus = Literal["pending", "confirmed", "rejected"]


class GeneratedMemoryCandidateEdit(CandidateModel):
    revision: int = Field(ge=1)
    payload: MemoryCandidatePayload | None = None
    evidence: MemoryEvidence | None = None

    @model_validator(mode="after")
    def has_edit(self):
        if self.payload is None and self.evidence is None:
            raise ValueError("payload or evidence is required")
        return self


class GeneratedMemoryCandidateDecision(CandidateModel):
    revision: int = Field(ge=1)

_PAYLOAD_SCHEMAS = [
    {
        "additionalProperties": False,
        "required": ["predicate", "value"],
        "properties": {
            "subject_entity_id": {},
            "predicate": {},
            "value": {},
            "valid_from_node_id": {},
            "valid_to_node_id": {},
        },
    },
    {
        "additionalProperties": False,
        "required": ["entity_id", "data", "valid_from_node_id"],
        "properties": {
            "entity_id": {},
            "data": {},
            "valid_from_node_id": {},
            "valid_to_node_id": {},
            "legacy_transition": {},
        },
    },
    {
        "additionalProperties": False,
        "required": ["title"],
        "properties": {
            "chapter_id": {},
            "title": {},
            "story_time": {},
            "sort_key": {},
            "description": {},
        },
    },
    {
        "additionalProperties": False,
        "required": ["kind", "title"],
        "properties": {
            "kind": {},
            "title": {},
            "promise": {},
            "start_node_id": {},
            "due_node_id": {},
            "payoff": {},
        },
    },
]

# Providers get a compact but complete closed contract under small legacy token
# budgets. Runtime validation still uses the bounded discriminated union above.
MemoryCandidateList = Annotated[
    Annotated[list[MemoryCandidateProposal], Field(max_length=100)],
    WithJsonSchema(
        {
            "type": "array",
            "maxItems": 100,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "payload", "evidence"],
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["canon", "entity_state", "timeline", "plot"],
                    },
                    "payload": {"type": "object", "oneOf": _PAYLOAD_SCHEMAS},
                    "evidence": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["quote", "start", "end"],
                        "properties": {
                            "quote": {"type": "string", "minLength": 1},
                            "start": {"type": "integer", "minimum": 0},
                            "end": {"type": "integer", "minimum": 1},
                        },
                    },
                },
            },
        }
    ),
]
