from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def test_initial_migration_builds_complete_schema(tmp_path):
    import logging

    diagnostic_logger = logging.getLogger('novel_harness.stages')
    diagnostic_logger.disabled = False
    database_path = tmp_path / "migration.db"
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")

    command.upgrade(config, "head")
    assert not diagnostic_logger.disabled

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert {
        "projects",
        "ideas",
        "story_nodes",
        "entities",
        "canon_facts",
        "chapter_documents",
        "chapter_versions",
        "ai_jobs",
        "feedback",
        "preference_candidates",
        "conflicts",
        "assets",
        "alembic_version",
    } <= tables
