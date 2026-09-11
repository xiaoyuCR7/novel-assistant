import io
import json
import zipfile
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from job_helpers import create_chapter_version, save_chapter

from novel_harness.db.models import Asset
from novel_harness.main import create_app


def upload(client, action, archive, digest=None):
    return client.post(
        f"/api/v1/projects/backup/{action}",
        files={"file": ("backup.zip", archive, "application/zip")},
        data={"archive_hash": digest} if digest else {},
    )


@contextmanager
def other_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVEL_DATA_DIR", str(tmp_path / "destination"))
    with TestClient(create_app(start_executor=False)) as target:
        yield target


def repack(archive, changes=None, omit=()):
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(archive)) as source, zipfile.ZipFile(output, "w") as dest:
        for name in source.namelist():
            if name not in omit:
                dest.writestr(name, (changes or {}).get(name, source.read(name)))
        for name, data in (changes or {}).items():
            if name not in source.namelist():
                dest.writestr(name, data)
    return output.getvalue()


def test_backup_manifest_preview_and_restore_preserve_history_without_executing_jobs(
    client,
    project,
    seeded_chapter,
    tmp_path,
    monkeypatch,
):
    base = f"/api/v1/projects/{project['id']}"
    save_chapter(client, base + f"/chapters/{seeded_chapter}", json={"content": "已保存的故事正文"})
    pending = client.post(
        base + "/ai/jobs",
        headers={"Idempotency-Key": "pending-backup"},
        json={
            "project_id": project["id"],
            "task_type": "chat",
            "instructions": "恢复后不要自动生成",
        },
    ).json()
    archive = client.get(base + "/export").content
    with zipfile.ZipFile(io.BytesIO(archive)) as package:
        assert "backup-manifest.json" in package.namelist()
    preview = upload(client, "preview", archive)
    assert preview.status_code == 200, preview.text
    assert preview.json()["verified"] and preview.json()["conflict"]
    with other_workspace(tmp_path, monkeypatch) as target:
        preview = upload(target, "preview", archive)
        assert preview.status_code == 200, preview.text
        info = preview.json()
        assert info["counts"]["chapters"] == 1 and info["counts"]["jobs"] == 1
        restored = upload(target, "restore", archive, info["archive_hash"])
        assert restored.status_code == 201, restored.text
        assert restored.json()["id"] == project["id"]
        assert (
            target.get(base + f"/chapters/{seeded_chapter}").json()["content"] == "已保存的故事正文"
        )
        job = target.get(base + f"/ai/jobs/{pending['id']}").json()
        assert job["status"] == "recovery_required"
        assert job["instructions"] == "恢复后不要自动生成"
        assert not target.app.state.job_executor.run_once()
        assert upload(target, "restore", archive, info["archive_hash"]).status_code == 409


@pytest.mark.parametrize(
    "kind",
    [
        "path",
        "duplicate",
        "changed",
        "missing_db",
        "bad_db",
        "manifest_shape",
        "description_shape",
    ],
)
def test_invalid_backup_never_registers_a_project(client, project, tmp_path, monkeypatch, kind):
    archive = client.get(f"/api/v1/projects/{project['id']}/export").content
    if kind == "path":
        archive = repack(archive, {"../escape.txt": b"bad"})
    elif kind == "duplicate":
        archive = repack(archive, {"PROJECT.DB": b"bad"})
    elif kind == "changed":
        archive = repack(archive, {"project.json": b"{}"})
    elif kind == "missing_db":
        archive = repack(archive, omit=("project.db",))
    elif kind == "manifest_shape":
        archive = repack(archive, {"backup-manifest.json": json.dumps([])})
    elif kind == "description_shape":
        archive = repack(archive, {"project.json": json.dumps([])}, omit=("backup-manifest.json",))
    else:
        archive = repack(archive, {"project.db": b"not sqlite"}, omit=("backup-manifest.json",))
    with other_workspace(tmp_path, monkeypatch) as target:
        assert upload(target, "preview", archive).status_code == 422
        assert target.get("/api/v1/projects").json() == []
        assert not (tmp_path / "escape.txt").exists()


def test_legacy_backup_is_explicitly_unverified_and_hash_prevents_file_swap(
    client,
    project,
    tmp_path,
    monkeypatch,
):
    archive = client.get(f"/api/v1/projects/{project['id']}/export").content
    legacy = repack(archive, omit=("backup-manifest.json",))
    with other_workspace(tmp_path, monkeypatch) as target:
        response = upload(target, "preview", legacy)
        assert response.status_code == 200, response.text
        info = response.json()
        assert not info["verified"] and info["warnings"]
        swapped = upload(target, "restore", archive, info["archive_hash"])
        assert swapped.status_code == 409
        assert upload(target, "restore", legacy, info["archive_hash"]).status_code == 201


def test_restore_registration_failure_cleans_only_its_isolated_target(
    client,
    project,
    tmp_path,
    monkeypatch,
):
    archive = client.get(f"/api/v1/projects/{project['id']}/export").content
    with other_workspace(tmp_path, monkeypatch) as target:
        info = upload(target, "preview", archive).json()
        registry = target.app.state.vault_registry
        original = registry.register

        def fail(*_args):
            raise RuntimeError("injected registration failure")

        monkeypatch.setattr(registry, "register", fail)
        assert upload(target, "restore", archive, info["archive_hash"]).status_code == 500
        assert target.get("/api/v1/projects").json() == []
        assert not (registry.root / "projects" / project["id"]).exists()
        assert not list((registry.root / "projects").glob(".creating-*"))
        monkeypatch.setattr(registry, "register", original)
        assert upload(target, "restore", archive, info["archive_hash"]).status_code == 201


def test_manuscript_export_selects_volume_order_and_saved_source_only(client, project):
    base = f"/api/v1/projects/{project['id']}"
    chapters, volumes = [], []
    for number in (1, 2):
        volume = client.post(
            base + "/nodes",
            json={
                "kind": "volume",
                "title": f"卷{number}",
                "order_index": number,
            },
        ).json()
        volumes.append(volume["id"])
        chapter = client.post(
            base + "/nodes",
            json={
                "kind": "chapter",
                "title": f"章{number}",
                "parent_id": volume["id"],
            },
        ).json()
        chapters.append(chapter["id"])
        path = base + f"/chapters/{chapter['id']}"
        create_chapter_version(client, path + "/versions", json={"content": f"正式稿{number}"})
        save_chapter(client, path, json={"content": f"工作副本{number}"})
    output = client.post(
        base + "/manuscript/export",
        json={
            "format": "markdown",
            "source": "published",
            "node_ids": list(reversed(volumes)),
            "order": "story",
        },
    )
    assert output.status_code == 200, output.text
    assert output.text.index("正式稿1") < output.text.index("正式稿2")
    assert "工作副本" not in output.text and "project.db" not in output.text
    assert "attachment" in output.headers["content-disposition"]
    selected = client.post(
        base + "/manuscript/export",
        json={
            "format": "txt",
            "source": "working",
            "node_ids": list(reversed(chapters)),
            "order": "selection",
        },
    )
    assert selected.status_code == 200
    assert selected.text.index("工作副本2") < selected.text.index("工作副本1")
    assert "# " not in selected.text
    assert (
        client.post(
            base + "/manuscript/export",
            json={
                "format": "txt",
                "source": "working",
                "node_ids": ["foreign-chapter"],
            },
        ).status_code
        == 404
    )


def test_published_export_rejects_chapters_without_a_saved_version(client, project, seeded_chapter):
    response = client.post(
        f"/api/v1/projects/{project['id']}/manuscript/export",
        json={
            "source": "published",
            "node_ids": [seeded_chapter],
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "MANUSCRIPT_VERSION_REQUIRED"


def test_backup_restores_nested_assets_and_preserves_failed_asset_records(
    client, project, tmp_path, monkeypatch,
):
    vault = client.app.state.vault_registry.require(project['id'])
    nested = vault.root / 'assets' / 'nested' / 'scene.png'
    nested.parent.mkdir(parents=True)
    nested.write_bytes(b'fixture-image-bytes')
    with vault.database.session_scope() as session:
        session.add(Asset(id='ready-image', project_id=project['id'], kind='scene', prompt='',
                          status='ready', relative_path='assets/nested/scene.png'))
        session.add(Asset(id='failed-image', project_id=project['id'], kind='scene', prompt='',
                          status='failed', relative_path='', error_message='provider unavailable'))
    exported = client.get(f"/api/v1/projects/{project['id']}/export")
    assert exported.status_code == 200, exported.text
    with other_workspace(tmp_path, monkeypatch) as target:
        preview = upload(target, 'preview', exported.content)
        assert preview.status_code == 200, preview.text
        info = preview.json()
        assert info['counts']['assets'] == 2
        assert upload(target, 'restore', exported.content, info['archive_hash']).status_code == 201
        restored = target.app.state.vault_registry.require(project['id'])
        assert (restored.root / 'assets/nested/scene.png').read_bytes() == b'fixture-image-bytes'
        with restored.database.session_scope() as session:
            assert session.get(Asset, 'failed-image').status == 'failed'
        missing = repack(exported.content, omit=('assets/nested/scene.png', 'backup-manifest.json'))
        assert upload(target, 'preview', missing).status_code == 422


def test_restore_can_reconcile_a_process_loss_after_atomic_rename(
    client, project, tmp_path, monkeypatch,
):
    from novel_harness.db import vault as vault_module
    from novel_harness.services.backup_restore import restore_backup

    class SimulatedProcessLoss(BaseException):
        pass

    archive = client.get(f"/api/v1/projects/{project['id']}/export").content
    with other_workspace(tmp_path, monkeypatch) as target:
        digest = upload(target, 'preview', archive).json()['archive_hash']
        registry = target.app.state.vault_registry
        replace = vault_module.os.replace

        def interrupted(source, destination):
            replace(source, destination)
            if str(destination) == str(registry.root / 'projects' / project['id']):
                raise SimulatedProcessLoss()

        monkeypatch.setattr(vault_module.os, 'replace', interrupted)
        with pytest.raises(SimulatedProcessLoss):
            restore_backup(io.BytesIO(archive), digest, registry)
        assert target.get('/api/v1/projects').json() == []
        monkeypatch.setattr(vault_module.os, 'replace', replace)
        restored = upload(target, 'restore', archive, digest)
        assert restored.status_code == 201, restored.text
        assert restored.json()['id'] == project['id']
