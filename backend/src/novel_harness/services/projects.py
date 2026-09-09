"""Project domain service."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import stat
import zipfile
from contextlib import closing
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from novel_harness.db.models import (
    Asset,
    ChapterDocument,
    ChapterVersion,
    ImportBatch,
    Project,
    SourceDocument,
    StoryNode,
)
from novel_harness.schemas.projects import ProjectCreate
from novel_harness.services.serialization import serialize


def require_project(session: Session, project_id: str) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail={"code": "PROJECT_NOT_FOUND"})
    return project


def create_project(session: Session, payload: ProjectCreate) -> Project:
    project = Project(**payload.model_dump())
    session.add(project)
    session.flush()
    return project


def list_projects(session: Session) -> list[Project]:
    return list(session.scalars(select(Project).order_by(Project.updated_at.desc())).all())


def project_progress(session: Session, project_id: str) -> dict:
    project = require_project(session, project_id)
    chapters = list(
        session.scalars(
            select(StoryNode)
            .where(StoryNode.project_id == project_id, StoryNode.kind == "chapter")
            .order_by(StoryNode.order_index, StoryNode.created_at)
        ).all()
    )
    documents = list(
        session.scalars(
            select(ChapterDocument).where(ChapterDocument.project_id == project_id)
        ).all()
    )
    current_words = 0
    visible_chapters = {chapter.id for chapter in chapters}
    for document in documents:
        if document.chapter_id not in visible_chapters:
            continue
        if document.current_version_id:
            version = session.get(ChapterVersion, document.current_version_id)
            if version:
                current_words += version.word_count
    return {
        "target_words": project.target_words,
        "current_words": current_words,
        "completion_ratio": min(1.0, current_words / project.target_words),
        "chapter_count": len(chapters),
        "completed_chapters": sum(chapter.status == "completed" for chapter in chapters),
        "daily_goal": project.daily_goal,
    }


def export_project(session: Session, project_id: str, data_dir: Path) -> bytes:
    # This read-only operation owns its transaction. Freeze all inputs together,
    # then release the writer lock before the potentially slow ZIP compression.
    with TemporaryDirectory(prefix="novel-export-") as staging:
        try:
            session.execute(text("BEGIN IMMEDIATE"))
            session.expire_all()
            entries, files = _capture_export_inputs(session, project_id, data_dir, Path(staging))
        finally:
            session.rollback()
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in entries.items():
                archive.writestr(name, content)
            for source, name in files:
                archive.write(source, name)
        return stream.getvalue()


def _capture_export_inputs(session: Session, project_id: str, data_dir: Path, staging: Path):
    """Called under the writer lock also used by committed asset-file deletion."""
    from novel_harness.services.chapter_summaries import ledger_text
    from novel_harness.services.retrieval import ordered_chapters

    project = require_project(session, project_id)
    chapters = [node for node in ordered_chapters(session) if node.project_id == project_id]
    documents = {
        document.chapter_id: document
        for document in session.scalars(
            select(ChapterDocument).where(ChapterDocument.project_id == project_id)
        ).all()
    }
    versions = [
        serialize(version)
        for version in session.scalars(
            select(ChapterVersion)
            .where(ChapterVersion.project_id == project_id)
            .order_by(ChapterVersion.created_at)
        ).all()
    ]
    assets = list(
        session.scalars(
            select(Asset)
            .where(Asset.project_id == project_id)
            .order_by(Asset.created_at)
            .execution_options(include_deleted=True)
        ).all()
    )
    batches = list(
        session.scalars(
            select(ImportBatch)
            .where(ImportBatch.project_id == project_id)
            .order_by(ImportBatch.created_at, ImportBatch.id)
        ).all()
    )
    import_sources = list(
        session.scalars(
            select(SourceDocument)
            .where(SourceDocument.project_id == project_id)
            .order_by(SourceDocument.import_batch_id, SourceDocument.relative_path)
            .execution_options(include_deleted=True)
        ).all()
    )

    def json_bytes(value: object) -> bytes:
        return json.dumps(value, ensure_ascii=False, indent=2, default=str).encode("utf-8")

    with closing(sqlite3.connect(data_dir / "project.db")) as source_db:
        with closing(sqlite3.connect(":memory:")) as snapshot:
            source_db.backup(snapshot)
            entries = {"project.db": snapshot.serialize()}
    entries.update({
        "rag/chapter-continuity-ledger.md": ledger_text(session).encode("utf-8"),
        "project.json": json_bytes(serialize(project)),
        "versions.json": json_bytes(versions),
        "assets-manifest.json": json_bytes([serialize(asset) for asset in assets]),
        "imports/manifest.json": json_bytes(
            {
                "batches": [
                    {
                        "id": batch.id,
                        "project_id": batch.project_id,
                        "source_kind": batch.source_kind,
                        "manifest": batch.manifest,
                        "created_at": batch.created_at,
                    }
                    for batch in batches
                ],
                "sources": [
                    {
                        key: getattr(source, key)
                        for key in (
                            "id",
                            "import_batch_id",
                            "chapter_id",
                            "relative_path",
                            "stored_path",
                            "category",
                            "title",
                            "encoding",
                            "size_bytes",
                            "byte_hash",
                            "content_hash",
                        )
                    }
                    for source in import_sources
                ],
            }
        ),
    })
    for position, chapter in enumerate(chapters, start=1):
        document = documents.get(chapter.id)
        content = document.content if document else ""
        entries[f"manuscript/{position:04d}-{chapter.id}.md"] = (
            f"# {chapter.title}\n\n{content}\n".encode()
        )
    files = []
    for position, asset in enumerate(assets):
        source = (data_dir / asset.relative_path).resolve()
        if source.is_relative_to((data_dir / "assets").resolve()) and source.is_file():
            staged = staging / str(position)
            shutil.copyfile(source, staged)
            files.append((staged, f"assets/{Path(asset.relative_path).name}"))
    archive_names = {name.casefold() for name in entries}
    for position, source_document in enumerate(import_sources):
        source, archive_name = _validated_import_source(data_dir, source_document.stored_path)
        if archive_name.casefold() in archive_names:
            raise _unsafe_import_source()
        staged = staging / f"import-{position:08d}"
        shutil.copyfile(source, staged)
        verified_source, verified_name = _validated_import_source(
            data_dir, source_document.stored_path
        )
        if verified_source != source or verified_name != archive_name:
            raise _unsafe_import_source()
        if (
            staged.stat().st_size != source_document.size_bytes
            or _file_digest(staged) != source_document.byte_hash
        ):
            raise _unsafe_import_source()
        archive_names.add(archive_name.casefold())
        files.append((staged, archive_name))
    return entries, files


def _unsafe_import_source() -> HTTPException:
    return HTTPException(
        422,
        detail={
            "code": "UNSAFE_IMPORT_SOURCE",
            "message": "An imported source cannot be exported safely.",
        },
    )


def _is_reparse_point(path: Path) -> bool:
    metadata = os.lstat(path)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & reparse_flag
    )


def _validated_import_source(data_dir: Path, stored_path: str) -> tuple[Path, str]:
    from novel_harness.services.import_drafts import normalize_relative_path

    try:
        archive_name = normalize_relative_path(stored_path)
        parts = PurePosixPath(archive_name).parts
        if not parts or parts[0] != "imports" or archive_name == "imports/manifest.json":
            raise _unsafe_import_source()
        vault_root = data_dir.resolve(strict=True)
        import_root = data_dir / "imports"
        current = data_dir
        for part in parts:
            current = current / part
            if _is_reparse_point(current):
                raise _unsafe_import_source()
        resolved_root = import_root.resolve(strict=True)
        resolved_source = current.resolve(strict=True)
        if (
            not resolved_root.is_relative_to(vault_root)
            or not resolved_source.is_relative_to(resolved_root)
            or not stat.S_ISREG(os.lstat(resolved_source).st_mode)
        ):
            raise _unsafe_import_source()
    except HTTPException as exc:
        if exc.detail.get("code") == "UNSAFE_IMPORT_SOURCE":
            raise
        raise _unsafe_import_source() from exc
    except (OSError, RuntimeError, ValueError) as exc:
        raise _unsafe_import_source() from exc
    return resolved_source, archive_name


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(64 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
