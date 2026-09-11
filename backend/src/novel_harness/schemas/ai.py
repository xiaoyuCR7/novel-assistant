"""AI job request schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_serializer, model_validator


class AIJobCreate(BaseModel):
    project_id: str
    chapter_id: str | None = None
    task_type: Literal[
        "chat",
        "continue",
        "full_chapter",
        "plan",
        "draft",
        "review",
        "suggest",
        "rewrite",
        "scene_description",
    ]
    instructions: str = Field(default="", max_length=16000)
    token_budget: int = Field(default=12_000, ge=256, le=1_048_576)
    expected_revision: int | None = Field(default=None, ge=1)
    replaces_job_id: str | None = Field(default=None, min_length=1, max_length=64)
    confirm_unknown: bool = False
    conversation_id: str | None = Field(default=None, min_length=1, max_length=64)

    @model_serializer(mode="wrap")
    def preserve_legacy_command(self, handler):
        result = handler(self)
        if self.conversation_id is None:
            result.pop("conversation_id", None)
        return result

    @model_validator(mode="after")
    def scope_revision(self):
        if self.chapter_id is not None and self.expected_revision is None:
            raise ValueError("章节任务需要 expected_revision")
        if self.chapter_id is None and (
            self.expected_revision is not None or self.task_type != "chat"
        ):
            raise ValueError("全书对话不使用章节 revision")
        return self


class TaskBudgetPreflight(BaseModel):
    required_input_tokens: int
    token_budget: int
    output_token_budget: int
    context_capacity: int
    effective_input_limit: int
    can_fit: bool
    estimated: Literal[True] = True
    scope: Literal["first-stage-hard-only"] = "first-stage-hard-only"
    message: str


class AssetGenerateRequest(BaseModel):
    project_id: str
    entity_id: str | None = None
    kind: Literal["character", "scene"]
    prompt: str = Field(min_length=1)
    size: str = "1024x1024"


class JobAcceptance(BaseModel):
    confirmed: bool = False
