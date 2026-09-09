"""Durable task command controls (never credentials)."""

from pydantic import BaseModel, Field


class ResumeJob(BaseModel):
    expected_control_revision: int = Field(ge=1)
    confirm_unknown: bool = False


class RetrySummary(ResumeJob):
    job_id: str


class CompleteChapter(BaseModel):
    expected_revision: int = Field(ge=1)
    replaces_job_id: str | None = Field(default=None, min_length=1, max_length=64)
    confirm_unknown: bool = False
