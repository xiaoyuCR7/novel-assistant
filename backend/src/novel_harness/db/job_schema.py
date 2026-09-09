"""Additive task schema upgrade, with a recoverable pre-upgrade SQLite backup."""

import sqlite3
from contextlib import closing
from uuid import uuid4

from sqlalchemy import inspect, select

from novel_harness.db.job_models import (
    AIJobActionReceipt,
    AIJobControl,
    AIJobStageAttempt,
    JobSchemaVersion,
)

JOB_TABLES = tuple(
    model.__table__
    for model in (AIJobControl, AIJobStageAttempt, AIJobActionReceipt, JobSchemaVersion)
)
JOB_SCHEMA_VERSION = 1


def backup_before_jobs_upgrade(path):
    folder = path.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"pre-durable-jobs-{uuid4().hex}.sqlite3"
    with closing(sqlite3.connect(path)) as source:
        with closing(sqlite3.connect(target)) as destination:
            source.backup(destination)
            if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("JOB_BACKUP_INVALID")
    return target


def _version(connection):
    if not inspect(connection).has_table("job_schema_versions"):
        return None
    version = connection.execute(
        select(JobSchemaVersion.version).where(JobSchemaVersion.feature == "durable_jobs")
    ).scalar_one_or_none()
    if version is not None and version != JOB_SCHEMA_VERSION:
        raise RuntimeError("JOB_SCHEMA_UNSUPPORTED")
    return version


def _validate_columns(connection):
    inspector = inspect(connection)
    for table in JOB_TABLES:
        if not inspector.has_table(table.name):
            raise RuntimeError("JOB_SCHEMA_INCOMPLETE")
        columns = {column["name"] for column in inspector.get_columns(table.name)}
        if not set(table.c.keys()).issubset(columns):
            raise RuntimeError("JOB_SCHEMA_INCOMPLETE")


def prepare_job_schema(engine, path):
    with engine.connect() as connection:
        if _version(connection) is not None:
            _validate_columns(connection)
            return
        existing = inspect(connection).has_table("projects")
    if existing:
        backup_before_jobs_upgrade(path)


def finish_job_schema(connection):
    _validate_columns(connection)
    if _version(connection) is None:
        connection.execute(
            JobSchemaVersion.__table__.insert().values(
                feature="durable_jobs", version=JOB_SCHEMA_VERSION
            )
        )
