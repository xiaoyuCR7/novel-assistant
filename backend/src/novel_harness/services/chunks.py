"""One versioned text projection shared by keyword, structural and vector recall."""

import hashlib
import json

from novel_harness.services.job_state import command_hash
from novel_harness.services.source_identity import source_document_identity_hash

STRATEGY = "text-v1-1200-180"
CHUNK_SIZE = 1200
CHUNK_OVERLAP = 180


def source_hash(item):
    payload = item["record"]
    if item["type"] == "manuscript":
        payload = {
            "version_id": item["record"]["version_id"],
            "title": item["title"],
            "content": item["content"],
        }
    elif item["type"] == "source_document":
        return source_document_identity_hash(item["record"], content=item["content"])
    return command_hash(payload)


def source_text(item):
    """Offsets address this text, stored as search_documents.body, not encoded JSON."""
    if item["type"] in {"manuscript", "source_document"}:
        return item["content"]
    from novel_harness.services.library import SOURCES

    _, title_field, content_field = SOURCES[item["type"]]
    record = item["record"]
    if item["type"] == "summary":
        from novel_harness.services.summary_projection import story_summary_details

        record = {**record, "details": story_summary_details(record.get("details"))}
    omitted = {
        "id",
        "project_id",
        "created_at",
        "updated_at",
        "revision",
        "deleted_at",
        "purge_after",
        "is_pinned",
        title_field,
        content_field,
    }
    extra = {
        key: value
        for key, value in record.items()
        if key not in omitted and value not in (None, "", [], {})
    }
    return item["content"] + ("\n" + json.dumps(extra, ensure_ascii=False) if extra else "")


def project_chunks(item, body):
    document_key = f"{item['type']}:{item['id']}"
    digest = source_hash(item)
    start, ordinal = 0, 0
    while True:
        end = min(len(body), start + CHUNK_SIZE)
        fragment = body[start:end]
        yield {
            "chunk_key": f"{document_key}:{STRATEGY}:{ordinal}",
            "document_key": document_key,
            "strategy": STRATEGY,
            "ordinal": ordinal,
            "start_offset": start,
            "end_offset": end,
            "source_hash": digest,
            "chunk_hash": hashlib.sha256((item["title"] + "\n" + fragment).encode()).hexdigest(),
            "body": fragment,
        }
        if end == len(body):
            break
        start = end - CHUNK_OVERLAP
        ordinal += 1
