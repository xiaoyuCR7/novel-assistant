from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class LibrarySummary(BaseModel):
    id: str
    type: str
    title: str
    preview: str
    revision: int
    is_pinned: bool
    status: str | None = None
    origin: str | None = None
    deleted_at: datetime | None = None
    purge_after: datetime | None = None


class LibraryPage(BaseModel):
    items: list[LibrarySummary]
    total: int
    counts: dict[str, int]
    next_cursor: str | None


class MaterialCreate(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    content: str = Field(default="", max_length=200_000)
    kind: str = ""
    tags: list[str] = Field(default_factory=list)


class MaterialPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=240)
    content: str | None = Field(default=None, max_length=200_000)
    is_pinned: bool | None = None
    fields: dict[str, Any] = Field(default_factory=dict, max_length=20)
