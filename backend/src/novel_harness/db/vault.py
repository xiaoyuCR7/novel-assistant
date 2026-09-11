"""Workspace registry contains metadata only; content lives in separate SQLite files."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import stat
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Lock, RLock
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from novel_harness.db.models import Project, new_id
from novel_harness.db.session import Database
from novel_harness.schemas.projects import ProjectCreate
from novel_harness.services.serialization import serialize

_REPARSE_POINT_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_PROJECT_LOCKS_GUARD = Lock()
_PROJECT_LOCKS: dict[str, tuple[Lock, int]] = {}


def _path_is_reparse_point(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT_ATTRIBUTE
    )


def _fsync_directory(path: Path) -> None:
    """Best-effort durability barrier for directory entry changes."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _fsync_directory_chain(start: Path, stop_inclusive: Path) -> None:
    """Best-effort fsync of each safe directory from ``start`` through ``stop``."""
    start = Path(start)
    stop_inclusive = Path(stop_inclusive)
    try:
        resolved_start = start.resolve(strict=True)
        resolved_stop = stop_inclusive.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("UNSAFE_FSYNC_DIRECTORY_CHAIN") from exc
    if (
        resolved_start != start
        or resolved_stop != stop_inclusive
        or not resolved_start.is_relative_to(resolved_stop)
    ):
        raise RuntimeError("UNSAFE_FSYNC_DIRECTORY_CHAIN")
    current = resolved_start
    while True:
        if not current.is_dir() or _path_is_reparse_point(current):
            raise RuntimeError("UNSAFE_FSYNC_DIRECTORY_CHAIN")
        try:
            _fsync_directory(current)
        except OSError:
            pass
        if current == resolved_stop:
            return
        current = current.parent


class _ProjectCreationLock:
    def __init__(self, projects_root: Path, project_id: str) -> None:
        self.path = projects_root / ".locks" / f"{project_id}.lock"
        self.key = os.path.normcase(str(self.path.resolve()))
        self.lock: Lock | None = None
        self.handle = None

    def __enter__(self):
        with _PROJECT_LOCKS_GUARD:
            lock, users = _PROJECT_LOCKS.get(self.key, (Lock(), 0))
            _PROJECT_LOCKS[self.key] = (lock, users + 1)
            self.lock = lock
        if not lock.acquire(blocking=False):
            self._release_registry_entry()
            raise HTTPException(409, detail={"code": "PROJECT_CREATION_BUSY"})
        try:
            lock_root = self.path.parent
            lock_root.mkdir(exist_ok=True)
            if (
                _path_is_reparse_point(lock_root)
                or lock_root.resolve() != lock_root
                or lock_root.parent != self.path.parent.parent
            ):
                raise RuntimeError("UNSAFE_PROJECT_LOCK_ROOT")
            try:
                lock_metadata = self.path.lstat()
            except FileNotFoundError:
                lock_metadata = None
            if lock_metadata is not None:
                if (
                    _path_is_reparse_point(self.path)
                    or not stat.S_ISREG(lock_metadata.st_mode)
                ):
                    raise RuntimeError("UNSAFE_PROJECT_LOCK_FILE")
                try:
                    resolved_lock = self.path.resolve(strict=True)
                except OSError as exc:
                    raise RuntimeError("UNSAFE_PROJECT_LOCK_FILE") from exc
                if resolved_lock.parent != lock_root:
                    raise RuntimeError("UNSAFE_PROJECT_LOCK_FILE")
            self.handle = self.path.open("a+b")
            self.handle.seek(0, os.SEEK_END)
            if self.handle.tell() == 0:
                self.handle.write(b"\0")
                self.handle.flush()
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if self.handle is not None:
                self.handle.close()
                self.handle = None
            lock.release()
            self._release_registry_entry()
            raise HTTPException(409, detail={"code": "PROJECT_CREATION_BUSY"}) from exc
        except BaseException:
            if self.handle is not None:
                self.handle.close()
                self.handle = None
            lock.release()
            self._release_registry_entry()
            raise
        return self

    def __exit__(self, *_args):
        try:
            if self.handle is not None:
                try:
                    self.handle.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
                self.handle.close()
                self.handle = None
        finally:
            assert self.lock is not None
            self.lock.release()
            self._release_registry_entry()

    def _release_registry_entry(self) -> None:
        with _PROJECT_LOCKS_GUARD:
            lock, users = _PROJECT_LOCKS[self.key]
            if users == 1:
                del _PROJECT_LOCKS[self.key]
            else:
                _PROJECT_LOCKS[self.key] = (lock, users - 1)


@dataclass(frozen=True)
class ProjectVault:
    project_id: str
    root: Path
    database: Database

    def resolve(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise HTTPException(400, detail={"code": "UNSAFE_VAULT_PATH"})
        return path


class ProjectVaultRegistry:
    def __init__(self, data_dir: Path):
        self.root = data_dir.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "workspace.db"
        self._vaults: dict[str, ProjectVault] = {}
        self._lock = RLock()
        self._maintained: set[str] = set()
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS project_registry (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, vault_path TEXT NOT NULL UNIQUE,
                cover_path TEXT NOT NULL DEFAULT '', last_opened_at TEXT NOT NULL,
                migration_status TEXT NOT NULL DEFAULT 'complete')""")

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def register(self, project_id: str, title: str) -> None:
        self._validate_id(project_id)
        try:
            with self.connect() as db:
                db.execute(
                    """INSERT INTO project_registry
                    (id,title,vault_path,last_opened_at)
                    VALUES (?,?,?,strftime('%Y-%m-%dT%H:%M:%f','now'))
                    """,
                    (project_id, title, f"projects/{project_id}"),
                )
        except sqlite3.IntegrityError as exc:
            if self._registration_matches(project_id, title):
                return
            raise HTTPException(
                409, detail={"code": "PROJECT_REGISTRY_CONFLICT"}
            ) from exc

    def _registration_row(self, project_id: str) -> sqlite3.Row | None:
        with self.connect() as db:
            return db.execute(
                "SELECT * FROM project_registry WHERE id=?", (project_id,)
            ).fetchone()

    def _registration_matches(self, project_id: str, title: str) -> bool:
        row = self._registration_row(project_id)
        return row is not None and {
            "id": row["id"],
            "title": row["title"],
            "vault_path": row["vault_path"],
            "cover_path": row["cover_path"],
            "migration_status": row["migration_status"],
        } == {
            "id": project_id,
            "title": title,
            "vault_path": f"projects/{project_id}",
            "cover_path": "",
            "migration_status": "complete",
        }

    def _register_or_reconcile(self, project_id: str, title: str) -> None:
        try:
            self.register(project_id, title)
        except Exception:
            if self._registration_matches(project_id, title):
                return
            raise

    @staticmethod
    def _validate_id(project_id: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", project_id):
            raise HTTPException(404, detail={"code": "PROJECT_NOT_FOUND"})

    def require(self, project_id: str, *, job_only=False) -> ProjectVault:
        self._validate_id(project_id)
        with self._lock:
            if project_id in self._vaults:
                vault = self._vaults[project_id]
                if not job_only:
                    self._maintain(vault)
                return vault
            with self.connect() as db:
                row = db.execute(
                    "SELECT * FROM project_registry WHERE id=?", (project_id,)
                ).fetchone()
            if row is None:
                raise HTTPException(404, detail={"code": "PROJECT_NOT_FOUND"})
            projects_path = self.root / "projects"
            if (
                _path_is_reparse_point(projects_path)
                or projects_path.resolve() != projects_path
            ):
                raise HTTPException(404, detail={"code": "PROJECT_NOT_FOUND"})
            raw_root = self.root / row["vault_path"]
            root = raw_root.resolve()
            if not root.is_relative_to(projects_path):
                raise HTTPException(404, detail={"code": "PROJECT_NOT_FOUND"})
            if _path_is_reparse_point(raw_root):
                raise HTTPException(404, detail={"code": "PROJECT_NOT_FOUND"})
            if not (root / "project.db").is_file():
                raise HTTPException(503, detail={"code": "VAULT_UNAVAILABLE"})
            database = Database(root / "project.db")
            vault = ProjectVault(project_id, root, database)
            self._vaults[project_id] = vault
            if not job_only:
                self._maintain(vault)
            return vault

    def _maintain(self, vault):
        if vault.project_id in self._maintained:
            return
        from novel_harness.services.chapter_summaries import write_ledger
        from novel_harness.services.search_index import SEARCH_INDEX_VERSION, rebuild_index

        vault.database.create_schema()
        with vault.database.session_scope() as session:
            version = session.scalar(
                text("SELECT version FROM search_index_state WHERE id=1")
            )
            if version != SEARCH_INDEX_VERSION:
                rebuild_index(session)
            write_ledger(session)
        self._maintained.add(vault.project_id)

    def create_project(self, payload: ProjectCreate) -> dict:
        project_id = new_id()
        return self._create_vault(payload, project_id, lambda _session, _root: None)

    def restore_project(self, project_id: str, archive_hash: str, populate) -> dict:
        """Publish an isolated, checked backup without replacing an existing project."""
        self._validate_id(project_id)
        projects_root = self.root / "projects"
        projects_root.mkdir(exist_ok=True)
        if (_path_is_reparse_point(projects_root) or projects_root.resolve() != projects_root
                or projects_root.parent != self.root):
            raise RuntimeError("UNSAFE_PROJECTS_ROOT")
        with _ProjectCreationLock(projects_root, project_id), self._lock:
            if self._registration_row(project_id) is not None:
                raise HTTPException(409, detail={"code": "BACKUP_PROJECT_EXISTS",
                                                "message": "同 ID 项目已存在，未覆盖任何内容。"})
            final_root = projects_root / project_id
            marker_name = ".restore-receipt.json"
            self._cleanup_stale_creating(projects_root, project_id)
            if final_root.exists() or _path_is_reparse_point(final_root):
                # A crash after atomic rename but before registry commit is recoverable
                # only with the exact same validated archive, never by overwriting.
                if (_path_is_reparse_point(final_root) or final_root.resolve() != final_root
                        or not (final_root / marker_name).is_file()
                        or _path_is_reparse_point(final_root / marker_name)):
                    raise HTTPException(409, detail={"code": "BACKUP_PROJECT_EXISTS"})
                receipt = json.loads((final_root / marker_name).read_text(encoding="utf-8"))
                if receipt.get("archive_hash") != archive_hash or receipt.get("id") != project_id:
                    raise HTTPException(409, detail={"code": "BACKUP_PROJECT_EXISTS"})
                result, database = self._validate_final_vault(
                    final_root, project_id, lambda _session, _root: None,
                )
                try:
                    self._register_or_reconcile(project_id, result["title"])
                finally:
                    database.dispose()
                return result
            token = uuid4().hex
            temporary = projects_root / f".creating-{project_id}-{token}"
            renamed = False
            try:
                temporary.mkdir()
                self._verify_creating_path(projects_root, temporary, project_id, token)
                result = populate(temporary)
                (temporary / marker_name).write_text(json.dumps({
                    "id": project_id, "archive_hash": archive_hash,
                }), encoding="utf-8")
                _fsync_directory_chain(temporary, projects_root)
                os.replace(temporary, final_root)
                renamed = True
                _fsync_directory(projects_root)
                self._register_or_reconcile(project_id, result["title"])
                return result
            except BaseException:
                if renamed:
                    if self._registration_row(project_id) is None:
                        self._remove_final_vault(projects_root, final_root, project_id)
                elif temporary.exists():
                    self._verify_creating_path(projects_root, temporary, project_id, token)
                    shutil.rmtree(temporary)
                raise

    def _create_vault(
        self,
        payload: ProjectCreate,
        project_id: str,
        initializer: Callable[[Session, Path], None],
        before_register: Callable[[], None] | None = None,
    ) -> dict:
        """Create and publish one Vault, or validate/recover its final directory.

        The registry row is the visibility boundary.  All database and source-file
        work happens below one verified ``.creating`` directory before an atomic
        rename publishes the final filesystem state.
        """
        self._validate_id(project_id)
        projects_path = self.root / "projects"
        projects_path.mkdir(parents=True, exist_ok=True)
        if _path_is_reparse_point(projects_path):
            raise RuntimeError("UNSAFE_PROJECTS_ROOT")
        projects_root = projects_path.resolve()
        if projects_root != projects_path or projects_root.parent != self.root:
            raise RuntimeError("UNSAFE_PROJECTS_ROOT")
        with _ProjectCreationLock(projects_root, project_id), self._lock:
            final_root = projects_root / project_id
            self._cleanup_stale_creating(projects_root, project_id)

            if final_root.exists():
                result, database = self._validate_final_vault(
                    final_root, project_id, initializer
                )
                if before_register is not None:
                    try:
                        before_register()
                    except BaseException:
                        database.dispose()
                        self._remove_final_vault(projects_root, final_root, project_id)
                        raise
                try:
                    self._register_or_reconcile(project_id, result["title"])
                except BaseException:
                    database.dispose()
                    raise
                prior = self._vaults.pop(project_id, None)
                if prior is not None:
                    prior.database.dispose()
                self._vaults[project_id] = ProjectVault(project_id, final_root, database)
                self._maintained.add(project_id)
                return result

            token = uuid4().hex
            temporary = projects_root / f".creating-{project_id}-{token}"
            database: Database | None = None
            renamed = False
            ready_for_registration = False
            try:
                temporary.mkdir(exist_ok=False)
                self._verify_creating_path(projects_root, temporary, project_id, token)
                for relative in ("assets", "rag/vectors", "backups"):
                    (temporary / relative).mkdir(parents=True, exist_ok=True)
                database = Database(temporary / "project.db")
                database.create_schema()
                with database.session_scope() as session:
                    project = Project(id=project_id, **payload.model_dump())
                    session.add(project)
                    session.flush()
                    initializer(session, temporary)
                    session.flush()
                    result = serialize(project)
                database.dispose()
                database = None
                _fsync_directory(temporary)
                os.replace(temporary, final_root)
                renamed = True
                _fsync_directory(projects_root)
                result, final_database = self._validate_final_vault(
                    final_root, project_id, initializer
                )
                if before_register is not None:
                    try:
                        before_register()
                    except BaseException:
                        final_database.dispose()
                        self._remove_final_vault(projects_root, final_root, project_id)
                        _fsync_directory(projects_root)
                        raise
                ready_for_registration = True
                try:
                    self._register_or_reconcile(project_id, result["title"])
                except BaseException:
                    final_database.dispose()
                    raise
                self._vaults[project_id] = ProjectVault(
                    project_id, final_root, final_database
                )
                self._maintained.add(project_id)
                return result
            except BaseException:
                if database is not None:
                    database.dispose()
                if renamed and not ready_for_registration and final_root.exists():
                    self._remove_final_vault(projects_root, final_root, project_id)
                    _fsync_directory(projects_root)
                if not renamed and temporary.exists():
                    self._verify_creating_path(
                        projects_root, temporary, project_id, token
                    )
                    shutil.rmtree(temporary)
                raise

    @staticmethod
    def _verify_creating_path(
        projects_root: Path, candidate: Path, project_id: str, token: str
    ) -> None:
        expected = f".creating-{project_id}-{token}"
        if (
            candidate.name != expected
            or candidate.parent != projects_root
            or _path_is_reparse_point(candidate)
            or candidate.resolve().parent != projects_root
        ):
            raise RuntimeError("UNSAFE_CREATING_VAULT")

    def _cleanup_stale_creating(self, projects_root: Path, project_id: str) -> None:
        pattern = re.compile(
            rf"^\.creating-{re.escape(project_id)}-(?P<token>[0-9a-f]{{32}})$"
        )
        for candidate in projects_root.iterdir():
            match = pattern.fullmatch(candidate.name)
            if (
                match is None
                or not candidate.is_dir()
                or _path_is_reparse_point(candidate)
            ):
                continue
            self._verify_creating_path(
                projects_root, candidate, project_id, match.group("token")
            )
            shutil.rmtree(candidate)

    @staticmethod
    def _remove_final_vault(
        projects_root: Path, final_root: Path, project_id: str
    ) -> None:
        if (
            final_root.name != project_id
            or final_root.parent != projects_root
            or not final_root.is_dir()
            or _path_is_reparse_point(final_root)
            or final_root.resolve() != final_root
        ):
            raise RuntimeError("INVALID_FINAL_VAULT")
        shutil.rmtree(final_root)

    @staticmethod
    def _validate_final_vault(
        final_root: Path,
        project_id: str,
        initializer: Callable[[Session, Path], None],
    ) -> tuple[dict, Database]:
        if (
            not final_root.is_dir()
            or _path_is_reparse_point(final_root)
            or final_root.resolve() != final_root
            or not (final_root / "project.db").is_file()
        ):
            raise RuntimeError("INVALID_FINAL_VAULT")
        database = Database(final_root / "project.db")
        try:
            if not database.check():
                raise RuntimeError("INVALID_FINAL_VAULT")
            with database.session_scope() as session:
                project = session.get(Project, project_id)
                if project is None:
                    raise RuntimeError("INVALID_FINAL_VAULT")
                initializer(session, final_root)
                session.flush()
                return serialize(project), database
        except BaseException:
            database.dispose()
            raise

    def list_records(self) -> list[dict]:
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT id,title,cover_path,last_opened_at,migration_status "
                    "FROM project_registry ORDER BY last_opened_at DESC,id"
                )
            ]

    def check(self) -> bool:
        """Probe the workspace registry without opening or mutating a project Vault."""
        with self.connect() as db:
            return db.execute("SELECT 1").fetchone()[0] == 1

    def touch(self, project_id: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE project_registry SET last_opened_at=strftime('%Y-%m-%dT%H:%M:%f',"
                "'now') WHERE id=?",
                (project_id,),
            )

    def dispose(self) -> None:
        for vault in self._vaults.values():
            vault.database.dispose()
