import sqlite3
from contextlib import closing

import pytest
from sqlalchemy import event, inspect

from novel_harness.db.base import Base
from novel_harness.db.models import AIJob, ChapterDocument, Project, StoryNode
from novel_harness.db.session import Database

JOB_TABLES = {
    "ai_job_controls",
    "ai_job_stage_attempts",
    "ai_job_action_receipts",
    "job_schema_versions",
}


@pytest.fixture
def old_database(tmp_path):
    database = Database(tmp_path / "project.db")
    tables = [table for table in Base.metadata.sorted_tables if table.name not in JOB_TABLES]
    with database.engine.begin() as connection:
        Base.metadata.create_all(connection, tables=tables)
        connection.execute(Project.__table__.insert().values(id="novel-a", title="原稿"))
        connection.execute(
            StoryNode.__table__.insert().values(
                id="chapter-a",
                project_id="novel-a",
                kind="chapter",
                title="原章节",
            )
        )
        connection.execute(
            ChapterDocument.__table__.insert().values(
                chapter_id="chapter-a",
                project_id="novel-a",
                content="不能丢失的原稿",
                revision=7,
            )
        )
        connection.execute(
            AIJob.__table__.insert().values(
                id="old-job",
                project_id="novel-a",
                chapter_id="chapter-a",
                task_type="chat",
                prompt_version="legacy",
                status="succeeded",
                result={"reply": "历史回复"},
            )
        )
    yield database
    database.dispose()


def test_task_tables_exist_only_in_the_project_vault(client, project):
    vault = client.app.state.vault_registry.require(project["id"])
    assert JOB_TABLES <= set(inspect(vault.database.engine).get_table_names())
    with closing(sqlite3.connect(client.app.state.vault_registry.path)) as registry:
        names = {row[0] for row in registry.execute("SELECT name FROM sqlite_master")}
    assert not JOB_TABLES.intersection(names)


def test_old_vault_is_backed_up_before_upgrade_and_upgrade_is_repeatable(old_database):
    old_database.create_schema()
    backups = list((old_database.path.parent / "backups").glob("pre-durable-jobs-*.sqlite3"))
    assert len(backups) == 1
    with closing(sqlite3.connect(backups[0])) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute("SELECT id,title FROM projects").fetchall() == [("novel-a", "原稿")]
        names = {r[0] for r in backup.execute("SELECT name FROM sqlite_master")}
        assert "ai_job_controls" not in names
        assert backup.execute("SELECT content,revision FROM chapter_documents").fetchall() == [
            ("不能丢失的原稿", 7),
        ]
        assert backup.execute("SELECT id,status FROM ai_jobs").fetchall() == [
            ("old-job", "succeeded")
        ]
    with old_database.job_session_scope() as session:
        assert session.get(ChapterDocument, "chapter-a").content == "不能丢失的原稿"
        assert session.get(AIJob, "old-job").result == {"reply": "历史回复"}
    old_database.create_schema()
    remaining = list((old_database.path.parent / "backups").glob("pre-durable-jobs-*.sqlite3"))
    assert remaining == backups


def test_future_job_schema_is_rejected_before_any_schema_writes(old_database):
    with old_database.engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE job_schema_versions (feature TEXT PRIMARY KEY, version INTEGER)"
        )
        connection.exec_driver_sql("INSERT INTO job_schema_versions VALUES ('durable_jobs', 999)")
    with pytest.raises(RuntimeError, match="JOB_SCHEMA_UNSUPPORTED"):
        old_database.create_schema()
    assert "ai_job_controls" not in inspect(old_database.engine).get_table_names()


def test_failed_backup_does_not_start_ddl(old_database, monkeypatch):
    from novel_harness.db import job_schema

    def fail(_path):
        raise OSError("backup disk unavailable")

    monkeypatch.setattr(job_schema, "backup_before_jobs_upgrade", fail)
    with pytest.raises(OSError):
        old_database.create_schema()
    assert not JOB_TABLES.intersection(inspect(old_database.engine).get_table_names())


def test_interrupted_ddl_rolls_back_and_can_be_retried(old_database):
    from novel_harness.db.job_models import AIJobControl

    def fail(*args, **kwargs):
        raise RuntimeError("injected DDL failure")

    event.listen(AIJobControl.__table__, "after_create", fail)
    try:
        with pytest.raises(RuntimeError, match="injected DDL failure"):
            old_database.create_schema()
    finally:
        event.remove(AIJobControl.__table__, "after_create", fail)
    assert not JOB_TABLES.intersection(inspect(old_database.engine).get_table_names())
    old_database.create_schema()
    assert JOB_TABLES <= set(inspect(old_database.engine).get_table_names())
