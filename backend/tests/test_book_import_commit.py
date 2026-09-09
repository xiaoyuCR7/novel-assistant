from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path, PurePosixPath
from uuid import UUID, uuid5

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from novel_harness.db import vault as vault_module
from novel_harness.db.models import (
    ChapterDocument,
    ChapterVersion,
    ImportAnalysisUnit,
    ImportBatch,
    Project,
    ProjectContinuation,
    SourceDocument,
    StoryNode,
)
from novel_harness.db.session import Database
from novel_harness.db.vault import ProjectVaultRegistry
from novel_harness.schemas.imports import ImportBatchRead
from novel_harness.schemas.projects import ProjectCreate
from novel_harness.services import import_commit as import_commit_module


def _upload(client, *, display_name="旧稿", paths=None, contents=None):
    paths = paths or ["正文/第一章.md", "任务/续写.txt"]
    contents = contents or ["# 第一章\n风起。".encode(), "续写雨夜追查".encode()]
    response = client.post(
        "/api/v1/imports",
        data={"source_kind": "folder", "display_name": display_name, "paths": paths},
        files=[
            ("files", (f"upload-{index}.txt", content, "text/plain"))
            for index, content in enumerate(contents)
        ],
    )
    assert response.status_code == 201
    return response.json()


def _confirm(client, draft, *, completed=0, current=1):
    chapters = draft["chapters"]
    continuation = {
        "confirmed": True,
        "completed_through_node_id": chapters[completed]["draft_chapter_id"],
        "current_chapter_id": chapters[current]["draft_chapter_id"],
        "objective": "从雨夜继续",
        "source_document_ids": [
            item["audit_id"]
            for item in draft["files"]
            if item["category"] == "task" and item["selected"]
        ],
        "revision": 1,
    }
    response = client.patch(
        f"/api/v1/imports/{draft['draft_id']}",
        json={"revision": draft["revision"], "continuation": continuation},
    )
    assert response.status_code == 200
    return response.json()


def _commit(client, draft, expected_revision=None):
    return client.post(
        f"/api/v1/imports/{draft['draft_id']}/commit",
        json={"expected_revision": expected_revision or draft["revision"]},
    )


def test_draft_exposes_stable_chapter_and_source_audit_ids(client):
    first = _upload(client)
    restored = client.get(f"/api/v1/imports/{first['draft_id']}").json()

    assert all(item.get("audit_id") for item in first["files"])
    assert all(item.get("draft_chapter_id") for item in first["chapters"])
    assert [item["audit_id"] for item in restored["files"]] == [
        item["audit_id"] for item in first["files"]
    ]
    assert [item["draft_chapter_id"] for item in restored["chapters"]] == [
        item["draft_chapter_id"] for item in first["chapters"]
    ]


@pytest.mark.parametrize("target", ["file", "chapter"])
def test_draft_patch_rejects_mutated_stable_import_identity(client, target):
    draft = _upload(client)
    if target == "file":
        files = [dict(item) for item in draft["files"]]
        files[0]["audit_id"] = "forged-audit-id"
        body = {"revision": draft["revision"], "files": files}
    else:
        chapters = [dict(item) for item in draft["chapters"]]
        chapters[0]["draft_chapter_id"] = "forged-chapter-id"
        body = {"revision": draft["revision"], "chapters": chapters}

    response = client.patch(f"/api/v1/imports/{draft['draft_id']}", json=body)

    assert response.status_code == 422
    assert response.json()["detail"]["code"] in {
        "IMPORT_FILE_UNKNOWN",
        "IMPORT_CHAPTER_UNKNOWN",
    }


def test_commit_requires_confirmation_and_revalidates_durable_raw(client, tmp_path):
    draft = _upload(client)

    unconfirmed = _commit(client, draft)
    assert unconfirmed.status_code == 422
    assert unconfirmed.json()["detail"]["code"] == "IMPORT_CONFIRMATION_REQUIRED"

    confirmed = _confirm(client, draft, completed=0, current=0)
    raw = tmp_path / "imports" / draft["draft_id"] / "raw" / "正文" / "第一章.md"
    raw.write_bytes(b"tampered")
    changed = _commit(client, confirmed)
    assert changed.status_code == 422
    assert changed.json()["detail"]["code"] == "IMPORT_SOURCE_CHANGED"
    assert client.get("/api/v1/projects").json() == []


def test_commit_creates_isolated_vault_and_exact_import_graph(client, tmp_path):
    paths = [
        "正文/合集.md",
        "正文/第三章.txt",
        "任务/续写.txt",
        "设定/人物.txt",
    ]
    contents = [
        "# 第一章\n风起。\n# 第二章\n雨落。".encode(),
        "第三章\n灯亮。".encode(),
        "续写雨夜追查".encode(),
        "人物：阿雾".encode("gb18030"),
    ]
    draft = _upload(client, display_name="雾城旧稿", paths=paths, contents=contents)

    # Classification is intentionally deterministic but conservative; confirmation
    # explicitly assigns the imported roles before commit.
    files = []
    for item in draft["files"]:
        category = (
            "manuscript"
            if item["relative_path"].startswith("正文/")
            else "task"
            if item["relative_path"].startswith("任务/")
            else "character"
        )
        files.append({**item, "category": category, "selected": True})
    patched = client.patch(
        f"/api/v1/imports/{draft['draft_id']}",
        json={"revision": draft["revision"], "files": files},
    )
    assert patched.status_code == 200
    confirmed = _confirm(client, patched.json(), completed=0, current=1)

    response = _commit(client, confirmed)
    assert response.status_code == 201
    committed = response.json()
    assert set(committed) == {"project", "import_batch_id"}
    assert committed["project"]["title"] == "雾城旧稿"
    project_id = committed["project"]["id"]
    vault_root = tmp_path / "projects" / project_id
    assert (vault_root / "project.db").is_file()
    assert client.get("/api/v1/projects").json()[0]["id"] == project_id

    vault = client.app.state.vault_registry.require(project_id, job_only=True)
    with vault.database.session_scope() as session:
        assert session.get(Project, project_id).title == "雾城旧稿"
        chapters = list(
            session.scalars(
                select(StoryNode)
                .where(StoryNode.kind == "chapter")
                .order_by(StoryNode.order_index)
            )
        )
        assert [item.order_index for item in chapters] == [0, 1, 2]
        assert [item.status for item in chapters] == ["completed", "drafting", "planned"]
        documents = [session.get(ChapterDocument, item.id) for item in chapters]
        assert [item.content for item in documents] == [
            "# 第一章\n风起。\n",
            "# 第二章\n雨落。",
            "第三章\n灯亮。",
        ]
        for document in documents:
            version = session.get(ChapterVersion, document.current_version_id)
            assert version is not None
            assert version.source == "import"
            assert version.content == document.content
            expected_version_id = str(
                uuid5(
                    UUID(project_id),
                    f"version:{document.chapter_id}:import:"
                    f"{hashlib.sha256(document.content.encode()).hexdigest()}",
                )
            )
            assert version.id == expected_version_id

        sources = list(
            session.scalars(select(SourceDocument).order_by(SourceDocument.relative_path))
        )
        assert {source.relative_path for source in sources} == set(paths)
        for source in sources:
            assert PurePosixPath(source.stored_path).parts[:3] == (
                "imports",
                committed["import_batch_id"],
                "sources",
            )
            stored = vault.resolve(source.stored_path)
            assert stored.is_file()
            assert hashlib.sha256(stored.read_bytes()).hexdigest() == source.byte_hash
        by_path = {source.relative_path: source for source in sources}
        assert by_path["正文/合集.md"].chapter_id is None
        assert by_path["正文/第三章.txt"].chapter_id == chapters[2].id

        batch = session.get(ImportBatch, committed["import_batch_id"])
        assert batch.status == "imported"
        assert batch.manifest["draft_id"] == draft["draft_id"]
        assert batch.manifest["continuation"]["confirmed"] is True
        assert batch.manifest["chapter_count"] == len(chapters)
        assert len(batch.manifest["chapter_order_digest"]) == 64
        continuation = session.get(ProjectContinuation, project_id)
        assert continuation.completed_through_node_id == chapters[0].id
        assert continuation.current_chapter_id == chapters[1].id
        assert continuation.objective == "从雨夜继续"
        expected_audit_ids = [
            item["audit_id"]
            for item in confirmed["files"]
            if item["category"] == "task"
        ]
        assert continuation.source_document_ids == expected_audit_ids

        units = list(session.scalars(select(ImportAnalysisUnit)))
        assert batch.total_units == len(units)
        assert batch.completed_units == 0
        assert {unit.kind for unit in units} == {"source_memory", "summary_map"}
        assert len({unit.unit_key for unit in units}) == len(units)
        assert all(unit.source_document_id in unit.unit_key for unit in units)
        assert all(unit.source_hash in unit.unit_key for unit in units)


def test_commit_is_idempotent_concurrent_and_freezes_draft(client):
    draft = _upload(
        client,
        paths=["正文/一.md", "正文/二.md"],
        contents=[b"one", b"two"],
    )
    files = [{**item, "category": "manuscript"} for item in draft["files"]]
    patched = client.patch(
        f"/api/v1/imports/{draft['draft_id']}",
        json={"revision": draft["revision"], "files": files},
    ).json()
    confirmed = _confirm(client, patched)

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: _commit(client, confirmed), range(2)))

    assert sorted(response.status_code for response in responses) == [201, 201]
    bodies = [response.json() for response in responses]
    assert bodies[0] == bodies[1]
    assert len(client.get("/api/v1/projects").json()) == 1

    repeated = _commit(client, confirmed)
    assert repeated.status_code == 201
    assert repeated.json() == bodies[0]
    stale = client.post(
        f"/api/v1/imports/{draft['draft_id']}/commit",
        json={"expected_revision": confirmed["revision"] - 1},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "DRAFT_REVISION_CONFLICT"
    for response in (
        client.patch(
            f"/api/v1/imports/{draft['draft_id']}",
            json={"revision": confirmed["revision"], "objective": "change"},
        ),
        client.delete(f"/api/v1/imports/{draft['draft_id']}"),
    ):
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "IMPORT_DRAFT_COMMITTED"


def test_registered_replay_returns_current_project_without_revalidating_initial_graph(
    client
):
    draft = _upload(client)
    confirmed = _confirm(client, draft, completed=0, current=0)
    committed = _commit(client, confirmed)
    assert committed.status_code == 201
    project_id = committed.json()["project"]["id"]
    vault = client.app.state.vault_registry.require(project_id, job_only=True)
    with vault.database.session_scope() as session:
        project = session.get(Project, project_id)
        project.title = "edited title"
        project.target_words = 321_000
        document = session.scalar(select(ChapterDocument))
        document.content = "edited after import"

    replayed = _commit(client, confirmed)

    assert replayed.status_code == 201
    assert replayed.json()["project"]["id"] == project_id
    assert replayed.json()["project"]["title"] == "edited title"
    assert replayed.json()["project"]["target_words"] == 321_000
    with vault.database.session_scope() as session:
        assert session.scalar(select(ChapterDocument)).content == "edited after import"


def test_registered_replay_rejects_changed_import_fingerprint(client):
    draft = _upload(client)
    confirmed = _confirm(client, draft, completed=0, current=0)
    committed = _commit(client, confirmed).json()
    vault = client.app.state.vault_registry.require(committed["project"]["id"])
    with vault.database.session_scope() as session:
        batch = session.get(ImportBatch, committed["import_batch_id"])
        batch.manifest = {**batch.manifest, "graph_digest": "0" * 64}

    replayed = _commit(client, confirmed)

    assert replayed.status_code == 409
    assert replayed.json()["detail"]["code"] == "IMPORT_PROJECT_COLLISION"


def test_registered_replay_accepts_legacy_frozen_draft_without_fingerprint(client):
    draft = _upload(client)
    confirmed = _confirm(client, draft, completed=0, current=0)
    committed = _commit(client, confirmed)
    assert committed.status_code == 201
    metadata = client.app.state.import_drafts.root / draft["draft_id"] / "draft.json"
    legacy = json.loads(metadata.read_text(encoding="utf-8"))
    legacy.pop("committed_fingerprint")
    metadata.write_text(json.dumps(legacy), encoding="utf-8")

    replayed = _commit(client, confirmed)

    assert replayed.status_code == 201
    assert replayed.json() == committed.json()


def test_register_failure_is_invisible_and_retry_recovers_final_vault(
    client, tmp_path, monkeypatch
):
    draft = _upload(client)
    confirmed = _confirm(client, draft, completed=0, current=0)
    registry = client.app.state.vault_registry
    original_register = registry.register
    calls = 0

    def fail_once(project_id, title):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("registry unavailable")
        return original_register(project_id, title)

    monkeypatch.setattr(registry, "register", fail_once)
    failed = _commit(client, confirmed)
    assert failed.status_code == 500
    assert client.get("/api/v1/projects").json() == []
    final_dirs = [
        path for path in (tmp_path / "projects").iterdir() if not path.name.startswith(".")
    ]
    assert len(final_dirs) == 1
    frozen_patch = client.patch(
        f"/api/v1/imports/{draft['draft_id']}",
        json={"revision": confirmed["revision"], "objective": "must not change"},
    )
    assert frozen_patch.status_code == 409
    assert frozen_patch.json()["detail"]["code"] == "IMPORT_DRAFT_COMMITTED"

    recovered = _commit(client, confirmed)
    assert recovered.status_code == 201
    assert recovered.json()["project"]["id"] == final_dirs[0].name
    assert len(client.get("/api/v1/projects").json()) == 1

    with closing(sqlite3.connect(final_dirs[0] / "project.db")) as db, db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_registry_commit_then_exception_is_reconciled_as_success(
    client, tmp_path, monkeypatch
):
    draft = _upload(client)
    confirmed = _confirm(client, draft, completed=0, current=0)
    registry = client.app.state.vault_registry
    original_register = registry.register

    def committed_then_failed(project_id, title):
        original_register(project_id, title)
        raise OSError("lost acknowledgement after commit")

    monkeypatch.setattr(registry, "register", committed_then_failed)
    response = _commit(client, confirmed)

    assert response.status_code == 201
    project_id = response.json()["project"]["id"]
    assert [item["id"] for item in registry.list_records()] == [project_id]
    restarted = ProjectVaultRegistry(tmp_path)
    assert [item["id"] for item in restarted.list_records()] == [project_id]
    restarted.dispose()


@pytest.mark.parametrize("signal", [KeyboardInterrupt, SystemExit])
def test_registry_reconciliation_never_swallows_process_control_exceptions(
    tmp_path, monkeypatch, signal
):
    registry = ProjectVaultRegistry(tmp_path)
    original_register = registry.register

    def committed_then_interrupted(project_id, title):
        original_register(project_id, title)
        raise signal()

    monkeypatch.setattr(registry, "register", committed_then_interrupted)

    with pytest.raises(signal):
        registry._register_or_reconcile("interrupt-project", "interrupt")
    registry.dispose()


@pytest.mark.parametrize("conflict", ["title", "path"])
def test_registry_register_rejects_conflicting_persistent_identity(tmp_path, conflict):
    registry = ProjectVaultRegistry(tmp_path)
    project_id = "safe-project"
    registry.register(project_id, "original")
    if conflict == "path":
        with registry.connect() as db:
            db.execute(
                "UPDATE project_registry SET vault_path=? WHERE id=?",
                ("projects/different", project_id),
            )

    with pytest.raises(HTTPException) as exc:
        registry.register(project_id, "different" if conflict == "title" else "original")

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "PROJECT_REGISTRY_CONFLICT"
    assert project_id not in registry._vaults
    registry.dispose()


@pytest.mark.parametrize("conflict", ["title", "path"])
def test_import_never_caches_over_conflicting_registry_row(
    client, conflict
):
    draft = _upload(client)
    confirmed = _confirm(client, draft, completed=0, current=0)
    registry = client.app.state.vault_registry
    project_id = import_commit_module._project_id(draft["draft_id"])
    with registry.connect() as db:
        db.execute(
            """INSERT INTO project_registry
            (id,title,vault_path,last_opened_at) VALUES (?,?,?,'now')""",
            (
                project_id,
                "wrong" if conflict == "title" else draft["title"],
                (
                    f"projects/{project_id}"
                    if conflict == "title"
                    else "projects/different"
                ),
            ),
        )

    response = _commit(client, confirmed)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "PROJECT_REGISTRY_CONFLICT"
    assert project_id not in registry._vaults


@pytest.mark.parametrize("failure_point", ["copy", "initializer", "rename"])
def test_prepublication_failures_leave_no_visible_or_partial_vault(
    client, tmp_path, monkeypatch, failure_point
):
    draft = _upload(client)
    confirmed = _confirm(client, draft, completed=0, current=0)

    if failure_point == "copy":
        monkeypatch.setattr(
            import_commit_module,
            "_copy_source",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("copy failed")),
        )
    elif failure_point == "initializer":
        monkeypatch.setattr(
            import_commit_module,
            "create_version",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("init failed")),
        )
    else:
        original_replace = vault_module.os.replace

        def fail_publish(source, destination):
            if Path(source).name.startswith(".creating-"):
                raise OSError("rename failed")
            return original_replace(source, destination)

        monkeypatch.setattr(vault_module.os, "replace", fail_publish)

    failed = _commit(client, confirmed)
    assert failed.status_code == 500
    assert client.get("/api/v1/projects").json() == []
    projects_root = tmp_path / "projects"
    if projects_root.exists():
        assert not [
            path for path in projects_root.iterdir() if not path.name.startswith(".")
        ]
    assert client.get(f"/api/v1/imports/{draft['draft_id']}").status_code == 200


def test_retry_cleans_only_matching_stale_creating_directory(client, tmp_path):
    draft = _upload(client)
    confirmed = _confirm(client, draft, completed=0, current=0)
    project_id = import_commit_module._project_id(draft["draft_id"])
    projects_root = tmp_path / "projects"
    own_stale = projects_root / f".creating-{project_id}-{'a' * 32}"
    other_stale = projects_root / f".creating-{'b' * 36}-{'c' * 32}"
    own_stale.mkdir(parents=True)
    other_stale.mkdir()
    (own_stale / "partial").write_text("stale", encoding="utf-8")
    (other_stale / "keep").write_text("safe", encoding="utf-8")

    response = _commit(client, confirmed)

    assert response.status_code == 201
    assert not own_stale.exists()
    assert (other_stale / "keep").read_text(encoding="utf-8") == "safe"


def test_registry_startup_does_not_sweep_creating_directories(tmp_path):
    projects_root = tmp_path / "projects"
    project_id = "12345678-1234-4234-8234-123456789abc"
    stale = projects_root / f".creating-{project_id}-{'a' * 32}"
    invalid = projects_root / ".creating-not-a-project"
    final = projects_root / "87654321-4321-4321-8321-cba987654321"
    stale.mkdir(parents=True)
    invalid.mkdir()
    final.mkdir()
    (stale / "partial").write_text("stale", encoding="utf-8")
    (invalid / "keep").write_text("safe", encoding="utf-8")
    (final / "keep").write_text("final", encoding="utf-8")

    registry = ProjectVaultRegistry(tmp_path)
    registry.dispose()

    assert (stale / "partial").read_text(encoding="utf-8") == "stale"
    assert (invalid / "keep").read_text(encoding="utf-8") == "safe"
    assert (final / "keep").read_text(encoding="utf-8") == "final"


def test_draft_freeze_write_failure_removes_final_and_allows_retry(
    client, tmp_path, monkeypatch
):
    draft = _upload(client)
    confirmed = _confirm(client, draft, completed=0, current=0)
    store = client.app.state.import_drafts
    original_write = store._write_json
    calls = 0

    def fail_once(draft_dir, record):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("draft metadata unavailable")
        return original_write(draft_dir, record)

    monkeypatch.setattr(store, "_write_json", fail_once)
    failed = _commit(client, confirmed)
    assert failed.status_code == 500
    assert client.get("/api/v1/projects").json() == []
    projects_root = tmp_path / "projects"
    assert not projects_root.exists() or not [
        path for path in projects_root.iterdir() if not path.name.startswith(".")
    ]

    retried = _commit(client, confirmed)
    assert retried.status_code == 201


def test_multichapter_continuation_uses_stable_chapter_ids(client):
    body = "# 第一章\nA\n# 第二章\nB\n# 第三章\nC"
    draft = _upload(client, paths=["正文/全书.md"], contents=[body.encode()])
    confirmed = _confirm(client, draft, completed=1, current=2)

    response = _commit(client, confirmed)
    assert response.status_code == 201
    vault = client.app.state.vault_registry.require(response.json()["project"]["id"])
    with vault.database.session_scope() as session:
        chapters = list(
            session.scalars(select(StoryNode).order_by(StoryNode.order_index))
        )
        assert [chapter.status for chapter in chapters] == [
            "completed",
            "completed",
            "drafting",
        ]
        summary_units = list(
            session.scalars(
                select(ImportAnalysisUnit).where(
                    ImportAnalysisUnit.kind == "summary_map"
                )
            )
        )
        assert {unit.chapter_id for unit in summary_units} == {
            chapters[0].id,
            chapters[1].id,
        }


def test_repeated_equal_chunks_have_distinct_ordinal_unit_identity(client):
    body = "x" * 2220
    draft = _upload(client, paths=["正文/重复.txt"], contents=[body.encode()])
    confirmed = _confirm(client, draft, completed=0, current=0)

    response = _commit(client, confirmed)
    assert response.status_code == 201
    vault = client.app.state.vault_registry.require(response.json()["project"]["id"])
    with vault.database.session_scope() as session:
        units = list(
            session.scalars(
                select(ImportAnalysisUnit).where(
                    ImportAnalysisUnit.kind == "source_memory"
                )
            )
        )
        assert len(units) == 2
        assert len({unit.unit_key for unit in units}) == 2
        assert all(
            f":{unit.chunk_key.rsplit(':', 1)[-1]}:" in unit.unit_key
            for unit in units
        )


def test_same_source_equal_completed_chapters_have_unique_summary_identity(client):
    body = "# 相同章\n相同内容\n# 相同章\n相同内容\n# 当前章\n续写"
    draft = _upload(client, paths=["正文/重复章.md"], contents=[body.encode()])
    confirmed = _confirm(client, draft, completed=1, current=2)

    response = _commit(client, confirmed)

    assert response.status_code == 201
    vault = client.app.state.vault_registry.require(response.json()["project"]["id"])
    with vault.database.session_scope() as session:
        chapters = list(
            session.scalars(select(StoryNode).order_by(StoryNode.order_index))
        )
        units = list(
            session.scalars(
                select(ImportAnalysisUnit).where(
                    ImportAnalysisUnit.kind == "summary_map"
                )
            )
        )
        assert [chapter.status for chapter in chapters] == [
            "completed",
            "completed",
            "drafting",
        ]
        assert len(units) == 2
        assert len({unit.id for unit in units}) == 2
        assert len({unit.unit_key for unit in units}) == 2
        assert {unit.chapter_id for unit in units} == {
            chapters[0].id,
            chapters[1].id,
        }
        assert all(unit.chapter_id in unit.unit_key for unit in units)


def test_explicit_empty_continuation_sources_stay_empty(client):
    draft = _upload(client)
    chapter = draft["chapters"][0]
    patched = client.patch(
        f"/api/v1/imports/{draft['draft_id']}",
        json={
            "revision": draft["revision"],
            "continuation": {
                "confirmed": True,
                "completed_through_node_id": chapter["draft_chapter_id"],
                "current_chapter_id": chapter["draft_chapter_id"],
                "objective": "continue",
                "source_document_ids": [],
                "revision": 1,
            },
        },
    )
    assert patched.status_code == 200

    response = _commit(client, patched.json())

    assert response.status_code == 201
    vault = client.app.state.vault_registry.require(response.json()["project"]["id"])
    with vault.database.session_scope() as session:
        continuation = session.get(ProjectContinuation, response.json()["project"]["id"])
        assert continuation.source_document_ids == []


@pytest.mark.parametrize(
    "damage", ["title", "source", "node", "node_target_words", "unit", "raw"]
)
def test_existing_final_requires_complete_canonical_graph(
    client, monkeypatch, damage
):
    draft = _upload(client)
    confirmed = _confirm(client, draft, completed=0, current=0)
    registry = client.app.state.vault_registry
    original_register = registry.register
    monkeypatch.setattr(
        registry,
        "register",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("registry down")),
    )
    assert _commit(client, confirmed).status_code == 500
    monkeypatch.setattr(registry, "register", original_register)
    project_id = import_commit_module._project_id(draft["draft_id"])
    path = registry.root / "projects" / project_id / "project.db"
    with closing(sqlite3.connect(path)) as db, db:
        if damage == "title":
            db.execute("UPDATE projects SET title='forged'")
        elif damage == "source":
            db.execute("DELETE FROM source_documents")
        elif damage == "node":
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute("DELETE FROM story_nodes")
        elif damage == "node_target_words":
            db.execute("UPDATE story_nodes SET target_words=99")
        elif damage == "unit":
            db.execute("DELETE FROM import_analysis_units")
        else:
            stored = db.execute("SELECT stored_path FROM source_documents LIMIT 1").fetchone()[0]
            (path.parent / PurePosixPath(stored)).write_bytes(b"forged")

    retried = _commit(client, confirmed)
    assert retried.status_code == 409
    assert retried.json()["detail"]["code"] == "IMPORT_PROJECT_COLLISION"
    assert client.get("/api/v1/projects").json() == []
    assert path.parent.is_dir()


@pytest.mark.parametrize(
    "field",
    [
        "files_digest",
        "chapters_digest",
        "chapter_order_digest",
        "continuation",
        "file_count",
        "graph_digest",
    ],
)
def test_existing_final_rejects_tampered_canonical_manifest_top_level(
    client, monkeypatch, field
):
    draft = _upload(client)
    confirmed = _confirm(client, draft, completed=0, current=0)
    registry = client.app.state.vault_registry
    original_register = registry.register
    monkeypatch.setattr(
        registry,
        "register",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("registry down")),
    )
    assert _commit(client, confirmed).status_code == 500
    monkeypatch.setattr(registry, "register", original_register)
    project_id = import_commit_module._project_id(draft["draft_id"])
    path = registry.root / "projects" / project_id / "project.db"
    with closing(sqlite3.connect(path)) as db, db:
        batch_id, raw_manifest = db.execute(
            "SELECT id,manifest FROM import_batches"
        ).fetchone()
        manifest = json.loads(raw_manifest)
        if field == "continuation":
            manifest[field]["objective"] = "forged"
        elif field == "file_count":
            manifest[field] += 1
        else:
            manifest[field] = "0" * 64
        db.execute(
            "UPDATE import_batches SET manifest=? WHERE id=?",
            (json.dumps(manifest), batch_id),
        )

    retried = _commit(client, confirmed)

    assert retried.status_code == 409
    assert retried.json()["detail"]["code"] == "IMPORT_PROJECT_COLLISION"
    assert client.get("/api/v1/projects").json() == []


def test_minimal_forged_project_and_batch_is_never_registered(client, tmp_path):
    draft = _upload(client)
    confirmed = _confirm(client, draft, completed=0, current=0)
    project_id = import_commit_module._project_id(draft["draft_id"])
    batch_id = import_commit_module._batch_id(project_id, draft["draft_id"])
    root = tmp_path / "projects" / project_id
    root.mkdir(parents=True)
    database = Database(root / "project.db")
    database.create_schema()
    with database.session_scope() as session:
        session.add(Project(id=project_id, title=draft["title"]))
        session.flush()
        session.add(
            ImportBatch(
                id=batch_id,
                project_id=project_id,
                source_kind=draft["source_kind"],
                manifest={"draft_id": draft["draft_id"]},
                status="imported",
            )
        )
    database.dispose()

    response = _commit(client, confirmed)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "IMPORT_PROJECT_COLLISION"
    assert client.get("/api/v1/projects").json() == []


def test_valid_unregistered_final_refreezes_unfrozen_draft_then_registers(
    client, monkeypatch
):
    draft = _upload(client)
    confirmed = _confirm(client, draft, completed=0, current=0)
    registry = client.app.state.vault_registry
    original_register = registry.register
    monkeypatch.setattr(
        registry,
        "register",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("registry down")),
    )
    assert _commit(client, confirmed).status_code == 500
    metadata = client.app.state.import_drafts.root / draft["draft_id"] / "draft.json"
    durable = json.loads(metadata.read_text(encoding="utf-8"))
    durable["committed_project_id"] = None
    durable["committed_batch_id"] = None
    metadata.write_text(json.dumps(durable), encoding="utf-8")
    monkeypatch.setattr(registry, "register", original_register)

    recovered = _commit(client, confirmed)

    assert recovered.status_code == 201
    frozen = json.loads(metadata.read_text(encoding="utf-8"))
    assert frozen["committed_project_id"] == recovered.json()["project"]["id"]
    assert frozen["committed_batch_id"] == recovered.json()["import_batch_id"]


def test_unknown_continuation_source_audit_id_is_rejected(client):
    draft = _upload(client)
    chapter = draft["chapters"][0]
    response = client.patch(
        f"/api/v1/imports/{draft['draft_id']}",
        json={
            "revision": draft["revision"],
            "continuation": {
                "confirmed": True,
                "completed_through_node_id": chapter["draft_chapter_id"],
                "current_chapter_id": chapter["draft_chapter_id"],
                "source_document_ids": ["unknown-audit-id"],
                "revision": 1,
            },
        },
    )
    assert response.status_code == 200

    committed = _commit(client, response.json())

    assert committed.status_code == 422
    assert committed.json()["detail"]["code"] == "IMPORT_CONTINUATION_SOURCE_INVALID"


def test_project_creation_lock_blocks_cleanup_and_creation_for_same_project(
    tmp_path
):
    registry = ProjectVaultRegistry(tmp_path)
    projects_root = tmp_path / "projects"
    projects_root.mkdir(exist_ok=True)
    project_id = "12345678-1234-4234-8234-123456789abc"
    stale = projects_root / f".creating-{project_id}-{'a' * 32}"
    stale.mkdir()

    with vault_module._ProjectCreationLock(projects_root, project_id):
        with pytest.raises(HTTPException) as exc:
            registry._create_vault(
                ProjectCreate(title="busy"),
                project_id,
                lambda _session, _root: None,
            )

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "PROJECT_CREATION_BUSY"
    assert stale.is_dir()
    registry.dispose()


def test_project_creation_lock_rejects_reparse_lock_file(
    tmp_path, monkeypatch
):
    registry = ProjectVaultRegistry(tmp_path)
    projects_root = tmp_path / "projects"
    lock_file = projects_root / ".locks" / "safe-project.lock"
    lock_file.parent.mkdir(parents=True)
    lock_file.write_bytes(b"\0")
    original_check = vault_module._path_is_reparse_point
    monkeypatch.setattr(
        vault_module,
        "_path_is_reparse_point",
        lambda path: Path(path) == lock_file or original_check(path),
    )

    with pytest.raises(RuntimeError, match="UNSAFE_PROJECT_LOCK"):
        registry._create_vault(
            ProjectCreate(title="safe"),
            "safe-project",
            lambda _session, _root: None,
        )

    assert registry.list_records() == []
    registry.dispose()


def test_project_lock_checks_reparse_even_when_exists_reports_false(
    tmp_path, monkeypatch
):
    registry = ProjectVaultRegistry(tmp_path)
    projects_root = tmp_path / "projects"
    lock_file = projects_root / ".locks" / "safe-project.lock"
    lock_file.parent.mkdir(parents=True)
    lock_file.write_bytes(b"\0")
    original_exists = Path.exists
    original_check = vault_module._path_is_reparse_point
    monkeypatch.setattr(
        Path,
        "exists",
        lambda path: False if path == lock_file else original_exists(path),
    )
    monkeypatch.setattr(
        vault_module,
        "_path_is_reparse_point",
        lambda path: Path(path) == lock_file or original_check(path),
    )

    with pytest.raises(RuntimeError, match="UNSAFE_PROJECT_LOCK_FILE"):
        registry._create_vault(
            ProjectCreate(title="safe"),
            "safe-project",
            lambda _session, _root: None,
        )

    assert registry.list_records() == []
    assert not (projects_root / "safe-project").exists()
    registry.dispose()


def test_project_lock_rejects_real_broken_symlink(tmp_path):
    registry = ProjectVaultRegistry(tmp_path)
    projects_root = tmp_path / "projects"
    lock_file = projects_root / ".locks" / "safe-project.lock"
    lock_file.parent.mkdir(parents=True)
    try:
        lock_file.symlink_to(projects_root / "missing-lock-target")
    except OSError:
        pytest.skip("file symlinks are unavailable")

    with pytest.raises(RuntimeError, match="UNSAFE_PROJECT_LOCK_FILE"):
        registry._create_vault(
            ProjectCreate(title="safe"),
            "safe-project",
            lambda _session, _root: None,
        )

    assert registry.list_records() == []
    assert not (projects_root / "safe-project").exists()
    registry.dispose()


def test_project_creation_lock_blocks_a_real_competing_process(tmp_path):
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    project_id = "subprocess-project"
    script = "\n".join(
        (
            "import pathlib, sys",
            "from novel_harness.db.vault import _ProjectCreationLock",
            "root = pathlib.Path(sys.argv[1])",
            "with _ProjectCreationLock(root, sys.argv[2]):",
            "    print('ready', flush=True)",
            "    sys.stdin.readline()",
        )
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(projects_root), project_id],
        cwd=Path(__file__).parents[1],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "ready"
        registry = ProjectVaultRegistry(tmp_path)
        with pytest.raises(HTTPException) as exc:
            registry._create_vault(
                ProjectCreate(title="busy"), project_id, lambda _session, _root: None
            )
        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "PROJECT_CREATION_BUSY"
        registry.dispose()
    finally:
        if process.stdin is not None:
            process.stdin.write("stop\n")
            process.stdin.flush()
        process.wait(timeout=10)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def test_rename_validation_failure_removes_only_new_final_and_allows_retry(
    client, tmp_path, monkeypatch
):
    draft = _upload(client)
    confirmed = _confirm(client, draft, completed=0, current=0)
    registry = client.app.state.vault_registry
    original_validate = registry._validate_final_vault
    monkeypatch.setattr(
        registry,
        "_validate_final_vault",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("post-rename validation failed")
        ),
    )

    failed = _commit(client, confirmed)

    assert failed.status_code == 500
    project_id = import_commit_module._project_id(draft["draft_id"])
    assert not (tmp_path / "projects" / project_id).exists()
    assert registry.list_records() == []
    monkeypatch.setattr(registry, "_validate_final_vault", original_validate)
    assert _commit(client, confirmed).status_code == 201


def test_final_directory_is_synced_after_rename_before_draft_freeze(
    tmp_path, monkeypatch
):
    registry = ProjectVaultRegistry(tmp_path)
    events = []
    original_replace = vault_module.os.replace

    def replace(source, destination):
        events.append(("rename", Path(destination)))
        return original_replace(source, destination)

    monkeypatch.setattr(vault_module.os, "replace", replace)
    monkeypatch.setattr(
        vault_module,
        "_fsync_directory",
        lambda path: events.append(("fsync", Path(path))),
        raising=False,
    )
    project_id = "durable-project"
    registry._create_vault(
        ProjectCreate(title="durable"),
        project_id,
        lambda _session, _root: None,
        before_register=lambda: events.append(("freeze", None)),
    )

    rename_index = next(i for i, event in enumerate(events) if event[0] == "rename")
    parent_sync_index = next(
        i
        for i, event in enumerate(events)
        if event == ("fsync", tmp_path / "projects")
    )
    freeze_index = next(i for i, event in enumerate(events) if event[0] == "freeze")
    assert rename_index < parent_sync_index < freeze_index
    registry.dispose()


def test_nested_source_directory_chain_is_synced_before_publish(
    client, tmp_path, monkeypatch
):
    draft = _upload(
        client,
        paths=["正文/甲/乙/丙/第一章.md"],
        contents=[b"nested chapter"],
    )
    confirmed = _confirm(client, draft, completed=0, current=0)
    store = client.app.state.import_drafts
    events = []
    original_replace = vault_module.os.replace
    original_write = store._write_json

    monkeypatch.setattr(
        vault_module,
        "_fsync_directory",
        lambda path: events.append(("fsync", Path(path))),
    )

    def replace(source, destination):
        events.append(("rename", Path(source), Path(destination)))
        return original_replace(source, destination)

    def write(draft_dir, record):
        if record.committed_project_id is not None:
            events.append(("freeze", draft_dir))
        return original_write(draft_dir, record)

    monkeypatch.setattr(vault_module.os, "replace", replace)
    monkeypatch.setattr(store, "_write_json", write)

    response = _commit(client, confirmed)

    assert response.status_code == 201
    rename_index = next(
        i
        for i, event in enumerate(events)
        if event[0] == "rename" and event[1].name.startswith(".creating-")
    )
    rename_event = events[rename_index]
    temporary = rename_event[1]
    batch_id = response.json()["import_batch_id"]
    source_parent = (
        temporary / "imports" / batch_id / "sources" / "正文" / "甲" / "乙" / "丙"
    )
    expected_chain = []
    cursor = source_parent
    while True:
        expected_chain.append(cursor)
        if cursor == temporary:
            break
        cursor = cursor.parent
    for directory in expected_chain:
        assert ("fsync", directory) in events, events
        sync_index = next(
            i
            for i, event in enumerate(events)
            if event == ("fsync", directory)
        )
        assert sync_index < rename_index
    projects_sync = next(
        i
        for i, event in enumerate(events)
        if event == ("fsync", tmp_path / "projects") and i > rename_index
    )
    freeze_index = next(i for i, event in enumerate(events) if event[0] == "freeze")
    assert rename_index < projects_sync < freeze_index


def test_directory_chain_sync_is_safe_and_best_effort(tmp_path, monkeypatch):
    root = tmp_path / "root"
    leaf = root / "one" / "two" / "three"
    leaf.mkdir(parents=True)
    calls = []

    def flaky_sync(path):
        calls.append(Path(path))
        if Path(path).name == "two":
            raise OSError("directory sync unavailable")

    monkeypatch.setattr(vault_module, "_fsync_directory", flaky_sync)

    vault_module._fsync_directory_chain(leaf, root)

    assert calls == [leaf, leaf.parent, leaf.parent.parent, root]
    with pytest.raises(RuntimeError, match="UNSAFE_FSYNC_DIRECTORY_CHAIN"):
        vault_module._fsync_directory_chain(tmp_path, root)


@pytest.mark.parametrize("shape", ["files", "chapters"])
def test_large_confirmed_manifest_stays_payload_bounded(client, shape):
    if shape == "files":
        paths = ["正文/第一章.md"] + [f"资料/{index:03d}.txt" for index in range(200)]
        contents = [b"chapter"] + [f"note-{index}".encode() for index in range(200)]
        draft = _upload(client, paths=paths, contents=contents)
        files = [
            {
                **item,
                "category": "manuscript" if index == 0 else "other",
            }
            for index, item in enumerate(draft["files"])
        ]
        draft = client.patch(
            f"/api/v1/imports/{draft['draft_id']}",
            json={"revision": draft["revision"], "files": files},
        ).json()
        confirmed = _confirm(client, draft, completed=0, current=0)
    else:
        body = "\n".join(f"# 第{index}章\n内容{index}" for index in range(201))
        draft = _upload(client, paths=["正文/长篇.md"], contents=[body.encode()])
        confirmed = _confirm(client, draft, completed=0, current=1)

    response = _commit(client, confirmed)

    assert response.status_code == 201
    vault = client.app.state.vault_registry.require(response.json()["project"]["id"])
    with vault.database.session_scope() as session:
        batch = session.get(ImportBatch, response.json()["import_batch_id"])
        validated = ImportBatchRead.model_validate(batch)
        assert validated.manifest["file_count"] == len(confirmed["files"])
        assert validated.manifest["chapter_count"] == len(confirmed["chapters"])
        assert not any(
            isinstance(value, list) and len(value) > 200
            for value in validated.manifest.values()
        )


def test_verified_source_index_does_not_accumulate_raw_or_decoded_payloads(client):
    draft = _upload(
        client,
        paths=["正文/一.md", "资料/二.txt", "资料/三.txt"],
        contents=[b"chapter", b"reference-two", b"reference-three"],
    )
    draft = _confirm(client, draft, completed=0, current=0)
    store = client.app.state.import_drafts
    record = store.get(draft["draft_id"])

    indexed = import_commit_module._read_verified_sources(store, record)

    assert len(indexed) == 3
    assert all(
        len(entry) == 2
        and isinstance(entry[0], import_commit_module.DraftFile)
        and isinstance(entry[1], Path)
        for entry in indexed.values()
    )


def test_registry_startup_never_removes_reparse_creating_entry(
    tmp_path, monkeypatch
):
    projects_root = tmp_path / "projects"
    project_id = "12345678-1234-4234-8234-123456789abc"
    candidate = projects_root / f".creating-{project_id}-{'a' * 32}"
    candidate.mkdir(parents=True)
    sentinel = candidate / "keep"
    sentinel.write_text("safe", encoding="utf-8")
    monkeypatch.setattr(
        vault_module,
        "_path_is_reparse_point",
        lambda path: Path(path) == candidate,
        raising=False,
    )

    registry = ProjectVaultRegistry(tmp_path)
    registry.dispose()

    assert sentinel.read_text(encoding="utf-8") == "safe"


def test_registry_require_rejects_replaced_projects_reparse_root(
    client, tmp_path, monkeypatch
):
    project = client.post("/api/v1/projects", json={"title": "safe"}).json()
    registry = client.app.state.vault_registry
    cached = registry._vaults.pop(project["id"])
    cached.database.dispose()
    projects_root = tmp_path / "projects"
    monkeypatch.setattr(
        vault_module,
        "_path_is_reparse_point",
        lambda path: Path(path) == projects_root,
    )

    with pytest.raises(HTTPException) as exc:
        registry.require(project["id"])

    assert exc.value.status_code == 404
    assert exc.value.detail["code"] == "PROJECT_NOT_FOUND"


def test_whitespace_title_is_rejected_again_at_commit(client):
    draft = _upload(client)
    confirmed = _confirm(client, draft, completed=0, current=0)
    metadata = (
        client.app.state.import_drafts.root / draft["draft_id"] / "draft.json"
    )
    durable = json.loads(metadata.read_text(encoding="utf-8"))
    durable["display_name"] = "   "
    metadata.write_text(json.dumps(durable), encoding="utf-8")

    response = _commit(client, confirmed)

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "IMPORT_TITLE_REQUIRED"


def test_patch_rejects_blank_project_title(client):
    draft = _upload(client)

    response = client.patch(
        f"/api/v1/imports/{draft['draft_id']}",
        json={"revision": draft["revision"], "title": "   "},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INVALID_REQUEST"


def test_schema_maintenance_adds_import_source_size_column(tmp_path):
    path = tmp_path / "legacy-project.db"
    database = Database(path)
    database.create_schema()
    database.dispose()
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("ALTER TABLE source_documents DROP COLUMN size_bytes")

    restored = Database(path)
    restored.create_schema()
    restored.dispose()

    with closing(sqlite3.connect(path)) as connection, connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(source_documents)")
        }
    assert "size_bytes" in columns


@pytest.mark.parametrize("body", [{}, {"expected_revision": "1"}, {"revision": 1}])
def test_commit_request_requires_strict_expected_revision(client, body):
    draft = _upload(client)
    response = client.post(f"/api/v1/imports/{draft['draft_id']}/commit", json=body)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INVALID_REQUEST"
