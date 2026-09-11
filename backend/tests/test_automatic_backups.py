import io
import zipfile
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException


def service(client):
    from novel_harness.services.automatic_backups import AutomaticBackups

    return AutomaticBackups(client.app.state.vault_registry)


def test_backup_roundtrip_skips_unchanged_and_survives_restart(client, project, seeded_chapter):
    manager = service(client)
    pid = project["id"]
    assert manager.report(pid)["settings"]["enabled"] is False
    first = manager.run(pid)
    assert first["result"] == "created"
    record = first["backups"][0]
    data = manager.read(pid, record["id"])
    assert zipfile.is_zipfile(io.BytesIO(data))
    preview = client.post("/api/v1/projects/backup/preview", files={"file": ("backup.zip", data)})
    assert preview.status_code == 200 and preview.json()["verified"]
    assert service(client).report(pid)["backups"][0]["id"] == record["id"]
    assert manager.run(pid)["result"] == "unchanged"
    assert len(manager.report(pid)["backups"]) == 1


def test_schedule_is_opt_in_and_persistent_with_revision_guard(client, project):
    from fastapi import HTTPException

    manager = service(client)
    pid = project["id"]
    now = datetime(2026, 9, 11, tzinfo=UTC)
    manager.tick(now)
    assert not manager.report(pid)["backups"]
    config = dict(enabled=True, interval_hours=24, retention_count=2, revision=0)
    assert manager.configure(pid, config)["settings"]["revision"] == 1
    with pytest.raises(HTTPException) as error:
        manager.configure(pid, config)
    assert error.value.status_code == 409
    manager.tick(now)
    assert len(manager.report(pid)["backups"]) == 1
    restarted = service(client)
    restarted.tick(now + timedelta(hours=1))
    assert restarted.report(pid)["last_checked_at"] == now.isoformat()
    restarted.tick(now + timedelta(hours=24))
    assert restarted.report(pid)["last_checked_at"] == (now + timedelta(hours=24)).isoformat()
    assert len(restarted.report(pid)["backups"]) == 1


def test_retention_only_removes_managed_files_after_valid_new_backup(client, project, monkeypatch):
    from novel_harness.services import automatic_backups

    manager = service(client)
    pid = project["id"]
    manager.configure(pid, dict(enabled=False, interval_hours=24, retention_count=1, revision=0))
    first = manager.run(pid)["backups"][0]
    root = client.app.state.vault_registry.require(pid).root / "backups" / "automatic"
    (root / "author-copy.zip").write_bytes(b"keep")
    node = client.post(
        f"/api/v1/projects/{pid}/nodes", json={"kind": "chapter", "title": "新的章节"}
    )
    assert node.status_code == 201
    original = automatic_backups.export_project
    monkeypatch.setattr(automatic_backups, "export_project", lambda *args: b"broken")
    with pytest.raises(HTTPException):
        manager.run(pid)
    assert manager.read(pid, first["id"])
    assert manager.report(pid)["last_error"]
    monkeypatch.setattr(automatic_backups, "export_project", original)
    second = manager.run(pid)["backups"]
    assert len(second) == 1 and second[0]["id"] != first["id"]
    assert (root / "author-copy.zip").read_bytes() == b"keep"
    assert not (root / f"{first['id']}.zip").exists()


def test_backup_api_rejects_cross_project_and_invalid_settings(client, project):
    pid = project["id"]
    base = f"/api/v1/projects/{pid}/automatic-backups"
    assert client.get(base).status_code == 200
    assert (
        client.put(
            base, json={"enabled": True, "interval_hours": 0, "retention_count": 0, "revision": 0}
        ).status_code
        == 422
    )
    created = client.post(base + "/run")
    assert created.status_code == 200
    record = created.json()["backups"][0]
    assert client.get(base + "/" + record["id"]).headers["content-type"] == "application/zip"
    other = client.post("/api/v1/projects", json={"title": "另一本"}).json()["id"]
    assert (
        client.get(f"/api/v1/projects/{other}/automatic-backups/{record['id']}").status_code == 404
    )


def test_backup_busy_does_not_start_a_second_export(client, project):
    from fastapi import HTTPException

    manager = service(client)
    with manager.run_lock:
        with pytest.raises(HTTPException) as error:
            manager.run(project["id"])
    assert error.value.status_code == 409


def test_missing_or_damaged_backup_is_replaced_without_source_changes(client, project):
    manager = service(client)
    pid = project["id"]
    first = manager.run(pid)["backups"][0]
    manager._file(pid, first["id"]).write_bytes(b"damaged")
    with pytest.raises(HTTPException):
        manager.read(pid, first["id"])
    assert manager.run(pid)["result"] == "created"
    latest = manager.report(pid)["backups"][0]
    manager._file(pid, latest["id"]).unlink()
    assert manager.run(pid)["result"] == "created"


def test_unsafe_backup_path_blocks_deletion_and_download(client, project, monkeypatch):
    from novel_harness.services import automatic_backups

    manager = service(client)
    pid = project["id"]
    first = manager.run(pid)["backups"][0]
    path = manager._file(pid, first["id"])
    check = automatic_backups._path_is_reparse_point
    monkeypatch.setattr(
        automatic_backups,
        "_path_is_reparse_point",
        lambda candidate: candidate == path or check(candidate),
    )
    with pytest.raises(HTTPException):
        manager.read(pid, first["id"])
    assert path.exists()


def test_temp_cleanup_failure_releases_worker_lock(client, project, monkeypatch):
    from pathlib import Path

    from novel_harness.services import automatic_backups

    manager = service(client)
    original_replace = automatic_backups.os.replace
    original_unlink = Path.unlink

    def fail_publish(source, target):
        if str(target).endswith(".zip"):
            raise PermissionError("locked")
        return original_replace(source, target)

    def fail_cleanup(path, *args, **kwargs):
        if path.suffix == ".tmp":
            raise PermissionError("locked temporary file")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(automatic_backups.os, "replace", fail_publish)
    monkeypatch.setattr(Path, "unlink", fail_cleanup)
    with pytest.raises(PermissionError):
        manager.run(project["id"])
    assert manager.run_lock.acquire(blocking=False)
    manager.run_lock.release()


def test_retention_keeps_new_snapshot_when_system_clock_moves_backwards(client, project):
    manager, pid = service(client), project["id"]
    manager.configure(pid, dict(enabled=False, interval_hours=24, retention_count=1, revision=0))
    first = manager.run(pid, datetime(2026, 9, 11, tzinfo=UTC))["backups"][0]
    client.post(f"/api/v1/projects/{pid}/nodes", json={"kind": "chapter", "title": "回拨后新正文"})
    second = manager.run(pid, datetime(2026, 9, 10, tzinfo=UTC))
    assert second["result"] == "created"
    assert len(second["backups"]) == 1 and second["backups"][0]["id"] != first["id"]
    preview = client.post(
        "/api/v1/projects/backup/preview",
        files={
            "file": ("backup.zip", manager.read(pid, second["backups"][0]["id"])),
        },
    ).json()
    assert preview["counts"]["chapters"] == 1
    assert service(client).run(pid)["result"] == "unchanged"


def test_published_backup_recovers_after_catalog_insert_failure(client, project):
    import sqlite3

    manager, pid = service(client), project["id"]
    with manager.connect() as db:
        db.execute("""CREATE TRIGGER fail_catalog BEFORE INSERT ON automatic_backup
            BEGIN SELECT RAISE(ABORT, 'injected catalog failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        manager.run(pid)
    folder = manager._folder(pid)
    files = list(folder.glob("*.zip"))
    assert len(files) == 1
    with manager.connect() as db:
        assert db.execute("SELECT count(*) FROM automatic_backup").fetchone()[0] == 0
        db.execute("DROP TRIGGER fail_catalog")
    restarted = service(client)
    records = restarted.report(pid)["backups"]
    assert len(records) == 1 and records[0]["id"] == files[0].stem
    assert restarted.read(pid, records[0]["id"])
    assert restarted.run(pid)["result"] == "unchanged"
    assert len(list(folder.glob("*.zip"))) == 1


def test_unchanged_compares_latest_real_archive_not_policy_fingerprint(client, project):
    manager, pid = service(client), project["id"]
    first = manager.run(pid)["backups"][0]
    client.post(f"/api/v1/projects/{pid}/nodes", json={"kind": "chapter", "title": "新章节"})
    latest = manager.run(pid)["backups"][0]
    assert latest["id"] != first["id"]
    manager._file(pid, latest["id"]).unlink()
    with manager.connect() as db:
        db.execute("DELETE FROM automatic_backup WHERE id=?", (latest["id"],))
    assert manager.run(pid)["result"] == "created"


def test_recovery_cleans_only_temporary_file_owned_by_durable_intent(client, project):
    manager, pid = service(client), project["id"]
    folder = manager._folder(pid)
    identifier = "a" * 32
    temporary = folder / f"{identifier}.tmp"
    temporary.write_bytes(b"interrupted partial write")
    unknown = folder / ("b" * 32 + ".tmp")
    unknown.write_bytes(b"author-owned unknown file")
    with manager.connect() as db:
        db.execute(
            "INSERT INTO automatic_backup_pending VALUES (?,?,?,?,?,?)",
            (identifier, pid, datetime.now(UTC).isoformat(), 123, "c" * 64, "d" * 64),
        )
    restarted = service(client)
    assert restarted.report(pid)["backups"] == []
    assert not temporary.exists()
    assert unknown.read_bytes() == b"author-owned unknown file"
    with manager.connect() as db:
        assert db.execute("SELECT count(*) FROM automatic_backup_pending").fetchone()[0] == 0


def test_report_during_publication_does_not_reconcile_the_live_writer(client, project, monkeypatch):
    from novel_harness.services import automatic_backups

    manager, pid = service(client), project["id"]
    replace = automatic_backups.os.replace
    observed = []

    def report_before_publish(source, destination):
        if str(destination).endswith(".zip"):
            assert source.exists()
            observed.append(manager.report(pid)["backups"])
            assert source.exists()
            with manager.connect() as db:
                assert (
                    db.execute("SELECT count(*) FROM automatic_backup_pending").fetchone()[0] == 1
                )
        replace(source, destination)

    monkeypatch.setattr(automatic_backups.os, "replace", report_before_publish)
    assert manager.run(pid)["result"] == "created"
    assert observed == [[]]


def test_clock_rollback_does_not_suspend_opt_in_schedule_until_old_future_time(client, project):
    manager, pid = service(client), project["id"]
    manager.configure(pid, dict(enabled=True, interval_hours=24, retention_count=2, revision=0))
    later = datetime(2026, 9, 12, tzinfo=UTC)
    earlier = later - timedelta(days=1)
    manager.tick(later)
    manager.tick(earlier)
    assert manager.report(pid)["last_checked_at"] == earlier.isoformat()
