"""Validate untrusted backup archives in isolation before publishing a Vault."""

import hashlib
import json
import shutil
import sqlite3
import stat
import zipfile
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import HTTPException
from sqlalchemy import text

from novel_harness.db.models import Project
from novel_harness.db.session import Database
from novel_harness.services.import_drafts import normalize_relative_path
from novel_harness.services.serialization import serialize

MAX_ARCHIVE_BYTES = 200 * 1024 * 1024
MAX_EXPANDED_BYTES = 1024 * 1024 * 1024
MAX_FILES = 10000
MAX_METADATA_BYTES = 16 * 1024 * 1024
FORMAT = "novel-assistant-backup"


def _error(code="BACKUP_INVALID", message="备份文件无效或不完整，请重新选择原始备份。", status=422):
    return HTTPException(status, detail={"code": code, "message": message})


def _digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def backup_manifest(project_id, entries, files):
    manifest = {
        name: {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
        for name, content in entries.items()
    }
    for source, name in files:
        if name.casefold() in {key.casefold() for key in manifest}:
            raise _error(message="备份文件名重复，无法生成完整备份。")
        manifest[name] = {"sha256": _digest(source), "size": source.stat().st_size}
    return json.dumps(
        {"format": FORMAT, "version": 1, "project_id": project_id, "files": manifest},
        ensure_ascii=False,
    ).encode("utf-8")


def _safe_name(name):
    try:
        normalized = normalize_relative_path(name)
    except (HTTPException, ValueError):
        raise _error() from None
    if normalized != name or len(name) > 1000:
        raise _error()
    return normalized


def _identifier(name):
    return '"' + name.replace('"', '""') + '"'


def _database_info(folder, paths, verified):
    path = folder / "project.db"
    copies = []
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as database:
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA trusted_schema=OFF")
        schema = database.execute("SELECT type,name,sql FROM sqlite_master").fetchall()
        if any(row["type"] in {"trigger", "view"} for row in schema):
            raise _error(message="备份数据库含不支持的触发器或视图，未执行恢复。")
        for row in schema:
            if "CREATE VIRTUAL TABLE" in (row["sql"] or "").upper() and (
                row["name"] not in {"search_fts", "search_chunk_fts"}
                or "USING FTS5" not in row["sql"].upper()
            ):
                raise _error()
        if database.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise _error(message="备份数据库完整性检查失败。")
        if database.execute("PRAGMA foreign_key_check").fetchone():
            raise _error(message="备份数据库存在失效关联，未执行恢复。")
        projects = database.execute("SELECT * FROM projects").fetchall()
        if len(projects) != 1:
            raise _error(message="备份必须只包含一个小说项目。")
        project = dict(projects[0])
        from novel_harness.db.vault import ProjectVaultRegistry

        try:
            ProjectVaultRegistry._validate_id(project["id"])
        except HTTPException:
            raise _error() from None
        tables = {row["name"] for row in schema if row["type"] == "table"}
        for table in tables:
            columns = {
                row["name"] for row in database.execute(f"PRAGMA table_info({_identifier(table)})")
            }
            if (
                "project_id" in columns
                and database.execute(
                    f"SELECT 1 FROM {_identifier(table)} WHERE project_id IS NOT NULL "
                    "AND project_id != ? LIMIT 1",
                    (project["id"],),
                ).fetchone()
            ):
                raise _error(message="备份混入了其他项目的数据，未执行恢复。")
        if "assets" in tables:
            for asset in database.execute("SELECT relative_path,status FROM assets"):
                if not asset[0] and asset[1] != "ready":
                    continue
                target = _safe_name(asset[0])
                if not target.startswith("assets/"):
                    raise _error()
                source = target
                if source not in paths and not verified:
                    source = "assets/" + Path(target).name
                if source not in paths:
                    raise _error("BACKUP_ASSET_MISSING", "备份缺少数据库引用的素材文件。")
                copies.append((source, target))
        if "source_documents" in tables:
            for source in database.execute(
                "SELECT stored_path,size_bytes,byte_hash FROM source_documents"
            ):
                name = _safe_name(source[0])
                if (
                    not name.startswith("imports/")
                    or name not in paths
                    or paths[name]["size"] != source[1]
                    or paths[name]["sha256"] != source[2]
                ):
                    raise _error("BACKUP_SOURCE_MISSING", "备份缺少或改变了导入来源文件。")
                copies.append((name, name))
        counts = {}
        for name, table in {
            "chapters": "story_nodes",
            "versions": "chapter_versions",
            "jobs": "ai_jobs",
            "assets": "assets",
        }.items():
            where = " WHERE kind='chapter'" if name == "chapters" else ""
            counts[name] = (
                database.execute(f"SELECT count(*) FROM {_identifier(table)}{where}").fetchone()[0]
                if table in tables
                else 0
            )
    return project, counts, copies


@contextmanager
def _validated_archive(file):
    file.seek(0)
    digest, size = hashlib.sha256(), 0
    for block in iter(lambda: file.read(1024 * 1024), b""):
        size += len(block)
        if size > MAX_ARCHIVE_BYTES:
            raise _error("BACKUP_TOO_LARGE", "备份 ZIP 超过 200 MiB 上限。", 413)
        digest.update(block)
    file.seek(0)
    with TemporaryDirectory(prefix="novel-backup-check-") as temporary:
        folder = Path(temporary).resolve()
        try:
            with zipfile.ZipFile(file) as archive:
                infos = archive.infolist()
                if len(infos) > MAX_FILES:
                    raise _error("BACKUP_TOO_LARGE", "备份文件数量超过上限。")
                paths, names, total = {}, set(), 0
                for info in infos:
                    name = _safe_name(info.filename.rstrip("/") if info.is_dir() else info.filename)
                    if name.casefold() in names:
                        raise _error(message="备份包含重复文件路径。")
                    names.add(name.casefold())
                    mode = stat.S_IFMT(info.external_attr >> 16)
                    if mode not in {0, stat.S_IFREG, stat.S_IFDIR} or info.flag_bits & 1:
                        raise _error(message="备份含链接、特殊文件或加密文件，未执行恢复。")
                    allowed = name in {
                        "project.db",
                        "project.json",
                        "versions.json",
                        "assets-manifest.json",
                        "backup-manifest.json",
                    } or (name.split("/", 1)[0] in {"assets", "imports", "manuscript", "rag"})
                    if not allowed or (
                        name.startswith("rag/") and name != "rag/chapter-continuity-ledger.md"
                    ):
                        raise _error(message="备份包含不支持的文件。")
                    if info.is_dir():
                        continue
                    if name in {"project.json", "backup-manifest.json"} and (
                        info.file_size > MAX_METADATA_BYTES
                    ):
                        raise _error("BACKUP_TOO_LARGE", "备份说明文件超过安全上限。")
                    total += info.file_size
                    if (
                        total > MAX_EXPANDED_BYTES
                        or info.file_size / max(1, info.compress_size) > 1000
                    ):
                        raise _error("BACKUP_TOO_LARGE", "备份解压大小或压缩比超过安全上限。")
                    target = folder / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    actual, hashed = 0, hashlib.sha256()
                    with archive.open(info) as source, target.open("xb") as output:
                        for block in iter(lambda: source.read(1024 * 1024), b""):
                            actual += len(block)
                            if actual > info.file_size:
                                raise _error()
                            hashed.update(block)
                            output.write(block)
                    if actual != info.file_size:
                        raise _error()
                    paths[name] = {"size": actual, "sha256": hashed.hexdigest()}
                if not {"project.db", "project.json"}.issubset(paths):
                    raise _error(message="这不是完整项目备份：缺少数据库或项目说明。")
                verified = "backup-manifest.json" in paths
                manifest = (
                    json.loads((folder / "backup-manifest.json").read_bytes()) if verified else {}
                )
                if not isinstance(manifest, dict):
                    raise _error()
                if verified and (
                    manifest.get("format") != FORMAT
                    or manifest.get("version") != 1
                    or manifest.get("files")
                    != {
                        name: value
                        for name, value in paths.items()
                        if name != "backup-manifest.json"
                    }
                ):
                    raise _error("BACKUP_CHECKSUM_FAILED", "备份校验清单不匹配，文件可能已改变。")
                project, counts, copies = _database_info(folder, paths, verified)
                description = json.loads((folder / "project.json").read_bytes())
                if not isinstance(description, dict):
                    raise _error()
                if (
                    description.get("id") != project["id"]
                    or description.get("title") != project["title"]
                    or verified
                    and manifest.get("project_id") != project["id"]
                ):
                    raise _error(message="备份项目身份与数据库不一致。")
        except (
            sqlite3.Error,
            zipfile.BadZipFile,
            EOFError,
            ValueError,
            KeyError,
            TypeError,
            OSError,
        ):
            raise _error() from None
        yield folder, project, counts, copies, verified, digest.hexdigest()


def inspect_backup(file, registry):
    with _validated_archive(file) as (_, project, counts, _, verified, archive_hash):
        return {
            "project_id": project["id"],
            "title": project["title"],
            "format_version": 1 if verified else 0,
            "verified": verified,
            "counts": counts,
            "archive_hash": archive_hash,
            "conflict": registry._registration_row(project["id"]) is not None,
            "warnings": []
            if verified
            else ["旧备份没有逐文件哈希清单；已检查数据库、关联及来源文件，无法验证历史素材哈希。"],
        }


def restore_backup(file, archive_hash, registry):
    with _validated_archive(file) as (folder, project, _, copies, _, actual_hash):
        if actual_hash != archive_hash:
            raise _error("BACKUP_CHANGED", "文件与预检时不同，请重新检查备份。", 409)

        def populate(destination):
            shutil.copyfile(folder / "project.db", destination / "project.db")
            for source, target in dict.fromkeys(copies):
                path = destination / target
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(folder / source, path)
            database = Database(destination / "project.db")
            try:
                # Existing additive migrations also add newly introduced conversation tables.
                database.create_schema()
                with database.job_session_scope() as session:
                    session.execute(
                        text(
                            "UPDATE ai_jobs SET status='recovery_required', "
                            "error_code='BACKUP_RESTORED', "
                            "error_message='备份已恢复，请核对后手动继续任务。' "
                            "WHERE status IN ('queued','running','cancel_requested')"
                        )
                    )
                    session.execute(
                        text(
                            "UPDATE ai_job_controls SET worker_epoch=NULL, "
                            "authorized_stage=NULL, authorized_attempt_no=NULL, "
                            "control_revision=control_revision+1, "
                            "recovery_reason='backup_restored' WHERE job_id IN "
                            "(SELECT id FROM ai_jobs WHERE error_code='BACKUP_RESTORED')"
                        )
                    )
                    session.execute(
                        text("UPDATE import_batches SET status='paused' WHERE status='analyzing'")
                    )
                    result = serialize(session.get(Project, project["id"]))
                return result
            finally:
                database.dispose()

        return registry.restore_project(project["id"], actual_hash, populate)
