from __future__ import annotations

import gc
import io
import json
import multiprocessing
import os
import stat
import struct
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path
from threading import Event, get_ident
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.datastructures import UploadFile

from novel_harness.config import (
    DEFAULT_IMPORT_MAX_COMPRESSION_RATIO,
    DEFAULT_IMPORT_MAX_FILE_BYTES,
    DEFAULT_IMPORT_MAX_FILES,
    DEFAULT_IMPORT_MAX_TOTAL_BYTES,
    Settings,
)
from novel_harness.services import import_drafts as import_drafts_module
from novel_harness.services.import_drafts import (
    ImportDraftStore,
    ImportLimits,
    classify_document,
    decode_text,
    normalize_relative_path,
    split_chapters,
)


def error_code(exc: pytest.ExceptionInfo[HTTPException]) -> str:
    assert isinstance(exc.value.detail, dict)
    return str(exc.value.detail["code"])


def make_zip(entries: list[tuple[str, bytes, int | None]]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content, mode in entries:
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_DEFLATED
            if mode is not None:
                info.create_system = 3
                info.external_attr = mode << 16
            archive.writestr(info, content)
    return output.getvalue()


def make_stored_zip(entries: list[tuple[str, bytes]]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return output.getvalue()


def _folder_upload(
    client,
    *,
    display_name: str = "旧稿",
    paths: list[str] | None = None,
    contents: list[bytes] | None = None,
):
    selected_paths = paths or ["正文/第一章.md", "设定/人物.txt"]
    selected_contents = contents or ["# 第一章\n风起。".encode(), "# 人物\n阿雾".encode()]
    return client.post(
        "/api/v1/imports",
        data={
            "source_kind": "folder",
            "display_name": display_name,
            "paths": selected_paths,
        },
        files=[
            ("files", (f"upload-{index}.txt", content, "text/plain"))
            for index, content in enumerate(selected_contents)
        ],
    )


def test_import_draft_api_folder_lifecycle_and_restart(client) -> None:
    created = _folder_upload(client)

    assert created.status_code == 201
    payload = created.json()
    assert set(payload) == {
        "draft_id",
        "title",
        "source_kind",
        "manifest",
        "files",
        "chapters",
        "continuation",
        "objective",
        "revision",
    }
    assert payload["source_kind"] == "folder"
    assert {item["relative_path"] for item in payload["files"]} == {
        "设定/人物.txt",
        "正文/第一章.md",
    }

    client.app.state.import_drafts = ImportDraftStore(
        client.app.state.settings.data_dir / "imports"
    )
    restored = client.get(f"/api/v1/imports/{payload['draft_id']}")
    assert restored.status_code == 200
    assert restored.json() == payload

    changed = client.patch(
        f"/api/v1/imports/{payload['draft_id']}",
        json={"revision": 1, "objective": "续写雨夜调查"},
    )
    assert changed.status_code == 200
    assert changed.json()["revision"] == 2
    assert changed.json()["objective"] == "续写雨夜调查"

    stale = client.patch(
        f"/api/v1/imports/{payload['draft_id']}",
        json={"revision": 1, "objective": "stale"},
    )
    assert stale.status_code == 409
    detail = stale.json()["detail"]
    assert detail["code"] == "DRAFT_REVISION_CONFLICT"
    assert set(detail["current"]) == {
        "draft_id",
        "title",
        "source_kind",
        "manifest",
        "files",
        "chapters",
        "continuation",
        "objective",
        "revision",
    }
    assert detail["current"] == changed.json()
    assert all(isinstance(item["content_preview"], str) for item in detail["current"]["files"])
    assert {
        "chapter_inventory",
        "created_at",
        "updated_at",
        "display_name",
        "warnings",
        "ignored",
    }.isdisjoint(detail["current"])

    discarded = client.delete(f"/api/v1/imports/{payload['draft_id']}")
    assert discarded.status_code == 204
    missing = client.get(f"/api/v1/imports/{payload['draft_id']}")
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "IMPORT_DRAFT_NOT_FOUND"


def test_import_draft_api_exposes_only_bounded_plain_text_file_preview(client) -> None:
    hostile = "<img src=x onerror=alert('owned')>" + ("任务资料" * 1_500)
    created = _folder_upload(
        client,
        paths=["正文/第一章.md", "人物/角色.txt", "任务/下一步.txt", "杂项/剪贴.txt"],
        contents=[
            "# 第一章\n不会公开的正文。".encode(),
            "# 人物\n不会公开的角色设定。".encode(),
            hostile.encode(),
            "普通资料".encode(),
        ],
    )

    assert created.status_code == 201
    files = {item["relative_path"]: item for item in created.json()["files"]}
    task = files["任务/下一步.txt"]
    other = files["杂项/剪贴.txt"]

    assert task["category"] == "task"
    assert task["content_preview"].startswith("<img src=x onerror=alert('owned')>")
    assert len(task["content_preview"]) == 4000
    assert other["category"] == "other"
    assert other["content_preview"] == "普通资料"
    assert files["正文/第一章.md"]["category"] == "manuscript"
    assert files["正文/第一章.md"]["content_preview"] == ""
    assert files["人物/角色.txt"]["category"] == "character"
    assert files["人物/角色.txt"]["content_preview"] == ""
    assert created.json()["chapters"]
    assert all(item["content_preview"] == "" for item in created.json()["chapters"])
    for item in files.values():
        assert "content" not in item
        assert "raw_bytes" not in item
        assert "source_path" not in item


def test_public_redacted_chapter_preview_can_be_patched_back(client) -> None:
    created = _folder_upload(
        client,
        paths=["正文/第一章.md", "正文/第二章.md"],
        contents=["# 第一章\n一".encode(), "# 第二章\n二".encode()],
    )
    payload = created.json()

    assert created.status_code == 201
    assert all(chapter["content_preview"] == "" for chapter in payload["chapters"])

    changed = client.patch(
        f"/api/v1/imports/{payload['draft_id']}",
        json={
            "revision": payload["revision"],
            "files": payload["files"],
            "chapters": payload["chapters"],
        },
    )

    assert changed.status_code == 200
    assert [chapter["title"] for chapter in changed.json()["chapters"]] == [
        "第一章",
        "第二章",
    ]


@pytest.mark.parametrize(
    ("data", "files", "expected_code"),
    [
        (
            {"source_kind": "folder", "display_name": "book", "paths": ["one.txt"]},
            [],
            "INVALID_REQUEST",
        ),
        (
            {"source_kind": "folder", "display_name": "book", "paths": ["one.txt"]},
            [("files", ("one.txt", b"one")), ("files", ("two.txt", b"two"))],
            "IMPORT_PATH_COUNT_MISMATCH",
        ),
        (
            {"source_kind": "zip", "display_name": "book"},
            [("files", ("one.zip", b"one")), ("files", ("two.zip", b"two"))],
            "IMPORT_ZIP_FILE_COUNT",
        ),
        (
            {"source_kind": "directory", "display_name": "book", "paths": ["one.txt"]},
            [("files", ("one.txt", b"one"))],
            "INVALID_REQUEST",
        ),
        (
            {
                "source_kind": "folder",
                "display_name": "book",
                "paths": ["ONE.txt", "one.txt"],
            },
            [("files", ("a.txt", b"one")), ("files", ("b.txt", b"two"))],
            "UNSAFE_IMPORT_PATH",
        ),
    ],
)
def test_import_draft_api_rejects_invalid_upload_shapes(
    client, data, files, expected_code
) -> None:
    response = client.post("/api/v1/imports", data=data, files=files)

    assert 400 <= response.status_code < 500
    assert response.json()["detail"]["code"] == expected_code


def test_import_draft_api_zip_uses_archive_safety_and_requires_zip_filename(client) -> None:
    archive = make_zip([("../escape.txt", b"no", None)])
    unsafe = client.post(
        "/api/v1/imports",
        data={"source_kind": "zip", "display_name": "book"},
        files={"files": ("book.zip", archive, "application/zip")},
    )
    assert unsafe.status_code == 422
    assert unsafe.json()["detail"]["code"] == "UNSAFE_ARCHIVE_PATH"

    wrong_extension = client.post(
        "/api/v1/imports",
        data={"source_kind": "zip", "display_name": "book"},
        files={"files": ("book.bin", make_zip([("one.txt", b"one", None)]))},
    )
    assert wrong_extension.status_code == 422
    assert wrong_extension.json()["detail"]["code"] == "IMPORT_ZIP_REQUIRED"


def test_zip_container_can_exceed_entry_limit_when_each_entry_is_bounded(client) -> None:
    archive = make_stored_zip(
        [("one.txt", b"a" * (6 * 1024 * 1024)), ("two.txt", b"b" * (6 * 1024 * 1024))]
    )
    assert len(archive) > 10 * 1024 * 1024

    response = client.post(
        "/api/v1/imports",
        data={"source_kind": "zip", "display_name": "large archive"},
        files={"files": ("book.zip", archive, "application/zip")},
    )

    assert response.status_code == 201
    assert {item["relative_path"] for item in response.json()["files"]} == {
        "one.txt",
        "two.txt",
    }


def test_zip_entry_still_obeys_single_file_limit(client) -> None:
    archive = make_stored_zip([("too-large.txt", b"x" * (10 * 1024 * 1024 + 1))])

    response = client.post(
        "/api/v1/imports",
        data={"source_kind": "zip", "display_name": "large entry"},
        files={"files": ("book.zip", archive, "application/zip")},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "IMPORT_FILE_TOO_LARGE"


def test_import_draft_api_folder_ignores_unsupported_files(client) -> None:
    response = _folder_upload(
        client,
        paths=["one.txt", "cover.png"],
        contents=[b"chapter", b"image"],
    )

    assert response.status_code == 201
    assert [item["relative_path"] for item in response.json()["files"]] == ["one.txt"]


def test_import_draft_api_validation_and_identifier_errors_are_stable(client) -> None:
    created = _folder_upload(client).json()
    draft_url = f"/api/v1/imports/{created['draft_id']}"

    for body in (
        {"revision": 1, "extra": True},
        {"revision": "1", "objective": "bad"},
    ):
        response = client.patch(draft_url, json=body)
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "INVALID_REQUEST"

    for method in (client.get, client.delete):
        malformed = method("/api/v1/imports/not-a-uuid")
        assert malformed.status_code == 404
        assert malformed.json()["detail"]["code"] == "IMPORT_DRAFT_NOT_FOUND"
        absent = method("/api/v1/imports/12345678-1234-4234-8234-123456789abc")
        assert absent.status_code == 404
        assert absent.json()["detail"]["code"] == "IMPORT_DRAFT_NOT_FOUND"


def test_import_draft_api_preserves_atomic_revision_conflicts(client) -> None:
    created = _folder_upload(client).json()
    url = f"/api/v1/imports/{created['draft_id']}"

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(
            executor.map(
                lambda objective: client.patch(
                    url, json={"revision": 1, "objective": objective}
                ),
                ["first", "second"],
            )
        )

    assert sorted(response.status_code for response in responses) == [200, 409]
    conflict = next(response for response in responses if response.status_code == 409)
    assert conflict.json()["detail"]["code"] == "DRAFT_REVISION_CONFLICT"


def test_import_create_obeys_same_origin_boundary(client) -> None:
    response = client.post(
        "/api/v1/imports",
        headers={"Origin": "https://evil.example"},
        data={"source_kind": "folder", "display_name": "book", "paths": ["one.txt"]},
        files={"files": ("one.txt", b"one")},
    )

    assert response.status_code == 403


def test_import_uploads_are_read_in_bounded_chunks(client, monkeypatch) -> None:
    sizes: list[int] = []
    route_threads: set[int] = set()
    fsync_threads: set[int] = set()
    closed_states: list[bool] = []
    original_read = UploadFile.read
    original_close = UploadFile.close
    original_fsync = os.fsync

    async def recording_read(self, size=-1):
        sizes.append(size)
        route_threads.add(get_ident())
        return await original_read(self, size)

    def recording_fsync(descriptor):
        fsync_threads.add(get_ident())
        return original_fsync(descriptor)

    async def recording_close(self):
        await original_close(self)
        closed_states.append(self.file.closed)

    monkeypatch.setattr(UploadFile, "read", recording_read)
    monkeypatch.setattr(UploadFile, "close", recording_close)
    monkeypatch.setattr(os, "fsync", recording_fsync)
    response = _folder_upload(client, contents=[b"a" * 70_000, b"b"])

    assert response.status_code == 201
    assert sizes
    assert set(sizes) == {64 * 1024}
    assert fsync_threads
    assert route_threads.isdisjoint(fsync_threads)
    assert len(closed_states) >= 2
    assert all(closed_states)


def test_blocked_store_worker_does_not_block_health_endpoint(client, monkeypatch) -> None:
    store = client.app.state.import_drafts
    original = store.create_from_staged_files
    entered = Event()
    release = Event()
    route_threads: set[int] = set()
    store_threads: set[int] = set()
    original_read = UploadFile.read

    async def recording_read(self, size=-1):
        route_threads.add(get_ident())
        return await original_read(self, size)

    def blocked_create(display_name, files):
        store_threads.add(get_ident())
        entered.set()
        assert release.wait(5)
        return original(display_name, files)

    monkeypatch.setattr(UploadFile, "read", recording_read)
    monkeypatch.setattr(store, "create_from_staged_files", blocked_create)
    with ThreadPoolExecutor(max_workers=2) as executor:
        upload = executor.submit(_folder_upload, client)
        assert entered.wait(5)
        health = executor.submit(client.get, "/api/v1/health")
        try:
            health_response = health.result(timeout=1)
        except FutureTimeoutError:
            pytest.fail("blocked import store operation blocked the event loop")
        finally:
            release.set()
        assert upload.result(timeout=5).status_code == 201

    assert health_response.status_code == 200
    assert route_threads
    assert store_threads
    assert route_threads.isdisjoint(store_threads)


def test_store_startup_removes_only_uuid_staging_children(
    tmp_path: Path, caplog
) -> None:
    root = tmp_path / "imports"
    stale = root / ".uploads" / uuid4().hex
    stale.mkdir(parents=True)
    (stale / "payload").write_bytes(b"dead")
    invalid = root / ".uploads" / "keep-me"
    invalid.mkdir()

    ImportDraftStore(root)

    assert not stale.exists()
    assert invalid.is_dir()
    assert "invalid import upload staging entry" in caplog.text.lower()


def test_store_startup_unlinks_staging_symlink_without_following_target(
    tmp_path: Path,
) -> None:
    root = tmp_path / "imports"
    upload_root = root / ".uploads"
    upload_root.mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    (external / "keep.txt").write_text("safe", encoding="utf-8")
    link = upload_root / uuid4().hex
    try:
        link.symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")

    ImportDraftStore(root)

    assert not link.exists()
    assert (external / "keep.txt").read_text(encoding="utf-8") == "safe"


def test_store_startup_fails_closed_for_reparse_upload_root(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "imports"
    upload_root = root / ".uploads"
    upload_root.mkdir(parents=True)
    monkeypatch.setattr(
        import_drafts_module,
        "_path_is_reparse_point",
        lambda path: Path(path) == upload_root,
        raising=False,
    )

    with pytest.raises(RuntimeError, match="IMPORT_UPLOAD_STAGING_UNSAFE"):
        ImportDraftStore(root)


def test_reparse_detection_uses_windows_file_attributes() -> None:
    class ReparseMetadata:
        st_mode = stat.S_IFDIR
        st_file_attributes = 0x400

    class FakePath:
        def lstat(self):
            return ReparseMetadata()

    assert import_drafts_module._path_is_reparse_point(FakePath()) is True


def test_store_startup_fails_closed_for_real_upload_root_symlink(
    tmp_path: Path,
) -> None:
    root = tmp_path / "imports"
    root.mkdir()
    external = tmp_path / "external-root"
    external.mkdir()
    (external / "keep.txt").write_text("safe", encoding="utf-8")
    upload_root = root / ".uploads"
    try:
        upload_root.symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")

    with pytest.raises(RuntimeError, match="IMPORT_UPLOAD_STAGING_UNSAFE"):
        ImportDraftStore(root)

    assert (external / "keep.txt").read_text(encoding="utf-8") == "safe"


def test_route_revalidates_upload_root_after_startup(client, monkeypatch) -> None:
    root = client.app.state.import_drafts.root
    upload_root = root / ".uploads"
    upload_root.mkdir()
    monkeypatch.setattr(
        import_drafts_module,
        "_path_is_reparse_point",
        lambda path: Path(path) == upload_root,
        raising=False,
    )

    response = _folder_upload(client)

    assert response.status_code == 500
    assert list(upload_root.iterdir()) == []


def test_route_rejects_replaced_upload_root_symlink_without_touching_target(
    client,
) -> None:
    root = client.app.state.import_drafts.root
    upload_root = root / ".uploads"
    external = root.parent / "external-upload-target"
    external.mkdir()
    sentinel = external / "keep.txt"
    sentinel.write_text("safe", encoding="utf-8")
    try:
        upload_root.symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")

    response = _folder_upload(client)

    assert response.status_code == 500
    assert list(external.iterdir()) == [sentinel]


def test_worker_failure_cleans_staging_and_closes_uploads(client, monkeypatch) -> None:
    store = client.app.state.import_drafts
    closed_states: list[bool] = []
    original_close = UploadFile.close

    def fail_create(display_name, files):
        raise RuntimeError("worker failed")

    async def recording_close(self):
        await original_close(self)
        closed_states.append(self.file.closed)

    monkeypatch.setattr(store, "create_from_staged_files", fail_create)
    monkeypatch.setattr(UploadFile, "close", recording_close)

    response = _folder_upload(client)

    assert response.status_code == 500
    assert not (store.root / ".uploads").exists()
    assert len(closed_states) >= 2
    assert all(closed_states)


def test_store_startup_retries_staging_cleanup_after_oserror(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    root = tmp_path / "imports"
    stale = root / ".uploads" / uuid4().hex
    stale.mkdir(parents=True)
    original_rmtree = import_drafts_module.shutil.rmtree

    def deny_once(path):
        if Path(path) == stale:
            raise PermissionError("busy")
        return original_rmtree(path)

    monkeypatch.setattr(import_drafts_module.shutil, "rmtree", deny_once)
    ImportDraftStore(root)
    assert stale.is_dir()
    assert "could not remove stale import upload" in caplog.text.lower()

    monkeypatch.setattr(import_drafts_module.shutil, "rmtree", original_rmtree)
    ImportDraftStore(root)
    assert not stale.exists()


@pytest.mark.parametrize(
    ("limits", "contents", "expected_code"),
    [
        (ImportLimits(max_file_bytes=65_536, max_total_bytes=100_000), [b"x" * 65_537],
         "IMPORT_FILE_TOO_LARGE"),
        (ImportLimits(max_file_bytes=70_000, max_total_bytes=100_000),
         [b"x" * 60_000, b"y" * 60_000], "IMPORT_TOTAL_TOO_LARGE"),
    ],
)
def test_import_upload_limits_reject_and_clean_staging(
    client, limits, contents, expected_code
) -> None:
    root = client.app.state.settings.data_dir / "imports"
    client.app.state.import_drafts = ImportDraftStore(root, limits)
    response = _folder_upload(
        client,
        paths=[f"{index}.txt" for index in range(len(contents))],
        contents=contents,
    )

    assert response.status_code == 413
    assert response.json()["detail"]["code"] == expected_code
    assert not (root / ".uploads").exists()


def test_import_file_count_limit_is_checked_before_reading_uploads(
    client, monkeypatch
) -> None:
    root = client.app.state.settings.data_dir / "imports"
    client.app.state.import_drafts = ImportDraftStore(
        root, ImportLimits(max_files=1)
    )
    reads = 0
    original_read = UploadFile.read

    async def recording_read(self, size=-1):
        nonlocal reads
        reads += 1
        return await original_read(self, size)

    monkeypatch.setattr(UploadFile, "read", recording_read)
    response = _folder_upload(
        client,
        paths=["one.txt", "two.txt"],
        contents=[b"one", b"two"],
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "IMPORT_FILE_LIMIT"
    assert reads == 0


def test_zip_expanded_limits_are_rechecked_after_streaming(client) -> None:
    root = client.app.state.settings.data_dir / "imports"
    client.app.state.import_drafts = ImportDraftStore(
        root,
        ImportLimits(
            max_file_bytes=1024,
            max_total_bytes=2048,
            max_compression_ratio=1000,
        ),
    )
    archive = make_zip([("large.txt", b"x" * 1500, None)])
    assert len(archive) < 1024

    response = client.post(
        "/api/v1/imports",
        data={"source_kind": "zip", "display_name": "book"},
        files={"files": ("book.zip", archive, "application/zip")},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "IMPORT_FILE_TOO_LARGE"
    assert not (root / ".uploads").exists()


def test_staged_folder_creation_does_not_fall_back_to_in_memory_file_api(
    tmp_path: Path, monkeypatch
) -> None:
    store = ImportDraftStore(tmp_path / "imports")
    staged = tmp_path / "staged.txt"
    staged.write_bytes(b"chapter")
    monkeypatch.setattr(
        store,
        "create_from_files",
        lambda *args, **kwargs: pytest.fail("staged files were aggregated in memory"),
    )

    draft = store.create_from_staged_files("book", [("one.txt", staged)])

    assert [item.relative_path for item in draft.files] == ["one.txt"]


def test_staged_zip_creation_does_not_fall_back_to_in_memory_zip_api(
    tmp_path: Path, monkeypatch
) -> None:
    store = ImportDraftStore(tmp_path / "imports")
    staged = tmp_path / "book.zip"
    staged.write_bytes(make_zip([("one.txt", b"chapter", None)]))
    monkeypatch.setattr(
        store,
        "create_from_zip",
        lambda *args, **kwargs: pytest.fail("staged ZIP was aggregated in memory"),
    )

    draft = store.create_from_zip_path("book", staged)

    assert [item.relative_path for item in draft.files] == ["one.txt"]


def test_staged_zip_write_failure_removes_partial_draft(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "imports"
    store = ImportDraftStore(root)
    staged = tmp_path / "book.zip"
    staged.write_bytes(make_zip([("one.txt", b"chapter", None)]))

    def fail_after_metadata_appears(draft_dir: Path, record) -> None:
        (draft_dir / "draft.json").write_text("{}", encoding="utf-8")
        raise OSError("disk failed")

    monkeypatch.setattr(store, "_write_json", fail_after_metadata_appears)
    with pytest.raises(HTTPException):
        store.create_from_zip_path("book", staged)

    assert [path for path in root.iterdir() if path.name != ".locks"] == []


def hold_import_draft_lock(
    root: str,
    draft_id: str,
    ready: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
) -> None:
    with import_drafts_module._DraftLifecycleLock(Path(root), draft_id):
        ready.set()
        release.wait(10)


def mutate_import_draft_in_process(
    root: str,
    draft_id: str,
    action: str,
    start: multiprocessing.synchronize.Event,
    results: multiprocessing.queues.Queue,
) -> None:
    start.wait(10)
    store = ImportDraftStore(Path(root))
    try:
        if action == "update":
            store.update(draft_id, {"revision": 1, "objective": "process"})
        else:
            store.discard(draft_id)
    except HTTPException as exc:
        assert isinstance(exc.detail, dict)
        results.put(str(exc.detail["code"]))
    except BaseException as exc:  # pragma: no cover - assertion diagnostic
        results.put(type(exc).__name__)
    else:
        results.put("ok")


@pytest.mark.parametrize(
    ("raw", "expected_encoding"),
    [
        ("雪落无声".encode(), "utf-8"),
        (b"\xef\xbb\xbf" + "雪落无声".encode(), "utf-8-sig"),
        ("雪落无声".encode("gb18030"), "gb18030"),
    ],
)
def test_decode_text_is_strict_and_lossless(raw: bytes, expected_encoding: str) -> None:
    text, encoding = decode_text(raw)

    assert text == "雪落无声"
    assert encoding == expected_encoding


def test_decode_text_rejects_undecodable_bytes() -> None:
    with pytest.raises(HTTPException) as exc:
        decode_text(b"\x81")

    assert exc.value.status_code == 422
    assert error_code(exc) == "IMPORT_DECODE_FAILED"


def test_folder_import_discovers_only_text_sources_and_reports_ignored(tmp_path: Path) -> None:
    store = ImportDraftStore(tmp_path / "imports")

    draft = store.create_from_files(
        "my book",
        [
            ("正文/第一章.MD", "# 第一章\n风起。".encode()),
            ("notes.TXT", "随笔".encode()),
            ("cover.png", b"not really an image"),
        ],
    )

    assert [item.relative_path for item in draft.files] == ["notes.TXT", "正文/第一章.MD"]
    assert draft.ignored == [
        {"relative_path": "cover.png", "reason": "UNSUPPORTED_FILE_TYPE"}
    ]
    assert draft.warnings
    assert (tmp_path / "imports" / draft.draft_id / "raw" / "cover.png").is_file()


@pytest.mark.parametrize("source_kind", ["folder", "zip"])
def test_import_draft_api_returns_stable_visible_parse_warnings(
    client, source_kind: str
) -> None:
    limits = ImportLimits(
        max_files=20,
        max_file_bytes=100,
        max_total_bytes=1_000,
        max_compression_ratio=100,
    )
    client.app.state.import_drafts = ImportDraftStore(
        client.app.state.settings.data_dir / f"warning-imports-{source_kind}", limits
    )
    manifest = {
        "version": 1,
        "files": [{"path": "dup/a.md", "selected": False}],
    }
    entries = [
        (
            "novel-import.json",
            json.dumps(manifest, separators=(",", ":")).encode(),
        ),
        ("dup/a.md", "相同内容".encode()),
        ("dup/b.txt", "相同内容".encode("gb18030")),
        ("正文/明确.md", "# 第一章\n明确正文。".encode()),
        ("正文/无标题.txt", "没有章节标题的正文。".encode()),
        ("资料/大文件.txt", b"x" * 80),
        ("资料/空文件.txt", b""),
    ]
    if source_kind == "folder":
        response = _folder_upload(
            client,
            display_name="警告书稿",
            paths=[path for path, _ in entries],
            contents=[content for _, content in entries],
        )
    else:
        response = client.post(
            "/api/v1/imports",
            data={"source_kind": "zip", "display_name": "警告书稿"},
            files={
                "files": (
                    "book.zip",
                    make_zip([(path, content, None) for path, content in entries]),
                    "application/zip",
                )
            },
        )

    assert response.status_code == 201
    payload = response.json()
    assert [item["relative_path"] for item in payload["files"]] == sorted(
        (path for path, _ in entries if Path(path).suffix.casefold() in {".md", ".txt"}),
        key=str.casefold,
    )
    files = {item["relative_path"]: item for item in payload["files"]}
    duplicate_warning = (
        "检测到 2 份文件具有相同解码内容；系统不会自动删除副本，请确认要保留的文件。"
        "对应路径（稳定排序）：dup/a.md、dup/b.txt。"
    )
    assert files["dup/a.md"]["warning"] == duplicate_warning
    assert files["dup/b.txt"]["warning"] == duplicate_warning
    assert files["dup/a.md"]["selected"] is False
    assert files["dup/b.txt"]["selected"] is True
    assert files["资料/空文件.txt"]["warning"] == "文件解码后为空。"
    assert files["资料/大文件.txt"]["warning"] == (
        "文件大小接近单文件有效上限（80/100 字节，警告阈值 80%）。"
    )
    assert files["正文/无标题.txt"]["warning"] == (
        "章节边界不确定：未识别到明确章节标题，请在预览中确认名称和顺序。"
    )
    assert files["正文/明确.md"]["warning"] == ""

    restored = client.get(f"/api/v1/imports/{payload['draft_id']}")
    assert restored.status_code == 200
    assert restored.json()["files"] == payload["files"]


def test_duplicate_warning_preserves_action_count_and_stable_paths_within_budget(
    tmp_path: Path,
) -> None:
    paths = [f"重复/{index:03d}-{'x' * 48}.txt" for index in range(60)]
    draft = ImportDraftStore(tmp_path / "imports").create_from_files(
        "book", [(path, b"duplicate") for path in reversed(paths)]
    )

    warnings = {item.warning for item in draft.files}
    assert len(warnings) == 1
    warning = warnings.pop()
    prefix = (
        "检测到 60 份文件具有相同解码内容；系统不会自动删除副本，请确认要保留的文件。"
        "对应路径（稳定排序）："
    )
    assert warning.startswith(prefix)
    assert len(warning) <= 2_000
    shown_text, omission_text = warning[len(prefix) :].split("；另有 ", maxsplit=1)
    shown_paths = shown_text.split("、")
    omitted_count = int(omission_text.removesuffix(" 项路径已省略。"))
    assert shown_paths == paths[: len(shown_paths)]
    assert omitted_count == len(paths) - len(shown_paths)
    assert omitted_count > 0


def _legal_path_too_long_for_one_warning(character: str) -> str:
    path = "/".join([character * 200 for _ in range(10)]) + f"/{character}.txt"
    assert len(path) > 2_000
    return normalize_relative_path(path)


def test_duplicate_warning_omits_all_paths_without_losing_fixed_guidance() -> None:
    paths = sorted(
        [
            _legal_path_too_long_for_one_warning("b"),
            _legal_path_too_long_for_one_warning("a"),
        ],
        key=str.casefold,
    )

    warning = import_drafts_module._duplicate_content_warning(paths, 2_000)

    assert warning == (
        "检测到 2 份文件具有相同解码内容；系统不会自动删除副本，请确认要保留的文件。"
        "另有 2 项路径已省略。"
    )
    assert len(warning) <= 2_000
    assert import_drafts_module._duplicate_content_warning(paths, 2_000) == warning


@pytest.mark.parametrize("source_kind", ["folder", "zip"])
def test_import_draft_api_returns_long_legal_duplicate_paths_without_500(
    client, monkeypatch: pytest.MonkeyPatch, source_kind: str
) -> None:
    store = client.app.state.import_drafts
    record = store.create_from_files(
        "constructed-long-path-draft",
        [("one.txt", b"duplicate"), ("two.txt", b"duplicate")],
    )
    paths = [
        _legal_path_too_long_for_one_warning("a"),
        _legal_path_too_long_for_one_warning("b"),
    ]
    files = [
        item.model_copy(
            update={
                "relative_path": path,
                "audit_id": f"long-path-{index}",
                "warning": "",
            }
        )
        for index, (item, path) in enumerate(zip(record.files, paths, strict=True))
    ]
    import_drafts_module._apply_parse_warnings(files, set(), store.limits)
    constructed = record.model_copy(
        update={
            "source_kind": source_kind,
            "files": files,
            "chapters": [],
            "chapter_inventory": [],
        }
    )
    method_name = (
        "create_from_staged_files" if source_kind == "folder" else "create_from_zip_path"
    )
    monkeypatch.setattr(
        ImportDraftStore, method_name, lambda self, *args, **kwargs: constructed
    )

    if source_kind == "folder":
        response = _folder_upload(
            client,
            display_name="超长路径",
            paths=["one.txt", "two.txt"],
            contents=[b"duplicate", b"duplicate"],
        )
    else:
        response = client.post(
            "/api/v1/imports",
            data={"source_kind": "zip", "display_name": "超长路径"},
            files={
                "files": (
                    "book.zip",
                    make_zip([("one.txt", b"duplicate", None)]),
                    "application/zip",
                )
            },
        )

    assert response.status_code == 201
    warnings = {item["warning"] for item in response.json()["files"]}
    assert warnings == {
        "检测到 2 份文件具有相同解码内容；系统不会自动删除副本，请确认要保留的文件。"
        "另有 2 项路径已省略。"
    }
    assert all(len(warning) <= 2_000 for warning in warnings)


@pytest.mark.parametrize(
    ("path", "text", "category"),
    [
        ("正文/第一章.md", "第一章 风起\n内容", "manuscript"),
        ("任务/next.txt", "下一步任务\n补完第二章", "task"),
        ("资料/故事大纲.md", "# 总体大纲", "outline"),
        ("世界观/王国.md", "# 地理", "world"),
        ("人物/林舟.md", "# 角色小传", "character"),
        ("文风/style.md", "# 语言风格", "style"),
        ("杂项/备忘.md", "今天下雨", "other"),
        ("人物大纲.md", "角色与情节", "other"),
    ],
)
def test_classification_is_deterministic(path: str, text: str, category: str) -> None:
    assert classify_document(path, text) == category
    assert classify_document(path, text) == category


def test_classification_scans_bounded_markdown_headings_after_intro() -> None:
    assert classify_document("资料.md", "intro\n# 世界观\n群岛规则") == "world"


def test_classification_falls_back_when_markdown_headings_conflict() -> None:
    assert classify_document("资料.md", "# 角色\n林舟\n# 世界观\n群岛") == "other"


def test_classification_does_not_treat_plain_body_lines_as_titles() -> None:
    assert classify_document("misc.md", "任务\n普通正文") == "other"


def test_gb18030_task_has_correct_category_and_preview(tmp_path: Path) -> None:
    store = ImportDraftStore(tmp_path / "imports")

    draft = store.create_from_files(
        "旧稿",
        [("资料/下一步任务.txt", "补写港口相遇场景。".encode("gb18030"))],
    )

    assert draft.files[0].category == "task"
    assert draft.files[0].encoding == "gb18030"
    assert draft.files[0].content_preview == "补写港口相遇场景。"


def test_markdown_and_plain_text_chapter_splitting_preserves_offsets() -> None:
    markdown = "# 第一章 风起\n甲。\n\n## 第二章 雨至\n乙。\n"
    md_segments = split_chapters("正文/book.md", markdown)
    plain = "序言\n\n第一卷 山海\n卷首。\n第1章 归乡\n正文。"
    txt_segments = split_chapters("正文/book.txt", plain)

    assert [(item.title, item.start, item.end) for item in md_segments] == [
        ("第一章 风起", 0, markdown.index("## 第二章")),
        ("第二章 雨至", markdown.index("## 第二章"), len(markdown)),
    ]
    assert all(markdown[item.start : item.end] == item.content for item in md_segments)
    assert [item.source_path for item in md_segments] == ["正文/book.md", "正文/book.md"]
    assert txt_segments[0].title == "待命名章节"
    assert txt_segments[0].content == "序言\n\n"
    assert [item.title for item in txt_segments[1:]] == ["第一卷 山海", "第1章 归乡"]
    assert "卷首。" in txt_segments[1].content
    assert "正文。" in txt_segments[2].content


def test_body_without_chapter_heading_is_one_untitled_segment() -> None:
    text = "夜里下起了雨。\n他推门而入。"

    segments = split_chapters("正文.txt", text)

    assert len(segments) == 1
    assert segments[0].title == "待命名章节"
    assert segments[0].start == 0
    assert segments[0].end == len(text)
    assert segments[0].content == text


def test_split_chapters_stops_scanning_before_constructing_over_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_segment = import_drafts_module.ChapterSegment
    constructed = 0

    def counted_segment(*args: object, **kwargs: object):
        nonlocal constructed
        constructed += 1
        return original_segment(*args, **kwargs)

    monkeypatch.setattr(import_drafts_module, "ChapterSegment", counted_segment)
    text = "".join(f"# 第{index}章\n" for index in range(10_001))

    with pytest.raises(HTTPException) as exc:
        split_chapters("正文.md", text, max_segments=10_000)

    assert error_code(exc) == "IMPORT_CHAPTER_LIMIT"
    assert constructed <= 10_000


@pytest.mark.parametrize(
    "path",
    [
        "/absolute.md",
        "../escape.md",
        "a/../escape.md",
        "C:/book.md",
        "safe/file.txt:stream",
        "safe/file?.txt",
        "safe/trailing-dot.",
        "safe/trailing-space ",
        "safe/CON.txt",
        "safe/com9",
        "safe/LPT1.log",
        "a\x00b.md",
        "a//b.md",
    ],
)
def test_relative_paths_reject_unsafe_forms(path: str) -> None:
    with pytest.raises(HTTPException) as exc:
        normalize_relative_path(path)

    assert error_code(exc) == "UNSAFE_IMPORT_PATH"


def test_relative_paths_normalize_separators_and_duplicate_case(tmp_path: Path) -> None:
    assert normalize_relative_path("正文\\第一章.md") == "正文/第一章.md"
    store = ImportDraftStore(tmp_path / "imports")

    with pytest.raises(HTTPException) as exc:
        store.create_from_files("book", [("Book.md", b"one"), ("book.MD", b"two")])

    assert error_code(exc) == "UNSAFE_IMPORT_PATH"


@pytest.mark.parametrize("path", ["foo.", "trail ", "CON", "NUL.txt", "bad<name>.md"])
def test_portable_windows_paths_fail_before_disk_write(tmp_path: Path, path: str) -> None:
    root = tmp_path / "imports"

    with pytest.raises(HTTPException) as exc:
        ImportDraftStore(root).create_from_files("book", [(path, b"text")])

    assert error_code(exc) == "UNSAFE_IMPORT_PATH"
    assert list(root.iterdir()) == []


@pytest.mark.parametrize(
    "path",
    [
        "CONIN$",
        "CONOUT$.txt",
        "CON .txt",
        "COM¹.log",
        "LPT³",
        f"{'a' * 256}.txt",
        "/".join("界" * 80 for _ in range(18)),
    ],
)
def test_extended_portable_paths_fail_via_public_create(tmp_path: Path, path: str) -> None:
    root = tmp_path / "imports"

    with pytest.raises(HTTPException) as exc:
        ImportDraftStore(root).create_from_files("book", [(path, b"text")])

    assert error_code(exc) == "UNSAFE_IMPORT_PATH"
    assert list(root.iterdir()) == []


def test_manifest_partially_overrides_detection_and_is_not_a_source(tmp_path: Path) -> None:
    manifest = {
        "version": 1,
        "project": {"objective": "续写至渡海"},
        "files": [
            {"path": "资料/人物.md", "category": "world", "title": "群岛规则"},
        ],
        "chapters": [
            {"path": "正文.md", "source_index": 0, "title": "重命名的第一章"},
        ],
        "continuation": {"objective": "从雨夜继续"},
    }
    store = ImportDraftStore(tmp_path / "imports")

    draft = store.create_from_files(
        "book",
        [
            ("novel-import.json", json.dumps(manifest, ensure_ascii=False).encode()),
            ("资料/人物.md", "# 林舟\n人物简介".encode()),
            ("正文.md", "# 第一章\n开端\n# 第二章\n发展".encode()),
        ],
    )

    assert [item.relative_path for item in draft.files] == ["正文.md", "资料/人物.md"]
    overridden = next(item for item in draft.files if item.relative_path == "资料/人物.md")
    assert overridden.category == "world"
    assert overridden.title == "群岛规则"
    assert draft.chapters[0].title == "重命名的第一章"
    assert draft.chapters[1].title == "第二章"
    assert draft.objective == "续写至渡海"
    assert draft.continuation.objective == "从雨夜继续"


@pytest.mark.parametrize(
    ("manifest", "code"),
    [
        ({"version": 2}, "IMPORT_MANIFEST_VERSION"),
        (
            {"version": 1, "files": [{"path": "missing.md", "category": "world"}]},
            "IMPORT_MANIFEST_UNKNOWN_PATH",
        ),
        (
            {
                "version": 1,
                "chapters": [
                    {"path": "book.md", "source_index": 0, "title": "A"},
                    {"path": "book.md", "source_index": 0, "title": "B"},
                ],
            },
            "IMPORT_MANIFEST_DUPLICATE_CHAPTER",
        ),
        ({"version": 1, "surprise": True}, "IMPORT_MANIFEST_INVALID"),
    ],
)
def test_manifest_reports_clear_errors(
    tmp_path: Path, manifest: dict[str, object], code: str
) -> None:
    store = ImportDraftStore(tmp_path / "imports")

    with pytest.raises(HTTPException) as exc:
        store.create_from_files(
            "book",
            [
                ("novel-import.json", json.dumps(manifest).encode()),
                ("book.md", b"# Chapter one\nText"),
            ],
        )

    assert error_code(exc) == code


def test_draft_metadata_and_raw_files_survive_new_store_instance(tmp_path: Path) -> None:
    root = tmp_path / "imports"
    created = ImportDraftStore(root).create_from_files(
        "book", [("正文/第一章.md", "# 第一章\n雪落。".encode())]
    )

    restored = ImportDraftStore(root).get(created.draft_id)

    assert restored.model_dump(mode="json") == created.model_dump(mode="json")
    assert restored.revision == 1
    assert restored.created_at == restored.updated_at
    assert (root / created.draft_id / "draft.json").is_file()
    assert not (root / created.draft_id / "draft.json.tmp").exists()
    assert (root / created.draft_id / "raw" / "正文" / "第一章.md").read_bytes() == (
        "# 第一章\n雪落。".encode()
    )


def test_atomic_update_does_not_replace_old_metadata_when_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "imports"
    store = ImportDraftStore(root)
    draft = store.create_from_files("book", [("book.txt", b"body")])
    before = (root / draft.draft_id / "draft.json").read_bytes()

    temporary_names: list[str] = []

    def fail_replace(source: Path, destination: Path) -> None:
        temporary_names.append(source.name)
        raise OSError("disk error")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="disk error"):
        store.update(draft.draft_id, {"revision": 1, "objective": "new"})

    assert (root / draft.draft_id / "draft.json").read_bytes() == before
    assert len(temporary_names) == 1
    assert temporary_names[0].startswith(".draft.json.")
    assert list((root / draft.draft_id).glob("*.tmp")) == []


def test_metadata_replace_invokes_directory_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Path] = []
    monkeypatch.setattr(
        import_drafts_module,
        "_fsync_directory",
        lambda path: calls.append(path),
        raising=False,
    )

    draft = ImportDraftStore(tmp_path / "imports").create_from_files(
        "book", [("book.txt", b"body")]
    )

    assert tmp_path / "imports" / draft.draft_id / "raw" in calls
    assert tmp_path / "imports" / draft.draft_id in calls


def test_nested_raw_directory_ancestors_receive_best_effort_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Path] = []
    monkeypatch.setattr(
        import_drafts_module,
        "_fsync_directory",
        lambda path: calls.append(path),
    )

    draft = ImportDraftStore(tmp_path / "imports").create_from_files(
        "book", [("a/b/book.txt", b"body")]
    )
    raw = tmp_path / "imports" / draft.draft_id / "raw"

    assert raw / "a" / "b" in calls
    assert raw / "a" in calls
    assert raw in calls


def test_update_preserves_immutable_file_metadata(tmp_path: Path) -> None:
    store = ImportDraftStore(tmp_path / "imports")
    draft = store.create_from_files("book", [("notes.txt", "原文".encode())])
    original = draft.files[0]
    forged = original.model_dump(exclude={"content_preview"})
    forged.update(
        {
            "category": "task",
            "title": "新标题",
            "selected": False,
            "encoding": "forged",
            "size_bytes": 999,
            "byte_hash": "0" * 64,
            "content_hash": "1" * 64,
            "warning": "forged",
        }
    )

    updated = store.update(
        draft.draft_id, {"revision": draft.revision, "files": [forged]}
    )
    item = updated.files[0]

    assert (item.category, item.title, item.selected) == ("task", "新标题", False)
    for field in (
        "relative_path",
        "encoding",
        "size_bytes",
        "byte_hash",
        "content_hash",
        "warning",
        "content_preview",
    ):
        assert getattr(item, field) == getattr(original, field)


def test_update_rejects_unknown_and_duplicate_files(tmp_path: Path) -> None:
    store = ImportDraftStore(tmp_path / "imports")
    draft = store.create_from_files("book", [("notes.txt", b"body")])
    base = draft.files[0].model_dump(exclude={"content_preview"})
    unknown = dict(base, relative_path="unknown.txt")

    with pytest.raises(HTTPException) as unknown_exc:
        store.update(draft.draft_id, {"revision": 1, "files": [unknown]})
    assert error_code(unknown_exc) == "IMPORT_FILE_UNKNOWN"

    duplicate = dict(base, relative_path="NOTES.TXT")
    with pytest.raises(HTTPException) as duplicate_exc:
        store.update(draft.draft_id, {"revision": 1, "files": [base, duplicate]})
    assert error_code(duplicate_exc) == "IMPORT_FILE_DUPLICATE"

    with pytest.raises(HTTPException) as missing_exc:
        store.update(draft.draft_id, {"revision": 1, "files": []})
    assert error_code(missing_exc) == "IMPORT_FILE_MISSING"


def test_file_selection_changes_filter_and_renumber_chapters(tmp_path: Path) -> None:
    store = ImportDraftStore(tmp_path / "imports")
    draft = store.create_from_files(
        "book",
        [
            ("正文/one.md", "# 第一章\n一".encode()),
            ("正文/two.md", "# 第二章\n二".encode()),
        ],
    )
    patched_files = []
    for item in draft.files:
        values = item.model_dump(exclude={"content_preview"})
        if item.relative_path.endswith("one.md"):
            values["selected"] = False
        patched_files.append(values)
    patched_chapters = []
    for index, chapter in enumerate(reversed(draft.chapters), start=3):
        values = chapter.model_dump(exclude={"source_path", "start", "end"})
        values["order_index"] = index
        patched_chapters.append(values)

    updated = store.update(
        draft.draft_id,
        {
            "revision": 1,
            "files": patched_files,
            "chapters": patched_chapters,
        },
    )

    assert len(updated.chapters) == 1
    assert updated.chapters[0].relative_path == "正文/two.md"
    assert updated.chapters[0].order_index == 0


def test_disable_restart_and_reenable_restores_edited_chapter_inventory(
    tmp_path: Path,
) -> None:
    root = tmp_path / "imports"
    store = ImportDraftStore(root)
    draft = store.create_from_files(
        "book", [("正文.md", "# 第一章\n一\n# 第二章\n二".encode())]
    )
    original_immutable = [
        (item.source_document_id, item.source_path, item.start, item.end)
        for item in draft.chapters
    ]
    files = [item.model_dump(exclude={"content_preview"}) for item in draft.files]
    files[0]["selected"] = False
    chapters = []
    for item in draft.chapters:
        values = item.model_dump(exclude={"source_path", "start", "end"})
        values["title"] = f"编辑-{item.title}"
        chapters.append(values)

    disabled = store.update(
        draft.draft_id,
        {"revision": 1, "files": files, "chapters": chapters},
    )
    assert disabled.chapters == []
    assert [item.title for item in disabled.chapter_inventory] == [
        "编辑-第一章",
        "编辑-第二章",
    ]

    restarted = ImportDraftStore(root)
    persisted = restarted.get(draft.draft_id)
    enable_files = [
        item.model_dump(exclude={"content_preview"}) for item in persisted.files
    ]
    enable_files[0]["selected"] = True
    enabled = restarted.update(
        draft.draft_id,
        {"revision": persisted.revision, "files": enable_files},
    )

    assert [item.title for item in enabled.chapters] == [
        "编辑-第一章",
        "编辑-第二章",
    ]
    assert [
        (item.source_document_id, item.source_path, item.start, item.end)
        for item in enabled.chapters
    ] == original_immutable
    assert [item.order_index for item in enabled.chapters] == [0, 1]


def test_other_file_becoming_manuscript_is_split_from_immutable_raw(tmp_path: Path) -> None:
    root = tmp_path / "imports"
    store = ImportDraftStore(root)
    draft = store.create_from_files(
        "book", [("misc.md", "# Opening\n一\n# Ending\n二".encode())]
    )
    assert draft.files[0].category == "other"
    assert draft.chapter_inventory == []
    files = [draft.files[0].model_dump(exclude={"content_preview"})]
    files[0]["category"] = "manuscript"

    updated = store.update(draft.draft_id, {"revision": 1, "files": files})
    restored = ImportDraftStore(root).get(draft.draft_id)

    assert [item.title for item in updated.chapters] == ["Opening", "Ending"]
    assert restored.chapter_inventory == updated.chapter_inventory
    assert restored.chapters == updated.chapters


def test_client_cannot_patch_internal_chapter_inventory(tmp_path: Path) -> None:
    store = ImportDraftStore(tmp_path / "imports")
    draft = store.create_from_files("book", [("正文.md", "# 第一章\n一".encode())])

    with pytest.raises(HTTPException) as exc:
        store.update(
            draft.draft_id,
            {
                "revision": 1,
                "chapter_inventory": [
                    item.model_dump() for item in draft.chapter_inventory
                ],
            },
        )

    assert error_code(exc) == "IMPORT_DRAFT_PATCH_INVALID"


def test_chapter_patch_rejects_duplicate_order_and_unknown_source(tmp_path: Path) -> None:
    store = ImportDraftStore(tmp_path / "imports")
    draft = store.create_from_files(
        "book", [("正文.md", "# 第一章\n一\n# 第二章\n二".encode())]
    )
    duplicate_order = []
    for chapter in draft.chapters:
        values = chapter.model_dump(exclude={"source_path", "start", "end"})
        values["order_index"] = 0
        duplicate_order.append(values)

    with pytest.raises(HTTPException) as duplicate_exc:
        store.update(
            draft.draft_id, {"revision": 1, "chapters": duplicate_order}
        )
    assert error_code(duplicate_exc) == "IMPORT_CHAPTER_ORDER_INVALID"

    unknown = draft.chapters[0].model_dump(exclude={"source_path", "start", "end"})
    unknown["relative_path"] = "missing.md"
    with pytest.raises(HTTPException) as unknown_exc:
        store.update(draft.draft_id, {"revision": 1, "chapters": [unknown]})
    assert error_code(unknown_exc) == "IMPORT_CHAPTER_UNKNOWN"


def test_partial_chapter_patch_rejects_inventory_order_collision_without_writing(
    tmp_path: Path,
) -> None:
    store = ImportDraftStore(tmp_path / "imports")
    draft = store.create_from_files(
        "book", [("正文.md", "# 第一章\n一\n# 第二章\n二".encode())]
    )
    second = draft.chapters[1].model_dump(exclude={"source_path", "start", "end"})
    second["order_index"] = 0

    with pytest.raises(HTTPException) as exc:
        store.update(draft.draft_id, {"revision": draft.revision, "chapters": [second]})

    assert error_code(exc) == "IMPORT_CHAPTER_ORDER_INVALID"
    assert store.get(draft.draft_id) == draft


def test_partial_chapter_patch_normalizes_legal_inventory_order_gap(tmp_path: Path) -> None:
    store = ImportDraftStore(tmp_path / "imports")
    draft = store.create_from_files(
        "book", [("正文.md", "# 第一章\n一\n# 第二章\n二".encode())]
    )
    second = draft.chapters[1].model_dump(exclude={"source_path", "start", "end"})
    second["order_index"] = 4

    updated = store.update(
        draft.draft_id, {"revision": draft.revision, "chapters": [second]}
    )

    assert [item.order_index for item in updated.chapter_inventory] == [0, 1]
    assert updated.chapters == updated.chapter_inventory


def test_concurrent_revision_updates_are_serialized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "imports"
    original_get_unlocked = ImportDraftStore._get_unlocked

    def delayed_get_unlocked(self: ImportDraftStore, draft_id: str):
        record = original_get_unlocked(self, draft_id)
        time.sleep(0.005)
        return record

    monkeypatch.setattr(ImportDraftStore, "_get_unlocked", delayed_get_unlocked)

    def update(draft_id: str, objective: str) -> str:
        try:
            ImportDraftStore(root).update(
                draft_id, {"revision": 1, "objective": objective}
            )
        except HTTPException as exc:
            assert isinstance(exc.detail, dict)
            return str(exc.detail["code"])
        except BaseException as exc:  # pragma: no cover - assertion diagnostic
            return type(exc).__name__
        return "ok"

    for index in range(20):
        draft = ImportDraftStore(root).create_from_files(
            str(index), [(f"book-{index}.txt", b"body")]
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(update, draft.draft_id, objective)
                for objective in ("one", "two")
            ]
            results = [future.result() for future in futures]
        assert sorted(results) == ["DRAFT_REVISION_CONFLICT", "ok"]
        restored = original_get_unlocked(ImportDraftStore(root), draft.draft_id)
        assert restored.revision == 2
        assert restored.objective in {"one", "two"}


def test_draft_lock_blocks_updates_from_another_process(tmp_path: Path) -> None:
    root = tmp_path / "imports"
    store = ImportDraftStore(root)
    draft = store.create_from_files("book", [("book.txt", b"body")])
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    process = context.Process(
        target=hold_import_draft_lock,
        args=(str(root), draft.draft_id, ready, release),
    )
    process.start()
    try:
        assert ready.wait(10)
        with pytest.raises(HTTPException) as exc:
            store.update(draft.draft_id, {"revision": 1, "objective": "blocked"})
        assert error_code(exc) == "DRAFT_BUSY"
    finally:
        release.set()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(5)
    assert process.exitcode == 0


def test_update_and_discard_share_one_thread_lifecycle_lock(tmp_path: Path) -> None:
    root = tmp_path / "imports"
    draft = ImportDraftStore(root).create_from_files("book", [("book.txt", b"body")])

    def mutate(action: str) -> str:
        store = ImportDraftStore(root)
        try:
            if action == "update":
                store.update(draft.draft_id, {"revision": 1, "objective": "thread"})
            else:
                store.discard(draft.draft_id)
        except HTTPException as exc:
            assert isinstance(exc.detail, dict)
            return str(exc.detail["code"])
        except BaseException as exc:  # pragma: no cover - assertion diagnostic
            return type(exc).__name__
        return "ok"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(mutate, ["update", "discard"]))

    assert all(result in {"ok", "IMPORT_DRAFT_NOT_FOUND", "DRAFT_BUSY"} for result in results)
    assert "ok" in results


def test_update_and_discard_share_one_cross_process_lifecycle_lock(tmp_path: Path) -> None:
    root = tmp_path / "imports"
    draft = ImportDraftStore(root).create_from_files("book", [("book.txt", b"body")])
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=mutate_import_draft_in_process,
            args=(str(root), draft.draft_id, action, start, results),
        )
        for action in ("update", "discard")
    ]
    for process in processes:
        process.start()
    start.set()
    outcomes = [results.get(timeout=15) for _ in processes]
    for process in processes:
        process.join(15)
        if process.is_alive():
            process.terminate()
            process.join(5)

    assert all(
        outcome in {"ok", "IMPORT_DRAFT_NOT_FOUND", "DRAFT_BUSY"}
        for outcome in outcomes
    )
    assert "ok" in outcomes
    assert all(process.exitcode == 0 for process in processes)


def test_process_lock_registry_does_not_retain_completed_drafts(tmp_path: Path) -> None:
    root = tmp_path / "imports"
    store = ImportDraftStore(root)
    drafts = [
        store.create_from_files(str(index), [(f"book-{index}.txt", b"body")])
        for index in range(20)
    ]

    for draft in drafts:
        store.get(draft.draft_id)
    gc.collect()

    assert len(import_drafts_module._PROCESS_LOCKS) == 0


def test_chapter_patch_preserves_discovered_offsets_across_restart(tmp_path: Path) -> None:
    root = tmp_path / "imports"
    store = ImportDraftStore(root)
    draft = store.create_from_files(
        "book", [("正文.md", "# 第一章\n开端\n# 第二章\n发展".encode())]
    )
    patched_chapters = [
        chapter.model_copy(update={"title": f"修订-{chapter.title}"})
        for chapter in draft.chapters
    ]

    updated = store.update(
        draft.draft_id,
        {
            "revision": draft.revision,
            "chapters": [
                {
                    key: value
                    for key, value in chapter.model_dump().items()
                    if key not in {"source_path", "start", "end"}
                }
                for chapter in patched_chapters
            ],
        },
    )
    restored = ImportDraftStore(root).get(draft.draft_id)

    assert [item.title for item in restored.chapters] == ["修订-第一章", "修订-第二章"]
    assert [(item.start, item.end) for item in restored.chapters] == [
        (item.start, item.end) for item in draft.chapters
    ]
    assert restored.model_dump(mode="json") == updated.model_dump(mode="json")


@pytest.mark.parametrize(
    ("name", "mode", "code"),
    [
        ("../escape.md", stat.S_IFREG | 0o644, "UNSAFE_ARCHIVE_PATH"),
        ("/absolute.md", stat.S_IFREG | 0o644, "UNSAFE_ARCHIVE_PATH"),
        ("C:/escape.md", stat.S_IFREG | 0o644, "UNSAFE_ARCHIVE_PATH"),
        ("link.md", stat.S_IFLNK | 0o777, "ARCHIVE_LINK_FORBIDDEN"),
    ],
)
def test_zip_rejects_unsafe_paths_and_links(
    tmp_path: Path, name: str, mode: int, code: str
) -> None:
    raw_zip = make_zip([(name, b"content", mode)])

    with pytest.raises(HTTPException) as exc:
        ImportDraftStore(tmp_path / "imports").create_from_zip("book", raw_zip)

    assert error_code(exc) == code
    assert list((tmp_path / "imports").glob("*")) == []


def test_zip_rejects_duplicate_normalized_paths(tmp_path: Path) -> None:
    raw_zip = make_zip(
        [
            ("folder\\Book.md", b"one", stat.S_IFREG | 0o644),
            ("folder/book.MD", b"two", stat.S_IFREG | 0o644),
        ]
    )

    with pytest.raises(HTTPException) as exc:
        ImportDraftStore(tmp_path / "imports").create_from_zip("book", raw_zip)

    assert error_code(exc) == "UNSAFE_ARCHIVE_PATH"


@pytest.mark.parametrize(
    "mode",
    [stat.S_IFIFO, stat.S_IFSOCK, stat.S_IFCHR, stat.S_IFBLK],
)
def test_zip_rejects_unix_non_regular_entries(tmp_path: Path, mode: int) -> None:
    raw_zip = make_zip([("special-entry", b"data", mode | 0o644)])

    with pytest.raises(HTTPException) as exc:
        ImportDraftStore(tmp_path / "imports").create_from_zip("book", raw_zip)

    assert error_code(exc) == "ARCHIVE_LINK_FORBIDDEN"


def test_zip_counts_directory_entries_toward_file_limit(tmp_path: Path) -> None:
    raw_zip = make_zip(
        [
            ("one/", b"", stat.S_IFDIR | 0o755),
            ("two/", b"", stat.S_IFDIR | 0o755),
        ]
    )

    with pytest.raises(HTTPException) as exc:
        ImportDraftStore(
            tmp_path / "imports",
            limits=ImportLimits(
                max_files=1,
                max_file_bytes=1024,
                max_total_bytes=2048,
                max_compression_ratio=100,
            ),
        ).create_from_zip("book", raw_zip)

    assert error_code(exc) == "IMPORT_FILE_LIMIT"


@pytest.mark.parametrize(
    ("name", "content", "mode"),
    [
        ("named-directory", b"", stat.S_IFDIR | 0o755),
        ("regular-with-slash/", b"", stat.S_IFREG | 0o644),
        ("nonempty-directory/", b"x", stat.S_IFDIR | 0o755),
    ],
)
def test_zip_rejects_directory_type_mismatches(
    tmp_path: Path, name: str, content: bytes, mode: int
) -> None:
    raw_zip = make_zip([(name, content, mode)])

    with pytest.raises(HTTPException) as exc:
        ImportDraftStore(tmp_path / "imports").create_from_zip("book", raw_zip)

    assert error_code(exc) == "IMPORT_ARCHIVE_INVALID"


def test_zip_rejects_encrypted_flag_during_precheck(tmp_path: Path) -> None:
    raw_zip = bytearray(make_zip([("book.txt", b"body", None)]))
    central_offset = raw_zip.find(b"PK\x01\x02")
    assert central_offset >= 0
    flag_bits = struct.unpack_from("<H", raw_zip, central_offset + 8)[0]
    struct.pack_into("<H", raw_zip, central_offset + 8, flag_bits | 1)
    root = tmp_path / "imports"

    with pytest.raises(HTTPException) as exc:
        ImportDraftStore(root).create_from_zip("book", bytes(raw_zip))

    assert error_code(exc) == "IMPORT_ARCHIVE_INVALID"
    assert list(root.iterdir()) == []


def test_default_zip_file_count_limit_rejects_1001_entries(tmp_path: Path) -> None:
    raw_zip = make_zip(
        [(f"chapter-{index}.txt", b"", None) for index in range(1001)]
    )

    with pytest.raises(HTTPException) as exc:
        ImportDraftStore(tmp_path / "imports").create_from_zip("book", raw_zip)

    assert error_code(exc) == "IMPORT_FILE_LIMIT"


def test_default_zip_single_file_limit_rejects_11_mib(tmp_path: Path) -> None:
    raw_zip = make_zip([("large.txt", b"x" * (11 * 1024 * 1024), None)])

    with pytest.raises(HTTPException) as exc:
        ImportDraftStore(tmp_path / "imports").create_from_zip("book", raw_zip)

    assert error_code(exc) == "IMPORT_FILE_TOO_LARGE"


def test_default_zip_compression_ratio_limit_rejects_over_100_to_1(
    tmp_path: Path,
) -> None:
    raw_zip = make_zip([("compressed.txt", b"0" * (256 * 1024), None)])

    with pytest.raises(HTTPException) as exc:
        ImportDraftStore(tmp_path / "imports").create_from_zip("book", raw_zip)

    assert error_code(exc) == "IMPORT_COMPRESSION_RATIO"


def test_default_zip_total_limit_rejects_declared_201_mib_via_public_entrypoint(
    tmp_path: Path,
) -> None:
    raw_zip = bytearray(
        make_zip([(f"part-{index}.txt", b"x", None) for index in range(21)])
    )
    central_signature = b"PK\x01\x02"
    cursor = 0
    for index in range(21):
        central_offset = raw_zip.find(central_signature, cursor)
        assert central_offset >= 0
        declared_size = 10 * 1024 * 1024 if index < 20 else 1024 * 1024
        struct.pack_into("<I", raw_zip, central_offset + 20, declared_size)
        struct.pack_into("<I", raw_zip, central_offset + 24, declared_size)
        filename_length, extra_length, comment_length = struct.unpack_from(
            "<HHH", raw_zip, central_offset + 28
        )
        cursor = (
            central_offset
            + 46
            + filename_length
            + extra_length
            + comment_length
        )
    root = tmp_path / "imports"

    with pytest.raises(HTTPException) as exc:
        ImportDraftStore(root).create_from_zip("book", bytes(raw_zip))

    assert error_code(exc) == "IMPORT_TOTAL_TOO_LARGE"
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("chapter_count", [10_000, 10_001])
def test_import_chapter_limit_is_enforced_before_chapter_models(
    tmp_path: Path, chapter_count: int
) -> None:
    text = "".join(f"# 第{index}章\n" for index in range(chapter_count))
    root = tmp_path / "imports"
    store = ImportDraftStore(root)

    if chapter_count == 10_000:
        draft = store.create_from_files("book", [("正文.md", text.encode())])
        assert len(draft.chapters) == 10_000
    else:
        with pytest.raises(HTTPException) as exc:
            store.create_from_files("book", [("正文.md", text.encode())])
        assert error_code(exc) == "IMPORT_CHAPTER_LIMIT"
        assert list(root.iterdir()) == []


def test_import_chapter_limit_uses_remaining_capacity_across_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_segment = import_drafts_module.ChapterSegment
    constructed = 0

    def counted_segment(*args: object, **kwargs: object):
        nonlocal constructed
        constructed += 1
        return original_segment(*args, **kwargs)

    monkeypatch.setattr(import_drafts_module, "ChapterSegment", counted_segment)
    first = "".join(f"# 第{index}章\n" for index in range(6_000))
    second = "".join(f"# 第{index}章\n" for index in range(4_001))

    with pytest.raises(HTTPException) as exc:
        ImportDraftStore(tmp_path / "imports").create_from_files(
            "book", [("正文/one.md", first.encode()), ("正文/two.md", second.encode())]
        )

    assert error_code(exc) == "IMPORT_CHAPTER_LIMIT"
    assert constructed <= 10_000


@pytest.mark.parametrize(
    ("limits", "entries", "code"),
    [
        (
            ImportLimits(
                max_files=1,
                max_file_bytes=1024,
                max_total_bytes=2048,
                max_compression_ratio=100,
            ),
            [("one.md", b"1", None), ("two.md", b"2", None)],
            "IMPORT_FILE_LIMIT",
        ),
        (
            ImportLimits(
                max_files=2,
                max_file_bytes=3,
                max_total_bytes=2048,
                max_compression_ratio=100,
            ),
            [("one.md", b"1234", None)],
            "IMPORT_FILE_TOO_LARGE",
        ),
        (
            ImportLimits(
                max_files=2,
                max_file_bytes=5,
                max_total_bytes=5,
                max_compression_ratio=100,
            ),
            [("one.md", b"123", None), ("two.md", b"456", None)],
            "IMPORT_TOTAL_TOO_LARGE",
        ),
        (
            ImportLimits(
                max_files=2,
                max_file_bytes=4096,
                max_total_bytes=4096,
                max_compression_ratio=2,
            ),
            [("one.md", b"0" * 1024, None)],
            "IMPORT_COMPRESSION_RATIO",
        ),
    ],
)
def test_zip_limits_are_checked_before_writing(
    tmp_path: Path,
    limits: ImportLimits,
    entries: list[tuple[str, bytes, int | None]],
    code: str,
) -> None:
    raw_zip = make_zip(entries)

    with pytest.raises(HTTPException) as exc:
        ImportDraftStore(tmp_path / "imports", limits=limits).create_from_zip("book", raw_zip)

    assert error_code(exc) == code
    assert list((tmp_path / "imports").glob("*")) == []


def test_settings_have_strict_import_defaults_and_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings()
    assert settings.import_max_files == DEFAULT_IMPORT_MAX_FILES == 1000
    assert settings.import_max_file_bytes == DEFAULT_IMPORT_MAX_FILE_BYTES == 10 * 1024 * 1024
    assert settings.import_max_total_bytes == DEFAULT_IMPORT_MAX_TOTAL_BYTES == 200 * 1024 * 1024
    assert settings.import_max_compression_ratio == DEFAULT_IMPORT_MAX_COMPRESSION_RATIO == 100

    monkeypatch.setenv("NOVEL_IMPORT_MAX_FILES", "12")
    monkeypatch.setenv("NOVEL_IMPORT_MAX_FILE_BYTES", "2048")
    monkeypatch.setenv("NOVEL_IMPORT_MAX_TOTAL_BYTES", "4096")
    monkeypatch.setenv("NOVEL_IMPORT_MAX_COMPRESSION_RATIO", "8")
    configured = Settings()
    assert ImportLimits.from_settings(configured) == ImportLimits(12, 2048, 4096, 8)

    with pytest.raises(ValidationError):
        Settings(import_max_files=True)
    with pytest.raises(ValidationError):
        Settings(import_max_file_bytes=4096, import_max_total_bytes=2048)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("import_max_files", 1001),
        ("import_max_file_bytes", 10 * 1024 * 1024 + 1),
        ("import_max_total_bytes", 200 * 1024 * 1024 + 1),
        ("import_max_compression_ratio", 101),
    ],
)
def test_settings_reject_import_limits_above_local_defaults(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        Settings(**{field: value})


@pytest.mark.parametrize(
    ("environment", "value"),
    [
        ("NOVEL_IMPORT_MAX_FILES", "1001"),
        ("NOVEL_IMPORT_MAX_FILE_BYTES", str(10 * 1024 * 1024 + 1)),
        ("NOVEL_IMPORT_MAX_TOTAL_BYTES", str(200 * 1024 * 1024 + 1)),
        ("NOVEL_IMPORT_MAX_COMPRESSION_RATIO", "101"),
    ],
)
def test_environment_cannot_raise_import_limits_above_defaults(
    monkeypatch: pytest.MonkeyPatch, environment: str, value: str
) -> None:
    monkeypatch.setenv(environment, value)
    with pytest.raises(ValidationError):
        Settings()


def test_import_limits_api_returns_the_store_effective_limits(client) -> None:
    client.app.state.import_drafts = ImportDraftStore(
        client.app.state.settings.data_dir / "lower-limit-imports",
        ImportLimits(12, 2048, 4096, 8),
    )

    response = client.get("/api/v1/imports/limits")

    assert response.status_code == 200
    assert response.json() == {
        "max_files": 12,
        "max_file_bytes": 2048,
        "max_total_bytes": 4096,
        "max_compression_ratio": 8,
    }


@pytest.mark.parametrize("value", [1.5, 8.5, True, "8"])
def test_compression_ratio_requires_a_strict_integer(value: object) -> None:
    with pytest.raises((TypeError, ValueError, ValidationError)):
        ImportLimits(max_compression_ratio=value)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Settings(import_max_compression_ratio=value)


@pytest.mark.parametrize(
    "field",
    ["max_files", "max_file_bytes", "max_total_bytes", "max_compression_ratio"],
)
@pytest.mark.parametrize("value", [True, 1.5, "2"])
def test_import_limits_require_strict_integer_fields(field: str, value: object) -> None:
    with pytest.raises(TypeError):
        ImportLimits(**{field: value})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "manifest",
    [
        {"version": 1, "project": {"objective": 12}},
        {"version": 1, "project": {"objective": "ok", "unknown": True}},
        {"version": 1, "files": [{"path": "book.md", "title": 12}]},
        {"version": 1, "files": {"book.md": "wrong"}},
        {"version": 1, "chapters": [{"path": "book.md", "order_index": "0"}]},
        {"version": 1, "continuation": {"objective": 12}},
    ],
)
def test_manifest_nested_types_are_strict_and_leave_no_draft(
    tmp_path: Path, manifest: dict[str, object]
) -> None:
    root = tmp_path / "imports"

    with pytest.raises(HTTPException) as exc:
        ImportDraftStore(root).create_from_files(
            "book",
            [
                ("novel-import.json", json.dumps(manifest).encode()),
                ("book.md", b"# Chapter one\nBody"),
            ],
        )

    assert error_code(exc) == "IMPORT_MANIFEST_INVALID"
    assert list(root.iterdir()) == []


@pytest.mark.parametrize(
    "manifest",
    [
        {
            "version": 1,
            "files": [
                {"path": "book.md", "relative_path": "other.md", "title": "Book"}
            ],
        },
        {
            "version": 1,
            "chapters": [
                {
                    "path": "book.md",
                    "source_path": "other.md",
                    "source_index": 0,
                }
            ],
        },
        {
            "version": 1,
            "chapters": [
                {"path": "book.md", "source_index": 0, "chapter_index": 1}
            ],
        },
    ],
)
def test_manifest_rejects_conflicting_alias_values(
    tmp_path: Path, manifest: dict[str, object]
) -> None:
    root = tmp_path / "imports"

    with pytest.raises(HTTPException) as exc:
        ImportDraftStore(root).create_from_files(
            "book",
            [
                ("novel-import.json", json.dumps(manifest).encode()),
                ("book.md", b"# Chapter one\nBody"),
                ("other.md", b"# Chapter other\nBody"),
            ],
        )

    assert error_code(exc) == "IMPORT_MANIFEST_INVALID"
    assert list(root.iterdir()) == []


def test_discard_only_accepts_a_canonical_uuid(tmp_path: Path) -> None:
    store = ImportDraftStore(tmp_path / "imports")
    draft = store.create_from_files("book", [("book.txt", b"body")])

    with pytest.raises(HTTPException):
        store.discard("../outside")
    assert store.get(draft.draft_id).draft_id == draft.draft_id

    store.discard(draft.draft_id)
    assert not (tmp_path / "imports" / draft.draft_id).exists()


def test_store_initialization_only_cleans_verified_discard_tombstones(
    tmp_path: Path,
) -> None:
    root = tmp_path / "imports"
    root.mkdir()
    draft_id = "12345678-1234-4234-8234-123456789abc"
    verified = root / f".discarded-{draft_id}-{'a' * 32}"
    verified.mkdir()
    (verified / "stale.txt").write_text("stale", encoding="utf-8")
    unverified = root / f".discarded-not-a-uuid-{'b' * 32}"
    unverified.mkdir()
    locks = root / ".locks"
    locks.mkdir()
    (locks / f"{draft_id}.lock").write_bytes(b"\0")

    ImportDraftStore(root)

    assert not verified.exists()
    assert unverified.is_dir()
    assert (locks / f"{draft_id}.lock").is_file()


def test_tombstone_cleanup_permission_error_is_warned_and_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "imports"
    original_store = ImportDraftStore(root)
    existing = original_store.create_from_files("existing", [("existing.txt", b"body")])
    draft_id = "12345678-1234-4234-8234-123456789abc"
    tombstone = root / f".discarded-{draft_id}-{'a' * 32}"
    tombstone.mkdir()
    original_rmtree = import_drafts_module.shutil.rmtree

    def deny_tombstone(path: Path) -> None:
        if Path(path) == tombstone:
            raise PermissionError("busy")
        original_rmtree(path)

    monkeypatch.setattr(import_drafts_module.shutil, "rmtree", deny_tombstone)
    store = ImportDraftStore(root)

    assert store.get(existing.draft_id) == existing
    assert store.create_from_files("new", [("new.txt", b"body")]).draft_id
    assert tombstone.is_dir()
    assert "Could not remove discarded import draft" in caplog.text

    monkeypatch.setattr(import_drafts_module.shutil, "rmtree", original_rmtree)
    ImportDraftStore(root)
    assert not tombstone.exists()


def test_discard_permission_error_leaves_retryable_tombstone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "imports"
    store = ImportDraftStore(root)
    draft = store.create_from_files("book", [("book.txt", b"body")])
    original_rmtree = import_drafts_module.shutil.rmtree

    def deny_discard(path: Path) -> None:
        if Path(path).name.startswith(f".discarded-{draft.draft_id}-"):
            raise PermissionError("busy")
        original_rmtree(path)

    monkeypatch.setattr(import_drafts_module.shutil, "rmtree", deny_discard)

    store.discard(draft.draft_id)

    with pytest.raises(HTTPException) as exc:
        store.get(draft.draft_id)
    assert error_code(exc) == "IMPORT_DRAFT_NOT_FOUND"
    assert len(list(root.glob(f".discarded-{draft.draft_id}-*"))) == 1
    assert "Could not remove discarded import draft" in caplog.text

    monkeypatch.setattr(import_drafts_module.shutil, "rmtree", original_rmtree)
    ImportDraftStore(root)
    assert list(root.glob(f".discarded-{draft.draft_id}-*")) == []
