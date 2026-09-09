"""Chapter document and immutable version schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ChapterContract(BaseModel):
    # Preserve author extensions, but validate every field consumed by runtime code.
    model_config = ConfigDict(extra="allow", strict=True)

    purpose: str = ""
    pov_entity_id: str | None = None
    location_entity_id: str | None = None
    required_entity_ids: list[str] = Field(default_factory=list)
    required_plot_ids: list[str] = Field(default_factory=list)
    forbidden_revelations: list[str] = Field(default_factory=list)
    forbidden_phrases: list[str] = Field(default_factory=list)
    target_words: int = Field(default=0, ge=0)


class ChapterUpdate(BaseModel):
    content: str
    contract: dict[str, Any] = Field(default_factory=dict)
    revision: int = Field(ge=1)

    @field_validator("contract")
    @classmethod
    def validate_contract(cls, value):
        return ChapterContract.model_validate(value).model_dump(exclude_unset=True)


class VersionCreate(BaseModel):
    content: str
    expected_revision: int = Field(ge=1)
    source: str = "manual"
    summary: str = ""
    generation_job_id: str | None = None


class VersionRestore(BaseModel):
    expected_revision: int = Field(ge=1)


class VersionSummary(BaseModel):
    """Metadata used to render history lists; immutable content stays on detail API."""

    id: str
    chapter_id: str
    created_at: datetime
    source: str
    summary: str
    word_count: int
    parent_version_id: str | None
    restored_from_version_id: str | None
    generation_job_id: str | None


class VersionPage(BaseModel):
    items: list[VersionSummary]
    next_cursor: str | None
