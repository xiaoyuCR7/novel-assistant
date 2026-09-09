"""Feedback, preference, and conflict request schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class FeedbackCreate(BaseModel):
    project_id: str
    chapter_id: str | None = None
    job_id: str | None = None
    rating: int = Field(ge=1, le=5)
    tags: list[str] = Field(default_factory=list)
    original_text: str = ""
    corrected_text: str = ""
    comment: str = ""


class PreferenceConfirmation(BaseModel):
    instruction: str | None = Field(default=None, max_length=2000)


class ConflictCreate(BaseModel):
    code: str = Field(min_length=1, max_length=80)
    severity: Literal["warning", "error"]
    message: str = Field(min_length=1)
    evidence: list[str] = Field(default_factory=list)
    related_entity_ids: list[str] = Field(default_factory=list)


class ConflictDecision(BaseModel):
    option_id: str
    note: str = ""


class EntityStateConflictResolution(BaseModel):
    state_id: str = Field(min_length=1, max_length=64)
    revision: int = Field(ge=1)
    note: str = Field(default="", max_length=2000)
