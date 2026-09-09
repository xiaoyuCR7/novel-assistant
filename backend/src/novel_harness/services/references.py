"""Stable references are resolved exclusively against the active Vault projection."""

import json
import re

from fastapi import HTTPException
from sqlalchemy import text

from novel_harness.db.models import CanonFact, EntityRelation
from novel_harness.services.job_state import command_hash
from novel_harness.services.search_index import dependencies_active
from novel_harness.services.source_identity import source_document_identity_hash


def source_citation(item):
    """Fingerprint the record that supplied the text, never a later database read."""
    payload = item['record']
    if item['type'] == 'manuscript':
        payload = {
            'version_id': item['record']['version_id'],
            'title': item['title'],
            'content': item['content'],
        }
    frozen_hash = item.get('chunk', {}).get('source_hash')
    if item['type'] == 'source_document':
        frozen_hash = source_document_identity_hash(item['record'])
    return {
        'type': item['type'], 'id': item['id'], 'title': item['title'],
        'revision': item['revision'],
        'source_hash': frozen_hash or command_hash(payload),
        **({'chunk': item['chunk']} if item.get('chunk') else {}),
    }

REFERENCE = re.compile(r"\[\[ref:([a-z_]+):([A-Za-z0-9_-]{1,64})\]\]")


def _unavailable_reference():
    return HTTPException(
        422,
        detail={
            "code": "REFERENCE_UNAVAILABLE",
            "message": "引用不属于当前项目、已删除或已过期。请移除引用标记后重试。",
        },
    )


def _require_dependency_available(session, kind, resource_id):
    model = {"canon": CanonFact, "relation": EntityRelation}.get(kind)
    if model is None:
        return
    record = session.get(model, resource_id, execution_options={"include_deleted": True})
    if (
        record is None
        or record.deleted_at is not None
        or (isinstance(record, CanonFact) and record.status != "confirmed")
        or not dependencies_active(session, record)
    ):
        raise _unavailable_reference()


def resolve_references(session, content, *, strict=True):
    items = []
    for kind, resource_id in dict.fromkeys(REFERENCE.findall(content)):
        encoded = session.scalar(
            text("SELECT data FROM search_documents WHERE key=:key"),
            {"key": f"{kind}:{resource_id}"},
        )
        if encoded is None:
            if strict:
                raise _unavailable_reference()
            continue
        try:
            _require_dependency_available(session, kind, resource_id)
        except HTTPException:
            if strict:
                raise
            continue
        items.append(json.loads(encoded))
    return items
