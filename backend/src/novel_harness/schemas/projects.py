"""Project request and response schemas."""

from __future__ import annotations

from pydantic import BaseModel, Field

from novel_harness.schemas.common import Record


class ProjectCreate(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    premise: str = ""
    genre: str = ""
    target_words: int = Field(default=100_000, ge=1)
    daily_goal: int = Field(default=1_500, ge=0)


class ProjectRead(Record):
    title: str
    premise: str
    genre: str
    target_words: int
    daily_goal: int
    status: str
