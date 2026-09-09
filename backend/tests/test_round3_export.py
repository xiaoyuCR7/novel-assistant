"""Export freezes inputs, not author writes during compression."""

import io
import json
import sqlite3
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from threading import Event

import pytest
from job_helpers import complete_summary, save_chapter
from sqlalchemy import text

from novel_harness.db.base import utc_now
from novel_harness.db.models import Asset


def pause_compression(monkeypatch):
    entered, release = Event(), Event()
    original = zipfile.ZipFile.writestr

    def paused(archive, name, *args, **kwargs):
        if name == "project.db":
            entered.set()
            assert release.wait(20), "Test did not release export compression"
        return original(archive, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "writestr", paused)
    return entered, release


def test_save_during_compression_succeeds_and_export_stays_consistent(
    client, project, seeded_chapter, monkeypatch, tmp_path
):
    base = f"/api/v1/projects/{project['id']}"
    url = base + f"/chapters/{seeded_chapter}"
    save_chapter(client, url, json={"content": "snapshot-before"})
    complete_summary(client, url)
    document = client.get(url).json()
    vault = client.app.state.vault_registry.require(project["id"])
    from novel_harness.services.chapter_summaries import ledger_text

    with vault.database.session_scope() as session:
        before_ledger = ledger_text(session)
    entered, release = pause_compression(monkeypatch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        exported = pool.submit(client.get, base + "/export")
        try:
            assert entered.wait(10)
            saved = client.put(url, json={
                "content": "saved-while-compressing", "contract": {},
                "revision": document["revision"],
            })
            assert saved.status_code == 200
        finally:
            release.set()
        result = exported.result(timeout=10)
    assert result.status_code == 200
    assert client.get(url).json()["content"] == "saved-while-compressing"
    with zipfile.ZipFile(io.BytesIO(result.content)) as archive:
        manuscript = next(name for name in archive.namelist() if name.startswith("manuscript/"))
        assert "snapshot-before" in archive.read(manuscript).decode()
        assert "saved-while-compressing" not in archive.read(manuscript).decode()
        assert archive.read("rag/chapter-continuity-ledger.md").decode() == before_ledger
        snapshot_path = tmp_path / "exported.db"
        snapshot_path.write_bytes(archive.read("project.db"))
        with closing(sqlite3.connect(snapshot_path)) as snapshot, snapshot:
            assert snapshot.execute("SELECT content FROM chapter_documents").fetchone() == (
                "snapshot-before",
            )


def test_asset_purge_during_compression_does_not_remove_snapshot_bytes(
    client, project, monkeypatch
):
    base = f"/api/v1/projects/{project['id']}"
    asset = client.post(base + "/assets/generate", json={
        "project_id": project["id"], "kind": "scene", "prompt": "demo scene",
    }).json()
    vault = client.app.state.vault_registry.require(project["id"])
    source = vault.root / asset["relative_path"]
    before = source.read_bytes()
    entered, release = pause_compression(monkeypatch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        exported = pool.submit(client.get, base + "/export")
        try:
            assert entered.wait(10)
            deleted = client.delete(base + f"/library/asset/{asset['id']}")
            assert deleted.status_code == 200
            with vault.database.job_session_scope() as session:
                item = session.get(Asset, asset["id"], execution_options={"include_deleted": True})
                item.purge_after = utc_now() - timedelta(days=1)
            purged = client.delete(base + f"/trash/asset/{asset['id']}/purge")
            assert purged.status_code == 200
            assert not source.exists()
        finally:
            release.set()
        result = exported.result(timeout=10)
    assert result.status_code == 200
    with zipfile.ZipFile(io.BytesIO(result.content)) as archive:
        assert archive.read("assets/" + source.name) == before
        assert json.loads(archive.read("assets-manifest.json"))[0]["id"] == asset["id"]


def test_export_uses_global_hierarchical_sequence(client, project):
    base = f"/api/v1/projects/{project['id']}"
    expected = []
    for volume_order in (1, 2):
        volume = client.post(base + "/nodes", json={
            "kind": "volume", "title": f"V{volume_order}", "order_index": volume_order,
        }).json()
        for chapter_order in (1, 2):
            title = f"V{volume_order}-C{chapter_order}"
            expected.append(title)
            client.post(base + "/nodes", json={
                "kind": "chapter", "title": title, "parent_id": volume["id"],
                "order_index": chapter_order,
            })
    result = client.get(base + "/export")
    assert result.status_code == 200
    with zipfile.ZipFile(io.BytesIO(result.content)) as archive:
        names = [name for name in archive.namelist() if name.startswith("manuscript/")]
        titles = [archive.read(name).decode().splitlines()[0][2:] for name in names]
        assert titles == expected
        assert names == sorted(names)
        assert [Path(name).name[:4] for name in names] == ["0001", "0002", "0003", "0004"]


def test_file_deletion_holds_writer_lock_before_unlink(client, project, monkeypatch):
    vault = client.app.state.vault_registry.require(project["id"])
    target = vault.root / "assets" / "queued-test.png"
    target.write_bytes(b"queued test")
    with vault.database.job_session_scope() as session:
        session.execute(text("INSERT INTO file_delete_queue VALUES ('assets/queued-test.png')"))
    original = Path.unlink
    observed = []

    def checked(path, *args, **kwargs):
        if path == target:
            with closing(sqlite3.connect(vault.database.path, timeout=0)) as rival, rival:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    rival.execute("BEGIN IMMEDIATE")
            observed.append(True)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", checked)
    vault.database.drain_file_deletions()
    assert observed == [True]
    assert not target.exists()


def test_busy_save_is_explicitly_retryable(client, project, seeded_chapter):
    base = f"/api/v1/projects/{project['id']}"
    url = base + f"/chapters/{seeded_chapter}"
    document = client.get(url).json()
    vault = client.app.state.vault_registry.require(project["id"])
    with closing(sqlite3.connect(vault.database.path)) as rival, rival:
        rival.execute("BEGIN IMMEDIATE")
        response = client.put(url, json={
            "content": "must not be lost", "revision": document["revision"], "contract": {},
        })
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "DATABASE_BUSY"
    assert response.json()["detail"]["retryable"] is True
    assert "SQL" not in response.text
    assert client.get(url).json()["content"] == document["content"]


@pytest.mark.parametrize("failure", ["copy", "compression"])
def test_failed_export_cleans_staging_and_releases_lock(client, project, monkeypatch, failure):
    from novel_harness.services import projects

    base = f"/api/v1/projects/{project['id']}"
    asset = client.post(base + "/assets/generate", json={
        "project_id": project["id"], "kind": "scene", "prompt": "test input",
    }).json()
    vault = client.app.state.vault_registry.require(project["id"])
    source = vault.root / asset["relative_path"]
    original_bytes = source.read_bytes()
    staged_paths = []
    original_copy = projects.shutil.copyfile

    def copy(src, dst):
        staged_paths.append(Path(dst))
        result = original_copy(src, dst)
        if failure == "copy":
            raise OSError("simulated staging disk failure")
        return result

    def bad_zip(*_args, **_kwargs):
        raise OSError("simulated compression failure")

    monkeypatch.setattr(projects.shutil, "copyfile", copy)
    if failure == "compression":
        monkeypatch.setattr(zipfile.ZipFile, "writestr", bad_zip)
    with vault.database.session_scope() as session:
        with pytest.raises(OSError, match="simulated"):
            projects.export_project(session, project["id"], vault.root)
        with closing(sqlite3.connect(vault.database.path, timeout=0)) as rival, rival:
            rival.execute("BEGIN IMMEDIATE")
            rival.rollback()
    assert staged_paths and all(not path.parent.exists() for path in staged_paths)
    assert source.read_bytes() == original_bytes
