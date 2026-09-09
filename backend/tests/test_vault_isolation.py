import sqlite3
from contextlib import closing

from job_helpers import run_job


def test_projects_have_distinct_physical_databases(client, tmp_path):
    a = client.post("/api/v1/projects", json={"title": "甲书"}).json()
    b = client.post("/api/v1/projects", json={"title": "乙书"}).json()
    for project in (a, b):
        path = tmp_path / "projects" / project["id"] / "project.db"
        assert path.is_file()
        with closing(sqlite3.connect(path)) as db, db:
            assert db.execute("SELECT id FROM projects").fetchall() == [(project["id"],)]
    with closing(sqlite3.connect(tmp_path / "workspace.db")) as db, db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert tables == {"project_registry"}


def test_project_scoped_chapter_rejects_other_vault(client, project, seeded_chapter):
    other = client.post("/api/v1/projects", json={"title": "乙书"}).json()
    own = f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}"
    foreign = f"/api/v1/projects/{other['id']}/chapters/{seeded_chapter}"
    assert client.get(own).status_code == 200
    assert client.get(foreign).status_code == 404
    assert client.get(f"/api/v1/chapters/{seeded_chapter}").status_code == 404


def test_payload_project_cannot_override_route_project(client, project, seeded_chapter):
    other = client.post("/api/v1/projects", json={"title": "乙书"}).json()
    response = run_job(client,
        f"/api/v1/projects/{project['id']}/ai/jobs",
        json={
            "project_id": other["id"],
            "chapter_id": seeded_chapter,
            "task_type": "draft",
        },
    )
    assert response.status_code == 409
