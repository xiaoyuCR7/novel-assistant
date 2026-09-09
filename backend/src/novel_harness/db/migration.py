"""Copy legacy content into validated Vaults; never remove the source database."""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from contextlib import closing
from datetime import UTC, datetime

from novel_harness.db.base import Base
from novel_harness.db.session import Database
from novel_harness.db.vault import ProjectVaultRegistry


def migrate_legacy_database(registry: ProjectVaultRegistry) -> None:
    legacy = registry.root / "novel-harness.db"
    if not legacy.is_file():
        return
    with closing(sqlite3.connect(legacy)) as source:
        source.row_factory = sqlite3.Row
        tables = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "projects" not in tables:
            return
        projects = source.execute("SELECT id,title FROM projects").fetchall()
        registered = {row["id"] for row in registry.list_records()}
        pending = [p for p in projects if p["id"] not in registered]
        if not pending:
            return
        backups = registry.root / "backups"
        backups.mkdir(exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
        with closing(sqlite3.connect(backups / f"legacy-{stamp}.db")) as backup:
            source.backup(backup)
        for project in pending:
            pid = project["id"]
            registry._validate_id(pid)
            final = registry.root / "projects" / pid
            if (final / "project.db").is_file():
                with closing(sqlite3.connect(final / "project.db")) as existing:
                    ids = existing.execute("SELECT id FROM projects").fetchall()
                    if ids != [(pid,)] or existing.execute("PRAGMA foreign_key_check").fetchall():
                        raise RuntimeError("Existing unregistered Vault failed validation")
                registry.register(pid, project["title"])
                continue
            temporary = registry.root / "projects" / f".migrating-{pid}-{stamp}"
            for folder in ("assets", "rag/vectors", "backups"):
                (temporary / folder).mkdir(parents=True, exist_ok=True)
            database = Database(temporary / "project.db")
            database.create_schema()
            database.dispose()
            with closing(sqlite3.connect(temporary / "project.db")) as target, target:
                target.execute("PRAGMA foreign_keys=ON")
                target.execute("BEGIN")
                target.execute("PRAGMA defer_foreign_keys=ON")
                for table in Base.metadata.sorted_tables:
                    name = table.name
                    if name == "job_schema_versions":
                        continue  # Target schema metadata is local, not narrative content.
                    if name not in tables:
                        continue
                    columns = {r[1] for r in source.execute(f'PRAGMA table_info("{name}")')}
                    if name == "projects":
                        predicate = "id=?"
                    # Import persistence tables use this same project_id ownership rule.
                    elif "project_id" in columns:
                        predicate = "project_id=?"
                    elif name in {
                        "generation_artifacts", "ai_job_controls", "ai_job_stage_attempts",
                        "ai_job_action_receipts",
                    }:
                        predicate = "job_id IN (SELECT id FROM ai_jobs WHERE project_id=?)"
                    elif name == "conflict_options":
                        predicate = "conflict_id IN (SELECT id FROM conflicts WHERE project_id=?)"
                    else:
                        raise RuntimeError(f"No migration ownership rule for {name}")
                    rows = source.execute(
                        f'SELECT * FROM "{name}" WHERE {predicate}', (pid,)
                    ).fetchall()
                    for row in rows:
                        values = dict(row)
                        if name == "assets" and values["relative_path"]:
                            old = (registry.root / values["relative_path"]).resolve()
                            asset_root = (registry.root / "projects" / pid / "assets").resolve()
                            if not old.is_relative_to(asset_root) or not old.is_file():
                                raise RuntimeError("Legacy asset is missing or outside its project")
                            relative = f"assets/{old.name}"
                            new = temporary / relative
                            shutil.copy2(old, new)
                            if (
                                hashlib.sha256(old.read_bytes()).digest()
                                != hashlib.sha256(new.read_bytes()).digest()
                            ):
                                raise RuntimeError("Asset hash mismatch")
                            values["relative_path"] = relative
                        names = ",".join(f'"{key}"' for key in values)
                        marks = ",".join("?" for _ in values)
                        target.execute(
                            f'INSERT INTO "{name}" ({names}) VALUES ({marks})',
                            tuple(values.values()),
                        )
                    count = target.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
                    if count != len(rows):
                        raise RuntimeError(f"Migration row count mismatch: {name}")
                if target.execute("PRAGMA foreign_key_check").fetchall():
                    raise RuntimeError("Migration foreign key validation failed")
            # Existing legacy assets may already occupy the project directory.
            final.mkdir(parents=True, exist_ok=True)
            for folder in ("assets", "rag", "backups"):
                shutil.copytree(temporary / folder, final / folder, dirs_exist_ok=True)
            # The database is the switch marker: assets must already be in place.
            (temporary / "project.db").replace(final / "project.db")
            registry.register(pid, project["title"])
