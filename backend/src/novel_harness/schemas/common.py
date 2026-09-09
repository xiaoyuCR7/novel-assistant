"""Shared schema definitions."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Record(ORMModel):
    id: str
    created_at: datetime
    updated_at: datetime
