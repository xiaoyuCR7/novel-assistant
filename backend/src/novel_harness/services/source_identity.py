"""Canonical immutable identities for imported source text."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from novel_harness.services.job_state import command_hash


def _field(record: Mapping[str, Any] | object, name: str) -> Any:
    return record[name] if isinstance(record, Mapping) else getattr(record, name)


def source_document_identity_hash(
    record: Mapping[str, Any] | object,
    *,
    content: str | None = None,
) -> str:
    """Hash authored identity, independently from pin/lifecycle concurrency state."""
    content_hash = _field(record, "content_hash")
    if content is None:
        if isinstance(record, Mapping):
            content = record.get("content")
        else:
            content = getattr(record, "content", None)
    if content is not None and hashlib.sha256(content.encode()).hexdigest() != content_hash:
        raise ValueError("SOURCE_DOCUMENT_CONTENT_HASH_MISMATCH")
    return command_hash(
        {
            "content_hash": content_hash,
            "category": _field(record, "category"),
            "relative_path": _field(record, "relative_path"),
            "revision": _field(record, "content_revision"),
        }
    )
