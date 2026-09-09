import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from importlib import import_module, util

import pytest
from pydantic import ValidationError

from novel_harness.db import models
from novel_harness.db.migration import migrate_legacy_database
from novel_harness.db.models import ChapterVersion, Project, StoryNode
from novel_harness.db.session import Database
from novel_harness.db.vault import ProjectVaultRegistry

IMPORT_TABLES = {
    "import_batches",
    "source_documents",
    "project_continuations",
    "import_analysis_units",
    "memory_candidates",
}


def _import_models():
    names = (
        "ImportBatch",
        "SourceDocument",
        "ProjectContinuation",
        "ImportAnalysisUnit",
        "MemoryCandidate",
    )
    missing = [name for name in names if not hasattr(models, name)]
    assert missing == [], f"missing import persistence models: {missing}"
    return tuple(getattr(models, name) for name in names)


def _add_import_graph(session, project_id: str):
    ImportBatch, SourceDocument, ProjectContinuation, ImportAnalysisUnit, MemoryCandidate = (
        _import_models()
    )
    chapter = StoryNode(
        id=f"chapter-{project_id}",
        project_id=project_id,
        kind="chapter",
        title="第一章",
    )
    session.add(chapter)
    session.flush()
    version = ChapterVersion(
        id=f"version-{project_id}",
        project_id=project_id,
        chapter_id=chapter.id,
        content="夜雨落在旧城。",
    )
    batch = ImportBatch(
        id=f"batch-{project_id}",
        project_id=project_id,
        source_kind="folder",
        manifest={"root": "旧稿"},
        total_units=1,
    )
    session.add_all([version, batch])
    session.flush()
    source = SourceDocument(
        id=f"source-{project_id}",
        project_id=project_id,
        import_batch_id=batch.id,
        chapter_id=chapter.id,
        relative_path="正文/第一章.txt",
        stored_path="imports/batch/第一章.txt",
        category="manuscript",
        title="第一章",
        content="夜雨落在旧城。",
        encoding="utf-8",
        byte_hash="a" * 64,
        content_hash=hashlib.sha256("夜雨落在旧城。".encode()).hexdigest(),
    )
    session.add(source)
    session.flush()
    continuation = ProjectContinuation(
        project_id=project_id,
        completed_through_node_id=chapter.id,
        current_chapter_id=chapter.id,
        objective="接着调查失踪案",
        source_document_ids=[source.id],
    )
    unit = ImportAnalysisUnit(
        id=f"unit-{project_id}",
        project_id=project_id,
        import_batch_id=batch.id,
        source_document_id=source.id,
        chapter_id=chapter.id,
        chunk_key="0:8",
        unit_key="source:0",
        kind="chapter",
        source_hash="c" * 64,
    )
    candidate = MemoryCandidate(
        id=f"candidate-{project_id}",
        project_id=project_id,
        import_batch_id=batch.id,
        source_document_id=source.id,
        chapter_id=chapter.id,
        source_version_id=version.id,
        kind="chapter_summary",
        payload={"recap": "雨夜失踪案开始。"},
        evidence=[{"quote": "夜雨落在旧城。", "relative_path": source.relative_path}],
        source_hash="d" * 64,
        dedupe_key="e" * 64,
    )
    session.add_all([continuation, unit, candidate])
    session.flush()
    return batch.id, source.id, project_id, unit.id, candidate.id


def test_project_vault_creates_import_tables_and_round_trips_foreign_keys(client, tmp_path):
    project = client.post("/api/v1/projects", json={"title": "旧稿导入"}).json()
    path = tmp_path / "projects" / project["id"] / "project.db"

    with closing(sqlite3.connect(path)) as connection, connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert IMPORT_TABLES <= tables

    database = Database(path)
    with database.session_scope() as session:
        keys = _add_import_graph(session, project["id"])
    with database.session_scope() as session:
        classes = _import_models()
        round_tripped = tuple(
            session.get(cls, key) for cls, key in zip(classes, keys, strict=True)
        )
        assert all(record is not None for record in round_tripped)
        batch, source, continuation, unit, candidate = round_tripped
        assert source.import_batch_id == batch.id
        assert continuation.source_document_ids == [source.id]
        assert unit.source_document_id == source.id
        assert candidate.source_version_id == f"version-{project['id']}"
        assert candidate.payload == {"recap": "雨夜失踪案开始。"}
    database.dispose()


def test_legacy_migration_uses_project_id_ownership_for_import_tables(tmp_path):
    legacy = Database(tmp_path / "novel-harness.db")
    legacy.create_schema()
    with legacy.session_scope() as session:
        session.add_all([Project(id="alpha", title="甲书"), Project(id="beta", title="乙书")])
        session.flush()
        _add_import_graph(session, "alpha")
        _add_import_graph(session, "beta")
    legacy.dispose()

    registry = ProjectVaultRegistry(tmp_path)
    migrate_legacy_database(registry)
    schemas = import_module("novel_harness.schemas.imports")
    classes = _import_models()
    for project_id, other_id in (("alpha", "beta"), ("beta", "alpha")):
        migrated = registry.require(project_id)
        own_keys = (
            f"batch-{project_id}",
            f"source-{project_id}",
            project_id,
            f"unit-{project_id}",
            f"candidate-{project_id}",
        )
        other_keys = (
            f"batch-{other_id}",
            f"source-{other_id}",
            other_id,
            f"unit-{other_id}",
            f"candidate-{other_id}",
        )
        with migrated.database.session_scope() as session:
            own_records = tuple(
                session.get(cls, key)
                for cls, key in zip(classes, own_keys, strict=True)
            )
            assert all(record is not None for record in own_records)
            assert all(
                session.get(cls, key) is None
                for cls, key in zip(classes, other_keys, strict=True)
            )
            assert schemas.ImportBatchRead.model_validate(own_records[0]).source_kind == "folder"
        with closing(sqlite3.connect(migrated.database.path)) as connection, connection:
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    registry.dispose()


def test_import_mutation_schemas_are_strict_and_bounded():
    module = util.find_spec("novel_harness.schemas.imports")
    assert module is not None, "missing novel_harness.schemas.imports"
    schemas = import_module("novel_harness.schemas.imports")

    draft = schemas.ImportDraftPatch(
        revision=1,
        objective="续写雨夜调查",
        continuation={"source_document_ids": ["source-1"]},
    )
    assert draft.revision == 1
    with pytest.raises(ValidationError):
        schemas.ImportDraftPatch(revision=1, unexpected=True)
    with pytest.raises(ValidationError):
        schemas.ImportDraftPatch(revision=1, continuation={"unexpected": True})
    with pytest.raises(ValidationError):
        schemas.ImportDraftPatch(revision=0)
    with pytest.raises(ValidationError):
        schemas.ImportFilePreview(
            relative_path="a" * 4097,
            category="manuscript",
            title="第一章",
        )
    with pytest.raises(ValidationError):
        schemas.MemoryCandidateDecision(revision=1, decision="maybe")

    revision_factories = (
        lambda revision: schemas.ImportDraftPatch(revision=revision),
        lambda revision: schemas.MemoryCandidatePatch(
            revision=revision, payload={"predicate": "rule"}
        ),
        lambda revision: schemas.MemoryCandidateDecision(
            revision=revision, decision="confirm"
        ),
        lambda revision: schemas.MemoryCandidateBulkConfirmEntry(
            candidate_id="candidate-1", revision=revision
        ),
        lambda revision: schemas.ImportContinuation(revision=revision),
    )
    for factory in revision_factories:
        assert factory(1).revision == 1
        for invalid_revision in (True, "1", 1.0):
            with pytest.raises(ValidationError):
                factory(invalid_revision)

    assert schemas.ImportCommitRequest(expected_revision=1).expected_revision == 1
    for invalid_revision in (True, "1", 1.0, 0):
        with pytest.raises(ValidationError):
            schemas.ImportCommitRequest(expected_revision=invalid_revision)
    with pytest.raises(ValidationError):
        schemas.ImportCommitRequest(revision=1)

    for invalid_bool in ("true", 1):
        with pytest.raises(ValidationError):
            schemas.ImportFilePreview(
                relative_path="第一章.txt",
                category="manuscript",
                title="第一章",
                selected=invalid_bool,
            )
    for invalid_int in ("1", True, 1.0):
        with pytest.raises(ValidationError):
            schemas.ImportFilePreview(
                relative_path="第一章.txt",
                category="manuscript",
                title="第一章",
                size_bytes=invalid_int,
            )
        with pytest.raises(ValidationError):
            schemas.ImportChapterPreview(
                relative_path="第一章.txt",
                title="第一章",
                order_index=invalid_int,
            )


def test_import_read_schemas_are_bounded_and_round_trip():
    schemas = import_module("novel_harness.schemas.imports")
    now = datetime.now(UTC)
    batch_data = {
        "id": "batch-1",
        "project_id": "project-1",
        "source_kind": "folder",
        "manifest": {"root": "旧稿"},
        "status": "imported",
        "completed_units": 0,
        "total_units": 1,
        "last_error": "",
        "created_at": now,
        "updated_at": now,
    }
    analysis_data = {
        "id": "unit-1",
        "project_id": "project-1",
        "import_batch_id": "batch-1",
        "source_document_id": "source-1",
        "chapter_id": "chapter-1",
        "chunk_key": "0:8",
        "unit_key": "source:0",
        "kind": "chapter",
        "source_hash": "a" * 64,
        "status": "completed",
        "attempt_count": 1,
        "worker_epoch": None,
        "error_code": None,
        "error_message": "",
        "result": {"candidate_count": 1, "verified": True},
        "created_at": now,
        "updated_at": now,
    }
    candidate_data = {
        "id": "candidate-1",
        "project_id": "project-1",
        "import_batch_id": "batch-1",
        "source_document_id": "source-1",
        "chapter_id": "chapter-1",
        "source_version_id": "version-1",
        "kind": "chapter_summary",
        "payload": {"recap": "雨夜失踪案开始。"},
        "evidence": [{"quote": "夜雨落在旧城。", "relative_path": "正文/第一章.txt"}],
        "source_hash": "b" * 64,
        "dedupe_key": "c" * 64,
        "status": "pending",
        "revision": 1,
        "conflict": {},
        "promoted_type": None,
        "promoted_record_id": None,
        "created_at": now,
        "updated_at": now,
    }

    batch = schemas.ImportBatchRead.model_validate(batch_data)
    analysis = schemas.ImportAnalysisRead.model_validate(analysis_data)
    candidate = schemas.MemoryCandidateRead.model_validate(candidate_data)
    assert schemas.ImportBatchRead.model_validate(batch.model_dump()) == batch
    assert schemas.ImportAnalysisRead.model_validate(analysis.model_dump()) == analysis
    assert schemas.MemoryCandidateRead.model_validate(candidate.model_dump()) == candidate
    assert analysis.result["verified"] is True

    for field in ("completed_units", "total_units"):
        with pytest.raises(ValidationError):
            schemas.ImportBatchRead.model_validate(batch_data | {field: -1})
    with pytest.raises(ValidationError):
        schemas.ImportBatchRead.model_validate(batch_data | {"source_kind": "directory"})
    with pytest.raises(ValidationError):
        schemas.ImportBatchRead.model_validate(batch_data | {"id": "x" * 10_000})
    with pytest.raises(ValidationError):
        schemas.ImportBatchRead.model_validate(
            batch_data | {"manifest": {str(index): index for index in range(1000)}}
        )
    with pytest.raises(ValidationError):
        schemas.ImportBatchRead.model_validate(
            batch_data | {"manifest": {"nested": {str(index): index for index in range(1000)}}}
        )
    with pytest.raises(ValidationError):
        schemas.ImportAnalysisRead.model_validate(analysis_data | {"attempt_count": -1})
    with pytest.raises(ValidationError):
        schemas.ImportAnalysisRead.model_validate(
            analysis_data | {"error_message": "x" * 10_000}
        )
    with pytest.raises(ValidationError):
        schemas.ImportAnalysisRead.model_validate(
            analysis_data | {"result": {str(index): index for index in range(1000)}}
        )
    with pytest.raises(ValidationError):
        schemas.ImportAnalysisRead.model_validate(
            analysis_data | {"result": {"value": 10**100_000}}
        )
    with pytest.raises(ValidationError):
        schemas.ImportAnalysisRead.model_validate(
            analysis_data | {"result": {"value": float("nan")}}
        )
    infinite_json = analysis.model_dump(mode="json")
    infinite_json["result"] = {"value": float("inf")}
    with pytest.raises(ValidationError):
        schemas.ImportAnalysisRead.model_validate_json(json.dumps(infinite_json))
    with pytest.raises(ValidationError):
        schemas.MemoryCandidateRead.model_validate(candidate_data | {"revision": -1})
    with pytest.raises(ValidationError):
        schemas.MemoryCandidateRead.model_validate(
            candidate_data | {"payload": {str(index): index for index in range(1000)}}
        )
    with pytest.raises(ValidationError):
        schemas.MemoryCandidateRead.model_validate(
            candidate_data | {"conflict": {str(index): index for index in range(1000)}}
        )
    with pytest.raises(ValidationError):
        schemas.MemoryCandidateRead.model_validate(
            candidate_data | {"evidence": [{} for _ in range(1000)]}
        )
    with pytest.raises(ValidationError):
        schemas.MemoryCandidatePage(
            items=[candidate for _ in range(10_000)],
            total=10_000,
            counts={"pending": 10_000},
            next_cursor=None,
        )
