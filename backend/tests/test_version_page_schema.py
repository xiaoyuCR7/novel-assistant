import sqlite3
from contextlib import closing

from novel_harness.db.session import Database

VERSION_PAGE_INDEX = "ix_chapter_versions_chapter_created_id_desc"


def _create_legacy_version_table(database):
    with database.engine.begin() as connection:
        connection.exec_driver_sql(
            """CREATE TABLE chapter_versions (
                id VARCHAR(36) PRIMARY KEY,
                chapter_id VARCHAR(36) NOT NULL,
                project_id VARCHAR(36) NOT NULL,
                parent_version_id VARCHAR(36),
                restored_from_version_id VARCHAR(36),
                generation_job_id VARCHAR(36),
                content TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                word_count INTEGER NOT NULL DEFAULT 0,
                source VARCHAR(32) NOT NULL DEFAULT 'manual',
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )"""
        )


def test_create_schema_adds_version_page_index_to_legacy_database(tmp_path):
    database = Database(tmp_path / "legacy.db")
    _create_legacy_version_table(database)

    database.create_schema()

    with closing(sqlite3.connect(database.path)) as connection, connection:
        columns = connection.execute(f'PRAGMA index_xinfo("{VERSION_PAGE_INDEX}")').fetchall()
    assert [(row[2], row[3]) for row in columns if row[5]] == [
        ("chapter_id", 0),
        ("created_at", 1),
        ("id", 1),
    ]
    database.dispose()


def test_version_page_order_uses_composite_index_without_temp_sort(tmp_path):
    database = Database(tmp_path / "page-plan.db")
    _create_legacy_version_table(database)
    database.create_schema()

    with closing(sqlite3.connect(database.path)) as connection, connection:
        plan = connection.execute(
            """EXPLAIN QUERY PLAN
            SELECT id, chapter_id, created_at, source, summary, word_count,
                   parent_version_id, restored_from_version_id, generation_job_id
            FROM chapter_versions
            WHERE chapter_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?""",
            ("chapter", 51),
        ).fetchall()
    detail = " ".join(row[3] for row in plan)
    assert VERSION_PAGE_INDEX in detail
    assert "USE TEMP B-TREE FOR ORDER BY" not in detail
    database.dispose()
