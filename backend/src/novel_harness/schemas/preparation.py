"""Strict contracts for the optional project-preparation interview."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

TextAnswer = Annotated[str, Field(max_length=10_000)]
ListAnswer = Annotated[list[Annotated[str, Field(max_length=1_000)]], Field(max_length=30)]
AnswerValue = TextAnswer | ListAnswer | None
QuestionStatus = Literal["open", "answered", "deferred", "not_applicable"]
PreparationStatus = Literal["not_started", "in_progress", "completed", "skipped"]


class StrictPreparationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PreparationQuestion(StrictPreparationModel):
    id: str = Field(min_length=1, max_length=64)
    fingerprint: str = Field(min_length=1, max_length=64)
    round: int = Field(ge=1, le=2)
    setting_key: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,119}$")
    category: str = Field(min_length=1, max_length=80)
    question: str = Field(min_length=1, max_length=500)
    rationale: str = Field(min_length=1, max_length=500)
    impact_areas: list[str] = Field(min_length=1, max_length=6)
    priority: Literal["high", "medium"]
    answer_format: Literal["short_text", "long_text", "ordered_list", "choice"]
    options: list[str] = Field(default_factory=list, max_length=8)
    source_ids: list[str] = Field(default_factory=list, max_length=8)
    origin: Literal["template", "ai"]
    status: QuestionStatus = "open"
    answer: AnswerValue = None
    canon_fact_id: str | None = Field(default=None, max_length=64)
    updated_at: datetime | None = None

    @model_validator(mode="after")
    def valid_shape(self):
        if self.answer_format == "choice" and not self.options:
            raise ValueError("choice questions require options")
        if self.answer_format != "choice" and self.options:
            raise ValueError("only choice questions may define options")
        if self.status == "answered" and self.answer is None:
            raise ValueError("answered questions require an answer")
        if self.status == "answered":
            if self.answer_format == "ordered_list" and not isinstance(self.answer, list):
                raise ValueError("ordered_list questions require a list answer")
            if self.answer_format != "ordered_list" and not isinstance(self.answer, str):
                raise ValueError("text and choice questions require a string answer")
            if self.answer_format == "choice" and self.answer not in self.options:
                raise ValueError("choice answer must match an option")
        return self


class PreparationImpactNotice(StrictPreparationModel):
    fact_ids: list[str] = Field(min_length=1, max_length=13)
    chapter_count: int = Field(ge=1)
    created_at: datetime


class ProjectPreparationRead(StrictPreparationModel):
    project_id: str
    revision: int = Field(ge=0)
    status: PreparationStatus
    round: int = Field(ge=0, le=2)
    source_hash: str = Field(default="", max_length=64)
    questions: list[PreparationQuestion] = Field(default_factory=list, max_length=13)
    generation_job_ids: list[str] = Field(default_factory=list, max_length=2)
    actionable_job_id: str | None = Field(default=None, max_length=36)
    impact_notice: PreparationImpactNotice | None = None
    answered_count: int = Field(default=0, ge=0, le=13)
    unresolved_count: int = Field(default=0, ge=0, le=13)
    unresolved_high_count: int = Field(default=0, ge=0, le=13)


class PreparationQuestionCommand(StrictPreparationModel):
    revision: int = Field(ge=1)
    action: Literal["answer", "defer", "not_applicable"]
    answer: AnswerValue = None

    @model_validator(mode="after")
    def meaningful_answer(self):
        if self.action != "answer":
            if self.answer is not None:
                raise ValueError("non-answer actions cannot include an answer")
            return self
        if isinstance(self.answer, str) and self.answer.strip():
            return self
        if isinstance(self.answer, list) and self.answer and all(
            isinstance(item, str) and item.strip() for item in self.answer
        ):
            if len(self.answer) > 30:
                raise ValueError("ordered answers contain at most 30 items")
            return self
        raise ValueError("answer is required")


class PreparationRevisionCommand(StrictPreparationModel):
    revision: int = Field(ge=1)


class GeneratedPreparationQuestion(StrictPreparationModel):
    setting_key: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,119}$")
    category: str = Field(min_length=1, max_length=80)
    question: str = Field(min_length=1, max_length=500)
    rationale: str = Field(min_length=1, max_length=500)
    impact_areas: list[str] = Field(min_length=1, max_length=6)
    priority: Literal["high", "medium"]
    answer_format: Literal["short_text", "long_text", "ordered_list", "choice"]
    options: list[str] = Field(default_factory=list, max_length=8)
    source_ids: list[str] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def valid_options(self):
        if (self.answer_format == "choice") != bool(self.options):
            raise ValueError("choice options do not match answer format")
        return self


class GeneratedPreparationBatch(StrictPreparationModel):
    questions: list[GeneratedPreparationQuestion] = Field(max_length=8)
