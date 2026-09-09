"""Deterministic, atomic import-draft commit into an isolated project Vault."""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID, uuid5

from fastapi import HTTPException
from sqlalchemy import select, text

from novel_harness.db.models import (
    ChapterDocument,
    ChapterVersion,
    ImportAnalysisUnit,
    ImportBatch,
    Project,
    ProjectContinuation,
    SourceDocument,
    StoryNode,
)
from novel_harness.db.vault import ProjectVaultRegistry, _fsync_directory_chain
from novel_harness.schemas.projects import ProjectCreate
from novel_harness.services.chunks import project_chunks
from novel_harness.services.import_drafts import (
    DraftChapter,
    DraftFile,
    ImportDraftRecord,
    ImportDraftStore,
    _audit_id,
    _draft_chapter_id,
    decode_text,
    normalize_relative_path,
    public_import_draft_payload,
    split_chapters,
)
from novel_harness.services.serialization import serialize
from novel_harness.services.versions import count_words, create_version

_PROJECT_NAMESPACE = UUID("8d2b4c4c-c579-4cd2-96e8-70ba82c44d74")
_COPY_CHUNK_BYTES = 64 * 1024


def _error(code: str, message: str, status_code: int = 422) -> HTTPException:
    return HTTPException(status_code, detail={"code": code, "message": message})


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical_digest(value: Any) -> str:
    return _digest(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    )


def _unordered_digest(values) -> tuple[int, str]:
    count = 0
    accumulator = 0
    modulus = 1 << 256
    for value in values:
        count += 1
        accumulator = (accumulator + int(_canonical_digest(value), 16)) % modulus
    return count, f"{accumulator:064x}"


def _project_id(draft_id: str) -> str:
    return str(uuid5(_PROJECT_NAMESPACE, f"import-project:{draft_id}"))


def _batch_id(project_id: str, draft_id: str) -> str:
    return str(uuid5(UUID(project_id), f"import-batch:{draft_id}"))


def _record_conflict(record: ImportDraftRecord) -> HTTPException:
    return HTTPException(
        409,
        detail={
            "code": "DRAFT_REVISION_CONFLICT",
            "current": public_import_draft_payload(record),
        },
    )


def _read_verified_sources(
    store: ImportDraftStore, record: ImportDraftRecord
) -> dict[str, tuple[DraftFile, Path]]:
    title = record.display_name.strip()
    if not title:
        raise _error("IMPORT_TITLE_REQUIRED", "A project title is required.")
    if not record.continuation.confirmed:
        raise _error(
            "IMPORT_CONFIRMATION_REQUIRED",
            "Confirm the continuation boundary before committing the import.",
        )

    selected = [item for item in record.files if item.selected]
    if not selected:
        raise _error("IMPORT_SELECTION_REQUIRED", "Select at least one source file.")
    normalized = [normalize_relative_path(item.relative_path) for item in record.files]
    if len({path.casefold() for path in normalized}) != len(normalized):
        raise _error("IMPORT_FILE_DUPLICATE", "Import file paths must be unique.")

    draft_dir = store._existing_draft_dir(record.draft_id)
    raw_root = (draft_dir / "raw").resolve()
    result: dict[str, tuple[DraftFile, Path]] = {}
    for item in selected:
        relative = normalize_relative_path(item.relative_path)
        if item.audit_id != _audit_id(record.draft_id, relative, item.byte_hash):
            raise _error("IMPORT_SOURCE_CHANGED", "An imported source identity is invalid.")
        source = store._raw_destination(raw_root, relative)
        try:
            if (
                not source.is_file()
                or source.is_symlink()
                or source.resolve() != source
                or not source.resolve().is_relative_to(raw_root)
            ):
                raise OSError("unsafe source")
            text_value = _read_verified_source(item, source)
        except OSError as exc:
            raise _error("IMPORT_SOURCE_CHANGED", "An imported source is unavailable.") from exc
        del text_value
        result[relative.casefold()] = (item, source)

    manuscript_paths = {
        normalize_relative_path(item.relative_path).casefold()
        for item in selected
        if item.category == "manuscript"
    }
    if not manuscript_paths:
        raise _error("IMPORT_CHAPTERS_REQUIRED", "Select at least one manuscript source.")
    return result


def _read_verified_source(item: DraftFile | dict[str, Any], source: Path) -> str:
    try:
        with source.open("rb") as handle:
            raw = handle.read()
    except OSError as exc:
        raise _error("IMPORT_SOURCE_CHANGED", "An imported source is unavailable.") from exc
    expected_size = item.size_bytes if isinstance(item, DraftFile) else item["size_bytes"]
    expected_byte_hash = item.byte_hash if isinstance(item, DraftFile) else item["byte_hash"]
    expected_encoding = item.encoding if isinstance(item, DraftFile) else item["encoding"]
    expected_content_hash = (
        item.content_hash if isinstance(item, DraftFile) else item["content_hash"]
    )
    if len(raw) != expected_size or _digest(raw) != expected_byte_hash:
        raise _error("IMPORT_SOURCE_CHANGED", "An imported source changed after preview.")
    text_value, encoding = decode_text(raw)
    del raw
    if encoding != expected_encoding or _digest(text_value.encode()) != expected_content_hash:
        raise _error("IMPORT_SOURCE_CHANGED", "An imported source changed after preview.")
    return text_value


def _validated_chapters(
    record: ImportDraftRecord,
    sources: dict[str, tuple[DraftFile, Path]],
) -> list[tuple[DraftChapter, str]]:
    active = [item.model_copy(deep=True) for item in record.chapters]
    if not active:
        raise _error("IMPORT_CHAPTERS_REQUIRED", "The import has no active chapters.")
    if [item.order_index for item in active] != list(range(len(active))):
        raise _error("IMPORT_CHAPTER_ORDER_INVALID", "Chapter order must be contiguous.")

    inventory_keys = Counter(
        (
            normalize_relative_path(item.relative_path).casefold(),
            item.start,
            item.end,
        )
        for item in record.chapter_inventory
    )
    approved: list[tuple[DraftChapter, str]] = []
    by_path: dict[str, list[DraftChapter]] = {}
    for chapter in active:
        path_key = normalize_relative_path(chapter.relative_path).casefold()
        source_entry = sources.get(path_key)
        if source_entry is None or source_entry[0].category != "manuscript":
            raise _error("IMPORT_CHAPTER_MAPPING_INVALID", "A chapter source is not selected.")
        if not chapter.title.strip():
            raise _error("IMPORT_CHAPTER_MAPPING_INVALID", "Every chapter requires a title.")
        if chapter.draft_chapter_id != _draft_chapter_id(
            record.draft_id, chapter.source_path, chapter.start, chapter.end
        ):
            raise _error("IMPORT_CHAPTER_MAPPING_INVALID", "A chapter identity is stale.")
        key = (path_key, chapter.start, chapter.end)
        if inventory_keys[key] < 1:
            raise _error("IMPORT_CHAPTER_MAPPING_INVALID", "A chapter mapping is stale.")
        inventory_keys[key] -= 1
        by_path.setdefault(path_key, []).append(chapter)

    selected_manuscript = {
        path_key for path_key, entry in sources.items() if entry[0].category == "manuscript"
    }
    if set(by_path) != selected_manuscript:
        raise _error("IMPORT_CHAPTER_MAPPING_INVALID", "Active chapter sources are incomplete.")
    for path_key, chapters in by_path.items():
        item, source_path = sources[path_key]
        text_value = _read_verified_source(item, source_path)
        discovered = split_chapters(item.relative_path, text_value)
        expected_offsets = [(item.start, item.end) for item in discovered]
        actual_offsets = sorted((item.start, item.end) for item in chapters)
        if actual_offsets != expected_offsets:
            raise _error("IMPORT_CHAPTER_MAPPING_INVALID", "Chapter offsets changed after preview.")
        for chapter in chapters:
            if chapter.start < 0 or chapter.end < chapter.start or chapter.end > len(text_value):
                raise _error("IMPORT_CHAPTER_MAPPING_INVALID", "A chapter offset is invalid.")

        for chapter in chapters:
            approved.append((chapter, text_value[chapter.start : chapter.end]))
        del text_value
    approved_by_id = {chapter.draft_chapter_id: content for chapter, content in approved}
    approved = [(chapter, approved_by_id[chapter.draft_chapter_id]) for chapter in active]
    return approved


def _continuation_indexes(
    record: ImportDraftRecord, chapters: list[tuple[DraftChapter, str]]
) -> tuple[int | None, int | None]:
    def matches(token: str | None) -> list[int]:
        if token is None:
            return []
        return [
            index
            for index, (chapter, _content) in enumerate(chapters)
            if token == chapter.draft_chapter_id
        ]

    completed_matches = matches(record.continuation.completed_through_node_id)
    current_matches = matches(record.continuation.current_chapter_id)
    completed = completed_matches[0] if completed_matches else None
    current = current_matches[0] if current_matches else None
    for token, resolved in (
        (record.continuation.completed_through_node_id, completed),
        (record.continuation.current_chapter_id, current),
    ):
        if token is not None and resolved is None:
            raise _error("IMPORT_CONTINUATION_INVALID", "A continuation chapter is unavailable.")
    if completed is not None and current is not None and current < completed:
        raise _error("IMPORT_CONTINUATION_INVALID", "The current chapter precedes the boundary.")
    return completed, current


def _copy_source(
    source: Path,
    target: Path,
    expected_hash: str,
    expected_size: int,
    sync_root: Path | None = None,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    hasher = hashlib.sha256()
    size = 0
    with source.open("rb") as input_handle, target.open("xb") as output_handle:
        while chunk := input_handle.read(_COPY_CHUNK_BYTES):
            size += len(chunk)
            hasher.update(chunk)
            output_handle.write(chunk)
        output_handle.flush()
        os.fsync(output_handle.fileno())
    if size != expected_size or hasher.hexdigest() != expected_hash:
        raise _error("IMPORT_SOURCE_CHANGED", "An imported source changed while copying.")
    if sync_root is not None:
        _fsync_directory_chain(target.parent, sync_root)


def _collision(message: str = "The deterministic import Vault is invalid.") -> HTTPException:
    return _error("IMPORT_PROJECT_COLLISION", message, 409)


def _registered_replay(
    registry: ProjectVaultRegistry,
    record: ImportDraftRecord,
    project_id: str,
    batch_id: str,
) -> dict[str, Any] | None:
    """Return the current edited project when a frozen import is already visible."""
    if record.committed_project_id is None and record.committed_batch_id is None:
        return None
    if record.committed_project_id != project_id or record.committed_batch_id != batch_id:
        raise _collision("The committed draft identity is invalid.")
    row = registry._registration_row(project_id)
    if row is None:
        return None
    if not registry._registration_matches(project_id, record.display_name.strip()):
        raise _collision("The project registry identity is invalid.")
    try:
        vault = registry.require(project_id, job_only=True)
    except HTTPException as exc:
        raise _collision("The registered import Vault is unavailable.") from exc
    with vault.database.session_scope() as session:
        project = session.get(Project, project_id)
        batch = session.get(ImportBatch, batch_id)
        if project is None or batch is None:
            raise _collision("The registered import identity is incomplete.")
        manifest = batch.manifest
        manifest_payload = {
            key: value for key, value in manifest.items() if key != "commit_fingerprint"
        }
        fingerprint = manifest.get("commit_fingerprint")
        if (
            batch.project_id != project_id
            or batch.source_kind != record.source_kind
            or batch.status != "imported"
            or manifest.get("draft_id") != record.draft_id
            or (
                record.committed_fingerprint is not None
                and fingerprint != record.committed_fingerprint
            )
            or _canonical_digest(manifest_payload) != fingerprint
        ):
            raise _collision("The registered import fingerprint is invalid.")
        return serialize(project)


def _build_expected_graph(
    record: ImportDraftRecord,
    sources: dict[str, tuple[DraftFile, Path]],
    chapters: list[tuple[DraftChapter, str]],
    completed_index: int | None,
    current_index: int | None,
    project_id: str,
    batch_id: str,
    payload: ProjectCreate,
) -> dict[str, Any]:
    node_specs: list[dict[str, Any]] = []
    for index, (chapter, content) in enumerate(chapters):
        node_id = str(
            uuid5(
                UUID(project_id),
                f"chapter:{chapter.order_index}:{chapter.relative_path}:"
                f"{chapter.start}:{chapter.end}",
            )
        )
        status = "planned"
        if completed_index is not None and index <= completed_index:
            status = "completed"
        if current_index == index:
            status = "drafting"
        version_id = str(
            uuid5(
                UUID(project_id),
                f"version:{node_id}:import:{_digest(content.encode())}",
            )
        )
        node_specs.append(
            {
                "id": node_id,
                "draft_chapter_id": chapter.draft_chapter_id,
                "relative_path": chapter.relative_path,
                "title": chapter.title,
                "order_index": chapter.order_index,
                "start": chapter.start,
                "end": chapter.end,
                "status": status,
                "content": content,
                "content_hash": _digest(content.encode()),
                "version_id": version_id,
            }
        )

    path_counts = Counter(
        normalize_relative_path(chapter.relative_path).casefold() for chapter, _content in chapters
    )
    chapter_by_path = {
        normalize_relative_path(spec["relative_path"]).casefold(): spec["id"]
        for spec in node_specs
        if path_counts[normalize_relative_path(spec["relative_path"]).casefold()] == 1
    }
    source_specs: dict[str, dict[str, Any]] = {}
    for path_key, (item, source_path) in sources.items():
        source_specs[path_key] = {
            "id": item.audit_id,
            "relative_path": item.relative_path,
            "stored_path": PurePosixPath(
                "imports", batch_id, "sources", item.relative_path
            ).as_posix(),
            "category": item.category,
            "title": item.title,
            "source_path": source_path,
            "encoding": item.encoding,
            "size_bytes": item.size_bytes,
            "byte_hash": item.byte_hash,
            "content_hash": item.content_hash,
            "content_revision": 1,
            "chapter_id": chapter_by_path.get(path_key),
        }

    selected_audit_ids = {spec["id"] for spec in source_specs.values()}
    audit_ids = list(dict.fromkeys(record.continuation.source_document_ids))
    if any(audit_id not in selected_audit_ids for audit_id in audit_ids):
        raise _error(
            "IMPORT_CONTINUATION_SOURCE_INVALID",
            "A continuation source is not part of this import.",
        )
    continuation = {
        "confirmed": True,
        "completed_through_node_id": (
            node_specs[completed_index]["id"] if completed_index is not None else None
        ),
        "current_chapter_id": (
            node_specs[current_index]["id"] if current_index is not None else None
        ),
        "objective": record.continuation.objective,
        "source_document_ids": audit_ids,
        "revision": record.continuation.revision,
    }

    unit_count, units_digest = _unordered_digest(
        _iter_unit_specs(project_id, source_specs, node_specs)
    )

    confirmed_files = [
        {
            "audit_id": item.audit_id,
            "relative_path": item.relative_path,
            "category": item.category,
            "title": item.title,
            "selected": item.selected,
            "encoding": item.encoding,
            "size_bytes": item.size_bytes,
            "byte_hash": item.byte_hash,
            "content_hash": item.content_hash,
        }
        for item in record.files
    ]
    confirmed_chapters = [
        {
            key: spec[key]
            for key in (
                "draft_chapter_id",
                "id",
                "relative_path",
                "title",
                "order_index",
                "start",
                "end",
                "status",
                "content_hash",
                "version_id",
            )
        }
        for spec in node_specs
    ]
    chapter_order = [spec["draft_chapter_id"] for spec in node_specs]
    continuation_sources = audit_ids if len(audit_ids) <= 200 else []
    confirmed_continuation = {
        "confirmed": record.continuation.confirmed,
        "completed_through_node_id": record.continuation.completed_through_node_id,
        "current_chapter_id": record.continuation.current_chapter_id,
        "objective": record.continuation.objective,
        "source_document_ids": continuation_sources,
        "source_document_count": len(audit_ids),
        "source_document_ids_digest": _canonical_digest(audit_ids),
        "revision": record.continuation.revision,
    }
    graph_payload = {
        "project": {"id": project_id, **payload.model_dump(), "status": "active"},
        "nodes": confirmed_chapters,
        "sources": [
            {
                key: spec[key]
                for key in (
                    "id",
                    "relative_path",
                    "stored_path",
                    "category",
                    "title",
                    "encoding",
                    "size_bytes",
                    "byte_hash",
                    "content_hash",
                    "content_revision",
                    "chapter_id",
                )
            }
            for spec in source_specs.values()
        ],
        "continuation": continuation,
        "unit_count": unit_count,
        "units_digest": units_digest,
    }
    snapshot = {
        "draft_id": record.draft_id,
        "project_id": project_id,
        "title": record.display_name,
        "source_kind": record.source_kind,
        "file_count": len(confirmed_files),
        "files_digest": _canonical_digest(confirmed_files),
        "chapter_count": len(confirmed_chapters),
        "chapters_digest": _canonical_digest(confirmed_chapters),
        "chapter_order_digest": _canonical_digest(chapter_order),
        "continuation_digest": _canonical_digest(confirmed_continuation),
        "graph_digest": _canonical_digest(graph_payload),
    }
    manifest_payload = {
        "draft_id": record.draft_id,
        "title": record.display_name,
        "source_kind": record.source_kind,
        "file_count": len(confirmed_files),
        "selected_file_count": len(source_specs),
        "files_digest": snapshot["files_digest"],
        "chapter_count": len(confirmed_chapters),
        "chapters_digest": snapshot["chapters_digest"],
        "chapter_order_digest": snapshot["chapter_order_digest"],
        "continuation": confirmed_continuation,
        "continuation_digest": snapshot["continuation_digest"],
        "node_count": len(node_specs),
        "document_count": len(node_specs),
        "version_count": len(node_specs),
        "source_count": len(source_specs),
        "unit_count": unit_count,
        "graph_digest": snapshot["graph_digest"],
    }
    fingerprint = _canonical_digest(manifest_payload)
    manifest = {**manifest_payload, "commit_fingerprint": fingerprint}
    return {
        "project": {"id": project_id, **payload.model_dump(), "status": "active"},
        "batch_id": batch_id,
        "snapshot": snapshot,
        "fingerprint": fingerprint,
        "manifest": manifest,
        "nodes": node_specs,
        "sources": source_specs,
        "continuation": continuation,
        "unit_count": unit_count,
        "units_digest": units_digest,
    }


def _iter_unit_specs(
    project_id: str,
    source_specs: dict[str, dict[str, Any]],
    node_specs: list[dict[str, Any]],
):
    for spec in source_specs.values():
        content = _read_verified_source(spec, spec["source_path"])
        item = {
            "type": "source_document",
            "id": spec["id"],
            "title": spec["title"],
            "content": content,
            "record": {
                "id": spec["id"],
                "relative_path": spec["relative_path"],
                "category": spec["category"],
                "content_hash": spec["content_hash"],
                "content_revision": spec["content_revision"],
            },
        }
        for chunk in project_chunks(item, content):
            unit_key = f"source:{spec['id']}:{chunk['ordinal']}:{chunk['chunk_hash']}"
            yield {
                "id": str(uuid5(UUID(project_id), unit_key)),
                "source_document_id": spec["id"],
                "chapter_id": spec["chapter_id"],
                "chunk_key": chunk["chunk_key"],
                "unit_key": unit_key,
                "kind": "source_memory",
                "source_hash": chunk["chunk_hash"],
                "result": {},
            }
        del content
    for spec in node_specs:
        if spec["status"] != "completed":
            continue
        path_key = normalize_relative_path(spec["relative_path"]).casefold()
        source_spec = source_specs[path_key]
        item = {
            "type": "manuscript",
            "id": spec["id"],
            "title": spec["title"] + " · 正文版本",
            "content": spec["content"],
            "record": {"version_id": spec["version_id"]},
        }
        for chunk in project_chunks(item, spec["content"]):
            unit_key = (
                f"summary:{source_spec['id']}:{spec['id']}:{chunk['ordinal']}:{chunk['chunk_hash']}"
            )
            yield {
                "id": str(uuid5(UUID(project_id), unit_key)),
                "source_document_id": source_spec["id"],
                "chapter_id": spec["id"],
                "chunk_key": chunk["chunk_key"],
                "unit_key": unit_key,
                "kind": "summary_map",
                "source_hash": chunk["chunk_hash"],
                "result": {
                    "source_version_id": spec["version_id"],
                    "content_hash": spec["content_hash"],
                    "source_start": spec["start"],
                    "source_end": spec["end"],
                },
            }


def _verify_existing_import(session, root: Path, expected: dict[str, Any]):
    project_id = expected["project"]["id"]
    batch_id = expected["batch_id"]
    projects = list(session.scalars(select(Project)))
    if len(projects) != 1:
        raise _collision()
    project = projects[0]
    project_values = {
        key: getattr(project, key)
        for key in ("id", "title", "premise", "genre", "target_words", "daily_goal", "status")
    }
    if project_values != expected["project"]:
        raise _collision("The imported project metadata does not match the draft.")

    batches = list(session.scalars(select(ImportBatch)))
    if len(batches) != 1:
        raise _collision()
    batch = batches[0]
    if (
        batch.id != batch_id
        or batch.project_id != project_id
        or batch.source_kind != expected["manifest"]["source_kind"]
        or batch.status != "imported"
        or batch.completed_units != 0
        or batch.total_units != expected["unit_count"]
        or batch.last_error != ""
        or batch.manifest != expected["manifest"]
    ):
        raise _collision("The import batch fingerprint or counters are invalid.")

    expected_nodes = {item["id"]: item for item in expected["nodes"]}
    nodes = session.scalars(
        select(StoryNode).execution_options(include_deleted=True, yield_per=200)
    )
    seen_nodes: set[str] = set()
    for node in nodes:
        if node.id not in expected_nodes or node.id in seen_nodes:
            raise _collision("The imported chapter set is incomplete.")
        seen_nodes.add(node.id)
        spec = expected_nodes[node.id]
        if (
            node.project_id != project_id
            or node.parent_id is not None
            or node.kind != "chapter"
            or node.title != spec["title"]
            or node.summary != ""
            or node.order_index != spec["order_index"]
            or node.target_words != 0
            or node.status != spec["status"]
            or node.pov_entity_id is not None
            or node.revision != 1
            or node.is_pinned is not False
            or node.deleted_at is not None
            or node.purge_after is not None
        ):
            raise _collision("An imported chapter does not match the confirmed mapping.")
    if seen_nodes != set(expected_nodes):
        raise _collision("The imported chapter set is incomplete.")

    documents = session.scalars(select(ChapterDocument).execution_options(yield_per=200))
    seen_documents: set[str] = set()
    for document in documents:
        if document.chapter_id not in expected_nodes or document.chapter_id in seen_documents:
            raise _collision("The imported chapter documents are incomplete.")
        seen_documents.add(document.chapter_id)
        spec = expected_nodes[document.chapter_id]
        if (
            document.project_id != project_id
            or document.content != spec["content"]
            or document.contract != {}
            or document.current_version_id != spec["version_id"]
            or document.revision != 2
        ):
            raise _collision("An imported chapter document is invalid.")
    if seen_documents != set(expected_nodes):
        raise _collision("The imported chapter documents are incomplete.")

    expected_versions = {item["version_id"]: item for item in expected["nodes"]}
    versions = session.scalars(select(ChapterVersion).execution_options(yield_per=200))
    seen_versions: set[str] = set()
    for version in versions:
        if version.id not in expected_versions or version.id in seen_versions:
            raise _collision("The imported version set is incomplete.")
        seen_versions.add(version.id)
        spec = expected_versions[version.id]
        if (
            version.chapter_id != spec["id"]
            or version.project_id != project_id
            or version.parent_version_id is not None
            or version.restored_from_version_id is not None
            or version.generation_job_id is not None
            or version.content != spec["content"]
            or version.summary != ""
            or version.word_count != count_words(spec["content"])
            or version.source != "import"
        ):
            raise _collision("An imported chapter version is invalid.")
    if seen_versions != set(expected_versions):
        raise _collision("The imported version set is incomplete.")

    expected_sources = {item["id"]: item for item in expected["sources"].values()}
    sources = session.scalars(
        select(SourceDocument).execution_options(include_deleted=True, yield_per=50)
    )
    seen_sources: set[str] = set()
    for source in sources:
        if source.id not in expected_sources or source.id in seen_sources:
            raise _collision("The imported source set is incomplete.")
        seen_sources.add(source.id)
        spec = expected_sources[source.id]
        expected_content = _read_verified_source(spec, spec["source_path"])
        values = {
            key: getattr(source, key)
            for key in (
                "project_id",
                "import_batch_id",
                "chapter_id",
                "relative_path",
                "stored_path",
                "category",
                "title",
                "content",
                "encoding",
                "size_bytes",
                "byte_hash",
                "content_hash",
                "content_revision",
            )
        }
        expected_values = {
            **{
                key: spec[key]
                for key in values
                if key not in {"project_id", "import_batch_id", "content"}
            },
            "project_id": project_id,
            "import_batch_id": batch_id,
            "content": expected_content,
        }
        if (
            values != expected_values
            or source.revision != 1
            or source.is_pinned is not False
            or source.deleted_at is not None
            or source.purge_after is not None
        ):
            raise _collision("An imported source document is invalid.")
        stored = (root / PurePosixPath(source.stored_path)).resolve()
        if not stored.is_relative_to(root.resolve()) or not stored.is_file():
            raise _collision("An imported source copy is missing.")
        hasher = hashlib.sha256()
        size = 0
        with stored.open("rb") as handle:
            while chunk := handle.read(_COPY_CHUNK_BYTES):
                size += len(chunk)
                hasher.update(chunk)
        if size != source.size_bytes or hasher.hexdigest() != source.byte_hash:
            raise _collision("An imported source copy is invalid.")
    if seen_sources != set(expected_sources):
        raise _collision("The imported source set is incomplete.")

    continuation = session.get(ProjectContinuation, project_id)
    if continuation is None:
        raise _collision("The imported continuation is missing.")
    continuation_values = {
        key: getattr(continuation, key)
        for key in (
            "completed_through_node_id",
            "current_chapter_id",
            "objective",
            "source_document_ids",
            "revision",
        )
    }
    expected_continuation = {
        key: value for key, value in expected["continuation"].items() if key != "confirmed"
    }
    if continuation_values != expected_continuation:
        raise _collision("The imported continuation is invalid.")

    def actual_unit_specs():
        units = session.scalars(select(ImportAnalysisUnit).execution_options(yield_per=200))
        for unit in units:
            if (
                unit.project_id != project_id
                or unit.import_batch_id != batch_id
                or unit.status != "queued"
                or unit.attempt_count != 0
                or unit.worker_epoch is not None
                or unit.error_code is not None
                or unit.error_message != ""
            ):
                raise _collision("An import analysis unit is invalid.")
            yield {
                key: getattr(unit, key)
                for key in (
                    "id",
                    "source_document_id",
                    "chapter_id",
                    "chunk_key",
                    "unit_key",
                    "kind",
                    "source_hash",
                    "result",
                )
            }

    actual_unit_count, actual_units_digest = _unordered_digest(actual_unit_specs())
    if (
        actual_unit_count != expected["unit_count"]
        or actual_units_digest != expected["units_digest"]
    ):
        raise _collision("The import analysis unit set is incomplete.")
    if session.execute(text("PRAGMA foreign_key_check")).first() is not None:
        raise _collision("The imported Vault has invalid references.")
    return project, batch


def commit_draft(
    registry: ProjectVaultRegistry,
    store: ImportDraftStore,
    draft_id: str,
    expected_revision: int,
) -> dict[str, Any]:
    """Commit one confirmed draft exactly once under its lifecycle lock."""
    with store.lifecycle(draft_id):
        record = store._get_unlocked(draft_id)
        if expected_revision != record.revision:
            raise _record_conflict(record)
        project_id = _project_id(record.draft_id)
        batch_id = _batch_id(project_id, record.draft_id)
        project_mismatch = record.committed_project_id not in {None, project_id}
        batch_mismatch = record.committed_batch_id not in {None, batch_id}
        if project_mismatch or batch_mismatch:
            raise _error(
                "IMPORT_PROJECT_COLLISION",
                "The committed draft identity is invalid.",
                409,
            )

        replayed = _registered_replay(registry, record, project_id, batch_id)
        if replayed is not None:
            return {"project": replayed, "import_batch_id": batch_id}

        sources = _read_verified_sources(store, record)
        chapters = _validated_chapters(record, sources)
        completed_index, current_index = _continuation_indexes(record, chapters)

        payload = ProjectCreate(title=record.display_name.strip())
        expected = _build_expected_graph(
            record,
            sources,
            chapters,
            completed_index,
            current_index,
            project_id,
            batch_id,
            payload,
        )

        def initialize(session, vault_root: Path) -> None:
            existing = session.get(ImportBatch, batch_id)
            if existing is not None:
                _verify_existing_import(session, vault_root, expected)
                return
            if not vault_root.name.startswith(f".creating-{project_id}-"):
                raise _error(
                    "IMPORT_PROJECT_COLLISION",
                    "The deterministic import Vault is incomplete.",
                    409,
                )

            batch = ImportBatch(
                id=batch_id,
                project_id=project_id,
                source_kind=record.source_kind,
                manifest=expected["manifest"],
                status="imported",
                completed_units=0,
                total_units=0,
            )
            session.add(batch)

            for spec in expected["nodes"]:
                node = StoryNode(
                    id=spec["id"],
                    project_id=project_id,
                    parent_id=None,
                    kind="chapter",
                    title=spec["title"],
                    order_index=spec["order_index"],
                    status=spec["status"],
                )
                session.add(node)
                session.flush()
                create_version(
                    session,
                    node.id,
                    content=spec["content"],
                    source="import",
                    version_id=spec["version_id"],
                )
                # Version creation invalidates derived summaries and normally marks
                # an edited chapter drafting.  Import boundaries are authoritative.
                node.status = spec["status"]

            for spec in expected["sources"].values():
                content = _read_verified_source(spec, spec["source_path"])
                target = (vault_root / PurePosixPath(spec["stored_path"])).resolve()
                if not target.is_relative_to(vault_root.resolve()):
                    raise _error("UNSAFE_IMPORT_PATH", "Stored source path escapes the Vault.")
                _copy_source(
                    spec["source_path"],
                    target,
                    spec["byte_hash"],
                    spec["size_bytes"],
                    vault_root,
                )
                row = SourceDocument(
                    id=spec["id"],
                    project_id=project_id,
                    import_batch_id=batch_id,
                    chapter_id=spec["chapter_id"],
                    relative_path=spec["relative_path"],
                    stored_path=spec["stored_path"],
                    category=spec["category"],
                    title=spec["title"],
                    content=content,
                    encoding=spec["encoding"],
                    size_bytes=spec["size_bytes"],
                    byte_hash=spec["byte_hash"],
                    content_hash=spec["content_hash"],
                    content_revision=spec["content_revision"],
                )
                session.add(row)
                session.flush()
                session.expunge(row)
                del content

            audit_ids = expected["continuation"]["source_document_ids"]
            session.add(
                ProjectContinuation(
                    project_id=project_id,
                    completed_through_node_id=expected["continuation"]["completed_through_node_id"],
                    current_chapter_id=expected["continuation"]["current_chapter_id"],
                    objective=record.continuation.objective,
                    source_document_ids=audit_ids,
                    revision=record.continuation.revision,
                )
            )

            pending_units: list[ImportAnalysisUnit] = []
            for spec in _iter_unit_specs(project_id, expected["sources"], expected["nodes"]):
                pending_units.append(
                    ImportAnalysisUnit(
                        **spec,
                        project_id=project_id,
                        import_batch_id=batch_id,
                        status="queued",
                    )
                )
                if len(pending_units) == 200:
                    session.add_all(pending_units)
                    session.flush()
                    for unit in pending_units:
                        session.expunge(unit)
                    pending_units.clear()
            if pending_units:
                session.add_all(pending_units)
                session.flush()
            batch.total_units = expected["unit_count"]

        def freeze_draft() -> None:
            if (
                record.committed_project_id == project_id
                and record.committed_batch_id == batch_id
                and record.committed_fingerprint == expected["fingerprint"]
            ):
                return
            frozen = record.model_copy(deep=True)
            frozen.committed_project_id = project_id
            frozen.committed_batch_id = batch_id
            frozen.committed_fingerprint = expected["fingerprint"]
            store._write_json(store._existing_draft_dir(record.draft_id), frozen)
            record.committed_project_id = project_id
            record.committed_batch_id = batch_id
            record.committed_fingerprint = expected["fingerprint"]

        project = registry._create_vault(
            payload,
            project_id,
            initialize,
            before_register=freeze_draft,
        )
        return {"project": project, "import_batch_id": batch_id}
