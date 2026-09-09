import sqlite3
from contextlib import closing

from fastapi.testclient import TestClient

from novel_harness.db.models import Entity, Project
from novel_harness.db.session import Database
from novel_harness.main import create_app


def test_legacy_migration_is_physical_and_idempotent(tmp_path, monkeypatch):
    legacy = Database(tmp_path / "novel-harness.db")
    legacy.create_schema()
    from novel_harness.db.job_models import AIJobControl
    from novel_harness.db.models import AIJob
    with legacy.session_scope() as session:
        session.add_all([Project(id="alpha", title="甲"), Project(id="beta", title="乙")])
        session.flush()
        session.add_all(
            [
                Entity(
                    id="one", project_id="alpha", name="林渡", kind="character", summary="甲的秘密"
                ),
                Entity(
                    id="two", project_id="beta", name="林渡", kind="character", summary="乙的秘密"
                ),
            ]
        )
        for project_id in ("alpha", "beta"):
            job = AIJob(
                id=f"job-{project_id}", project_id=project_id, task_type="chat",
                status="queued", prompt_version="test", instructions=project_id,
            )
            session.add(job)
            session.flush()
            session.add(AIJobControl(
                job_id=job.id, operation="writing", idempotency_key=project_id,
                request_hash="digest", command={"project_id": project_id},
            ))
    legacy.dispose()
    monkeypatch.setenv("NOVEL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NOVEL_AI_PROVIDER", "demo")
    for _ in range(2):
        with TestClient(create_app()) as client:
            assert len(client.get("/api/v1/projects").json()) == 2
            a = client.get("/api/v1/projects/alpha/workspace").json()
            assert [e["summary"] for e in a["entities"]] == ["甲的秘密"]
        with closing(sqlite3.connect(tmp_path / "projects/alpha/project.db")) as db, db:
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []
            assert db.execute("SELECT id FROM entities").fetchall() == [("one",)]
            assert db.execute("SELECT job_id FROM ai_job_controls").fetchall() == [("job-alpha",)]
    assert list((tmp_path / "backups").glob("legacy-*.db"))
    assert (tmp_path / "novel-harness.db").is_file()
