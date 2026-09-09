"""SQLite engine and session lifecycle."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from novel_harness.db.base import Base


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.engine = create_engine(
            f"sqlite:///{path.as_posix()}",
            connect_args={"check_same_thread": False},
        )
        self._sessions = sessionmaker(bind=self.engine, expire_on_commit=False, class_=Session)
        event.listen(self.engine, "connect", self._configure)

    @staticmethod
    def _configure(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")

    def create_schema(self) -> None:
        from novel_harness.db.dependency_schema import ensure_dependency_indexes
        from novel_harness.db.job_schema import finish_job_schema, prepare_job_schema
        from novel_harness.db.version_schema import ensure_version_page_index
        from novel_harness.services.search_index import create_index

        prepare_job_schema(self.engine, self.path)
        with self.engine.begin() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            Base.metadata.create_all(connection)
            for table in Base.metadata.sorted_tables:
                existing = {
                    row[1]
                    for row in connection.exec_driver_sql(f'PRAGMA table_info("{table.name}")')
                }
                for name, definition in {
                    "revision": "INTEGER NOT NULL DEFAULT 1",
                    "is_pinned": "BOOLEAN NOT NULL DEFAULT 0",
                    "deleted_at": "DATETIME",
                    "purge_after": "DATETIME",
                    "size_bytes": "INTEGER NOT NULL DEFAULT 0",
                    "content_revision": "INTEGER NOT NULL DEFAULT 1",
                    "promotion_fingerprint": "VARCHAR(64)",
                    "analysis_provider_identity": "JSON NOT NULL DEFAULT '{}'",
                    "analysis_identity": "VARCHAR(64)",
                }.items():
                    if name in table.c and name not in existing:
                        connection.exec_driver_sql(
                            f'ALTER TABLE "{table.name}" ADD COLUMN {name} {definition}'
                        )
            connection.exec_driver_sql(
                "UPDATE import_batches SET status=CASE status "
                "WHEN 'queued' THEN 'analyzing' WHEN 'completed' THEN 'analyzed' "
                "WHEN 'failed' THEN 'analysis_failed' ELSE status END"
            )
            connection.exec_driver_sql(
                "UPDATE import_analysis_units SET status=CASE status "
                "WHEN 'completed' THEN 'succeeded' WHEN 'paused' THEN 'queued' "
                "ELSE status END"
            )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_import_batches_project_status "
                "ON import_batches(project_id,status)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_import_analysis_units_status_created "
                "ON import_analysis_units(status,created_at,id)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_import_analysis_units_project_status_created "
                "ON import_analysis_units(project_id,status,created_at,id)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_import_analysis_units_batch_status_created "
                "ON import_analysis_units(import_batch_id,status,created_at,id)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_memory_candidates_import_analysis_identity "
                "ON memory_candidates(import_batch_id,kind,analysis_identity,id)"
            )
            create_index(connection)
            ensure_dependency_indexes(connection)
            ensure_version_page_index(connection)
            connection.execute(
                text("CREATE TABLE IF NOT EXISTS pending_projections (kind TEXT PRIMARY KEY)")
            )
            finish_job_schema(connection)

    @contextmanager
    def job_session_scope(self) -> Iterator[Session]:
        """Task transactions do not run unrelated filesystem maintenance."""
        session = self._sessions()
        session.info["vault_root"] = self.path.parent
        session.info["index_ready"] = True
        try:
            yield session
            session.commit()
            if session.info.get("ledger_queued"):
                from novel_harness.services.projections import drain_ledger

                drain_ledger(self)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @contextmanager
    def session_scope(self) -> Iterator[Session]:
        session = self._sessions()
        session.info["vault_root"] = self.path.parent
        session.info["index_ready"] = True
        try:
            yield session
            session.commit()
            if session.info.get("ledger_queued"):
                from novel_harness.services.projections import drain_ledger

                drain_ledger(self)
            self.drain_file_deletions()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def check(self) -> bool:
        with self.engine.connect() as connection:
            return connection.execute(text("SELECT 1")).scalar_one() == 1

    def drain_file_deletions(self):
        """Committed outbox; filesystem failure keeps the marker for next use."""
        try:
            with self.engine.begin() as connection:
                rows = list(connection.execute(text("SELECT relative_path FROM file_delete_queue")))
                if not rows:
                    return
                # Unlink is a write too: serialize it with export's input capture.
                # Re-read after locking, since another drainer may have finished.
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                rows = list(connection.execute(text("SELECT relative_path FROM file_delete_queue")))
                for (relative,) in rows:
                    target = (self.path.parent / relative).resolve()
                    if not target.is_relative_to((self.path.parent / "assets").resolve()):
                        continue
                    target.unlink(missing_ok=True)
                    connection.execute(
                        text("DELETE FROM file_delete_queue WHERE relative_path=:path"),
                        {"path": relative},
                    )
        except (OSError, SQLAlchemyError):
            logging.getLogger(__name__).warning(
                "Asset cleanup pending; retrying on next project use."
            )

    def dispose(self) -> None:
        self.engine.dispose()
