"""Opt-in local backups, using the same validated archive as manual export."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import sqlite3
import zipfile
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from threading import Event, Lock, Thread
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from novel_harness.db.vault import _fsync_directory, _path_is_reparse_point
from novel_harness.services.backup_restore import inspect_backup
from novel_harness.services.projects import export_project


class BackupPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0)
    enabled: bool
    interval_hours: int = Field(default=24, ge=1, le=168)
    retention_count: int = Field(default=7, ge=1, le=30)


def failure(code, message, status=409):
    return HTTPException(status, detail={"code": code, "message": message})


class AutomaticBackups:
    def __init__(self, registry):
        self.registry = registry
        folder = registry.root / ".settings"
        folder.mkdir(exist_ok=True)
        if _path_is_reparse_point(folder):
            raise RuntimeError("UNSAFE_BACKUP_SETTINGS")
        self.path = folder / "automatic-backups.db"
        if _path_is_reparse_point(self.path):
            raise RuntimeError("UNSAFE_BACKUP_SETTINGS")
        self.run_lock = Lock()
        self.stop_event = Event()
        self.thread = None
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS backup_policy (
                project_id TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 0, interval_hours INTEGER NOT NULL DEFAULT 24,
                retention_count INTEGER NOT NULL DEFAULT 7, last_checked_at TEXT,
                last_success_at TEXT, last_error TEXT, fingerprint TEXT)""")
            db.execute("""CREATE TABLE IF NOT EXISTS automatic_backup (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, created_at TEXT NOT NULL,
                size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS automatic_backup_pending (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, created_at TEXT NOT NULL,
                size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, fingerprint TEXT NOT NULL)""")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _policy(self, db, pid):
        db.execute("INSERT OR IGNORE INTO backup_policy(project_id) VALUES (?)", (pid,))
        return dict(db.execute("SELECT * FROM backup_policy WHERE project_id=?", (pid,)).fetchone())

    def report(self, pid):
        self.registry.require(pid)
        # An active writer owns its pending intent until it releases this lock.
        if self.run_lock.acquire(blocking=False):
            try:
                self._recover_pending(pid)
            except (HTTPException, OSError, sqlite3.Error, ValueError):
                self._record_error(pid)
            finally:
                self.run_lock.release()
        with self.connect() as db:
            row = self._policy(db, pid)
            backups = [
                dict(item)
                for item in db.execute(
                    "SELECT * FROM automatic_backup WHERE project_id=? ORDER BY rowid DESC",
                    (pid,),
                )
            ]
        return {
            "settings": {
                key: (bool(row[key]) if key == "enabled" else row[key])
                for key in ("revision", "enabled", "interval_hours", "retention_count")
            },
            "last_checked_at": row["last_checked_at"],
            "last_success_at": row["last_success_at"],
            "last_error": row["last_error"],
            "backups": backups,
        }

    def configure(self, pid, value):
        self.registry.require(pid)
        policy = BackupPolicy.model_validate(value)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._policy(db, pid)
            if current["revision"] != policy.revision:
                raise failure(
                    "BACKUP_SETTINGS_CHANGED", "备份设置已在其他页面修改，请刷新后再保存。"
                )
            db.execute(
                """UPDATE backup_policy SET revision=revision+1, enabled=?,
                interval_hours=?, retention_count=? WHERE project_id=?""",
                (policy.enabled, policy.interval_hours, policy.retention_count, pid),
            )
        return self.report(pid)

    def _folder(self, pid):
        root = self.registry.require(pid).root.resolve()
        folder = root
        for name in ("backups", "automatic"):
            folder = folder / name
            if _path_is_reparse_point(folder):
                raise failure("BACKUP_PATH_INVALID", "备份目录路径无效，未写入或删除任何文件。")
            folder.mkdir(exist_ok=True)
            if folder.resolve() != folder or not folder.resolve().is_relative_to(root):
                raise failure("BACKUP_PATH_INVALID", "备份目录路径无效。")
        return folder

    def _file(self, pid, identifier):
        if not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise failure("BACKUP_NOT_FOUND", "找不到这份备份。", 404)
        path = self._folder(pid) / f"{identifier}.zip"
        if _path_is_reparse_point(path) or (path.exists() and not path.is_file()):
            raise failure("BACKUP_PATH_INVALID", "备份文件路径无效。")
        return path

    def read(self, pid, identifier):
        self.registry.require(pid)
        with self.connect() as db:
            record = db.execute(
                "SELECT * FROM automatic_backup WHERE project_id=? AND id=?", (pid, identifier)
            ).fetchone()
        if not record:
            raise failure("BACKUP_NOT_FOUND", "找不到这份备份。", 404)
        try:
            data = self._file(pid, identifier).read_bytes()
        except FileNotFoundError:
            raise failure("BACKUP_NOT_FOUND", "备份文件已移走或删除。", 404) from None
        if hashlib.sha256(data).hexdigest() != record["sha256"]:
            raise failure("BACKUP_DAMAGED", "备份文件校验失败，请使用其他备份。")
        return data

    @staticmethod
    def _fingerprint(data):
        with zipfile.ZipFile(io.BytesIO(data)) as package:
            manifest = json.loads(package.read("backup-manifest.json"))
        return hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()

    def _record_error(self, pid, timestamp=None):
        with self.connect() as db:
            self._policy(db, pid)
            db.execute(
                "UPDATE backup_policy SET last_checked_at=COALESCE(?,last_checked_at), "
                "last_error=? WHERE project_id=?",
                (timestamp, "备份或保留清理未完成，请检查磁盘空间及备份文件后重试。", pid),
            )

    def _finish_pending(self, record):
        # Catalog publication and intent removal commit together. Retrying after
        # a process loss cannot leave a catalog row with an abandoned intent.
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT * FROM automatic_backup WHERE id=?",
                (record["id"],),
            ).fetchone()
            values = tuple(
                record[key]
                for key in (
                    "id",
                    "project_id",
                    "created_at",
                    "size_bytes",
                    "sha256",
                )
            )
            if previous is None:
                db.execute("INSERT INTO automatic_backup VALUES (?,?,?,?,?)", values)
            elif tuple(previous) != values:
                raise failure(
                    "BACKUP_CATALOG_CONFLICT",
                    "备份登记与文件身份不一致，请保留现场检查。",
                )
            db.execute(
                "UPDATE backup_policy SET fingerprint=?,last_success_at=?,last_error=NULL "
                "WHERE project_id=?",
                (record["fingerprint"], record["created_at"], record["project_id"]),
            )
            db.execute("DELETE FROM automatic_backup_pending WHERE id=?", (record["id"],))

    def _recover_pending(self, pid):
        """Recover only durable intents; never enumerate or delete unknown files."""
        with self.connect() as db:
            pending = list(
                db.execute(
                    "SELECT * FROM automatic_backup_pending WHERE project_id=? ORDER BY rowid",
                    (pid,),
                )
            )
        recovered = False
        for record in pending:
            path = self._file(pid, record["id"])
            temporary = path.with_suffix(".tmp")
            if _path_is_reparse_point(temporary) or (
                temporary.exists() and not temporary.is_file()
            ):
                raise failure("BACKUP_PATH_INVALID", "备份临时文件路径无效，未执行清理。")
            if path.exists():
                if path.stat().st_size != record["size_bytes"]:
                    raise failure("BACKUP_DAMAGED", "待登记备份大小不符，请保留文件并检查磁盘。")
                data = path.read_bytes()
                if (
                    hashlib.sha256(data).hexdigest() != record["sha256"]
                    or self._fingerprint(data) != record["fingerprint"]
                ):
                    raise failure("BACKUP_DAMAGED", "待登记备份校验失败，请保留文件并检查磁盘。")
                preview = inspect_backup(io.BytesIO(data), self.registry)
                if preview["project_id"] != pid:
                    raise failure("BACKUP_CATALOG_CONFLICT", "待登记备份不属于当前项目。")
                temporary.unlink(missing_ok=True)
                self._finish_pending(record)
                recovered = True
            else:
                # A pre-publication interruption has no completed archive.
                # Remove only the exact temporary path owned by this intent.
                temporary.unlink(missing_ok=True)
                with self.connect() as db:
                    db.execute("DELETE FROM automatic_backup_pending WHERE id=?", (record["id"],))
        if recovered:
            self._prune(pid)

    def run(self, pid, now=None):
        self.registry.require(pid)
        if not self.run_lock.acquire(blocking=False):
            raise failure("BACKUP_BUSY", "已有备份正在处理，请稍后重试。")
        now = now or datetime.now(UTC)
        timestamp = now.isoformat()
        temporary = None
        try:
            self._recover_pending(pid)
            with self.connect() as db:
                self._policy(db, pid)
            vault = self.registry.require(pid)
            with vault.database.session_scope() as session:
                data = export_project(session, pid, vault.root)
            inspect_backup(io.BytesIO(data), self.registry)
            fingerprint = self._fingerprint(data)
            # A lost/corrupted last backup must be replaced even if the source is unchanged.
            latest = self.report(pid)["backups"]
            previous_fingerprint = None
            if latest:
                try:
                    previous_fingerprint = self._fingerprint(self.read(pid, latest[0]["id"]))
                except (HTTPException, zipfile.BadZipFile, KeyError, ValueError):
                    pass
            result = "unchanged"
            if fingerprint != previous_fingerprint:
                identifier = uuid4().hex
                path = self._file(pid, identifier)
                temporary = path.with_suffix(".tmp")
                record = dict(
                    id=identifier,
                    project_id=pid,
                    created_at=timestamp,
                    size_bytes=len(data),
                    sha256=hashlib.sha256(data).hexdigest(),
                    fingerprint=fingerprint,
                )
                with self.connect() as db:
                    db.execute(
                        "INSERT INTO automatic_backup_pending VALUES (?,?,?,?,?,?)",
                        tuple(record.values()),
                    )
                with temporary.open("xb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
                _fsync_directory(path.parent)
                self._finish_pending(record)
                result = "created"
            self._prune(pid)
            with self.connect() as db:
                db.execute(
                    "UPDATE backup_policy SET last_checked_at=?,last_error=NULL WHERE project_id=?",
                    (timestamp, pid),
                )
            return {**self.report(pid), "result": result}
        except Exception:
            self._record_error(pid, timestamp)
            raise
        finally:
            try:
                if (
                    temporary is not None
                    and temporary.exists()
                    and not _path_is_reparse_point(temporary)
                ):
                    temporary.unlink()
            except OSError:
                logging.getLogger(__name__).warning("automatic_backup_temp_cleanup_failed")
            finally:
                self.run_lock.release()

    def _prune(self, pid):
        with self.connect() as db:
            policy = self._policy(db, pid)
            old = list(
                db.execute(
                    """SELECT id FROM automatic_backup WHERE project_id=?
                ORDER BY rowid DESC LIMIT -1 OFFSET ?""",
                    (pid, policy["retention_count"]),
                )
            )
        for row in old:
            self._file(pid, row["id"]).unlink(missing_ok=True)
            with self.connect() as db:
                db.execute(
                    "DELETE FROM automatic_backup WHERE project_id=? AND id=?", (pid, row["id"])
                )

    def tick(self, now=None):
        now = now or datetime.now(UTC)
        with self.connect() as db:
            policies = list(db.execute("SELECT * FROM backup_policy WHERE enabled=1"))
        for policy in policies:
            checked = policy["last_checked_at"]
            if checked and datetime.fromisoformat(checked) <= now < datetime.fromisoformat(
                checked
            ) + timedelta(hours=policy["interval_hours"]):
                continue
            try:
                self.run(policy["project_id"], now)
            except Exception:
                logging.getLogger(__name__).warning("automatic_backup_failed")

    def start(self):
        if self.thread is not None:
            return

        def loop():
            while not self.stop_event.wait(60):
                try:
                    self.tick()
                except Exception:
                    logging.getLogger(__name__).warning("automatic_backup_scheduler_failed")

        self.thread = Thread(target=loop, name="novel-backups", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join()
