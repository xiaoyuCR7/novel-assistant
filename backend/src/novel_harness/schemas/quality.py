"""Bounded quality workflow inputs and evidence-backed model reviews."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_QUALITY_TEXT = 50000
QualityDimension = Literal["readability", "engagement", "pacing", "clarity", "consistency"]


class QualityModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class QualityChapterInput(QualityModel):
    chapter_id: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=1)


class QualityAcceptance(QualityModel):
    chapter_id: str = Field(min_length=1, max_length=128)
    confirmed: bool = False
    selected_hunk_ids: list[str] | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("selected_hunk_ids")
    @classmethod
    def unique_hunks(cls, value):
        if value is not None and (
            len(set(value)) != len(value) or any(not item or len(item) > 128 for item in value)
        ):
            raise ValueError("Selected change identifiers must be distinct and bounded")
        return value


class QualityRunCreate(QualityModel):
    mode: Literal["polish", "collaborate"]
    chapters: list[QualityChapterInput] = Field(min_length=1, max_length=5)
    instructions: str = Field(default="", max_length=8000)
    token_budget: int = Field(default=12000, ge=256, le=1_048_576)
    quality_target: int = Field(default=75, ge=50, le=95)

    @model_validator(mode="after")
    def validate_chapters(self):
        if len({chapter.chapter_id for chapter in self.chapters}) != len(self.chapters):
            raise ValueError("Each chapter can appear only once")
        if self.mode == "polish" and len(self.chapters) != 1:
            raise ValueError("Polish mode requires exactly one chapter")
        return self


class QualityScores(QualityModel):
    readability: int = Field(ge=0, le=100)
    engagement: int = Field(ge=0, le=100)
    pacing: int = Field(ge=0, le=100)
    clarity: int = Field(ge=0, le=100)
    consistency: int = Field(ge=0, le=100)


class QualityIssue(QualityModel):
    dimension: QualityDimension
    severity: Literal["note", "warning", "error"]
    quote: str = Field(min_length=1, max_length=1000)
    reason: str = Field(min_length=1, max_length=1500)
    suggestion: str = Field(min_length=1, max_length=1500)

    @field_validator("quote", "reason", "suggestion")
    @classmethod
    def require_meaningful_text(cls, value):
        if not value.strip():
            raise ValueError("Review evidence and advice cannot be blank")
        return value


class QualityReview(QualityModel):
    scores: QualityScores
    summary: str = Field(min_length=1, max_length=2000)
    issues: list[QualityIssue] = Field(max_length=12)
    next_guidance: str = Field(max_length=2000)
    preserves_story: bool

    @field_validator("summary")
    @classmethod
    def require_summary(cls, value):
        if not value.strip():
            raise ValueError("Review summary cannot be blank")
        return value
