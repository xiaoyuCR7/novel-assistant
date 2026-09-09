"""Story knowledge request schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class IdeaCreate(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    content: str = ""
    tags: list[str] = Field(default_factory=list)
    source: str = ""
    status: str = "captured"


class NodeCreate(BaseModel):
    kind: Literal["volume", "chapter", "scene"]
    parent_id: str | None = None
    title: str = Field(min_length=1, max_length=240)
    summary: str = ""
    order_index: int = Field(default=0, ge=0)
    target_words: int = Field(default=0, ge=0)
    status: str = "planned"
    pov_entity_id: str | None = None


class EntityCreate(BaseModel):
    kind: Literal["character", "location", "organization", "item"]
    name: str = Field(min_length=1, max_length=240)
    summary: str = ""
    profile: dict[str, Any] = Field(default_factory=dict)
    state: dict[str, Any] = Field(default_factory=dict)


class RelationCreate(BaseModel):
    source_entity_id: str
    target_entity_id: str
    relation_type: str = Field(min_length=1, max_length=80)
    description: str = ""


class CanonFactCreate(BaseModel):
    subject_entity_id: str | None = None
    predicate: str = Field(min_length=1, max_length=120)
    value: Any
    source_note: str = ""
    source_version_id: str | None = None
    valid_from_node_id: str | None = None
    valid_to_node_id: str | None = None
    status: Literal["pending", "confirmed", "retracted"] = "confirmed"


class TimelineEventCreate(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    story_time: str = ""
    sort_key: int = 0
    chapter_id: str | None = None
    description: str = ""


class PlotThreadCreate(BaseModel):
    kind: Literal["main", "subplot", "character", "foreshadowing"]
    title: str = Field(min_length=1, max_length=240)
    promise: str = ""
    status: str = "active"
    start_node_id: str | None = None
    due_node_id: str | None = None
    payoff: str = ""


class StyleProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    is_active: bool = False
    config: dict[str, Any] = Field(default_factory=dict)
