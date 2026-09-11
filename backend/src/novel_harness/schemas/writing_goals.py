"""Author-defined goals; zero disables daily or weekly goals."""

from datetime import date

from pydantic import BaseModel, ConfigDict, Field


class WritingGoalsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=0, strict=True)
    target_words: int = Field(ge=1, le=10_000_000, strict=True)
    daily_goal: int = Field(ge=0, le=1_000_000, strict=True)
    weekly_chapters: int = Field(ge=0, le=1000, strict=True)
    deadline: date | None = None
