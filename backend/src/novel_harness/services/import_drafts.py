"""Safe, durable workspace storage for book-import drafts."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import stat
import threading
import time
import unicodedata
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO, StringIO
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID, uuid4, uuid5

from fastapi import HTTPException
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    model_validator,
)

from novel_harness.config import (
    DEFAULT_IMPORT_MAX_COMPRESSION_RATIO,
    DEFAULT_IMPORT_MAX_FILE_BYTES,
    DEFAULT_IMPORT_MAX_FILES,
    DEFAULT_IMPORT_MAX_TOTAL_BYTES,
    Settings,
)
from novel_harness.schemas.imports import (
    ImportCategory,
    ImportChapterPreview,
    ImportContinuation,
    ImportDraftPatch,
    ImportDraftRead,
    ImportFilePreview,
    ImportSourceKind,
    Payload,
)

logger = logging.getLogger(__name__)

_TEXT_EXTENSIONS = {".md", ".txt"}
_MANIFEST_NAME = "novel-import.json"
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
_WINDOWS_INVALID_CHARS = frozenset('<>:"|?*')
_WINDOWS_RESERVED_NAMES = {
    "con",
    "conin$",
    "conout$",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}
_MAX_CHAPTERS = 10_000
_LARGE_FILE_WARNING_PERCENT = 80
_MAX_FILE_WARNING_CHARS = 2_000
_PROCESS_LOCKS_GUARD = threading.Lock()
_DISCARD_TOMBSTONE = re.compile(
    r"^\.discarded-(?P<draft_id>[0-9a-f-]{36})-(?P<nonce>[0-9a-f]{32})$"
)
_MARKDOWN_HEADING = re.compile(r"(?m)^#{1,6}[ \t]+(?P<title>[^\r\n]+)")
_CHAPTER_ANCHOR = re.compile(
    r"(?m)^(?P<title>第[0-9０-９一二三四五六七八九十百千万零〇两]+[章卷]"
    r"(?:[ \t　:：.．、_-]+[^\r\n]*)?)[ \t　]*$"
)
_REPARSE_POINT_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _import_error(code: str, message: str, *, status_code: int = 422) -> HTTPException:
    return HTTPException(status_code, detail={"code": code, "message": message})


def _path_is_reparse_point(path: Path) -> bool:
    metadata = path.lstat()
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT_ATTRIBUTE
    )


def _fsync_directory(path: Path) -> None:
    """Best-effort directory durability; Windows may not permit directory handles."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            pass
    finally:
        os.close(descriptor)


def _fsync_directory_chain(start: Path, stop: Path) -> None:
    """Best-effort fsync for each existing raw ancestor through ``stop``."""
    current = start
    while current.is_relative_to(stop):
        _fsync_directory(current)
        if current == stop:
            return
        current = current.parent


@dataclass(slots=True)
class _ProcessLockEntry:
    lock: threading.Lock
    users: int = 0


_PROCESS_LOCKS: dict[str, _ProcessLockEntry] = {}


def _retain_process_lock(path: Path) -> tuple[str, _ProcessLockEntry]:
    key = os.path.normcase(str(path.resolve()))
    with _PROCESS_LOCKS_GUARD:
        entry = _PROCESS_LOCKS.get(key)
        if entry is None:
            entry = _ProcessLockEntry(threading.Lock())
            _PROCESS_LOCKS[key] = entry
        entry.users += 1
        return key, entry


def _release_process_lock(key: str, entry: _ProcessLockEntry) -> None:
    with _PROCESS_LOCKS_GUARD:
        entry.users -= 1
        if entry.users == 0 and _PROCESS_LOCKS.get(key) is entry:
            del _PROCESS_LOCKS[key]


class _DraftLifecycleLock:
    """Per-draft thread lock plus an OS lock that also fences other processes."""

    def __init__(self, root: Path, draft_id: str) -> None:
        self.path = root / ".locks" / f"{draft_id}.lock"
        self.registry_key = ""
        self.entry: _ProcessLockEntry | None = None
        self.handle: Any = None

    def __enter__(self) -> _DraftLifecycleLock:
        self.registry_key, self.entry = _retain_process_lock(self.path)
        self.entry.lock.acquire()
        handle = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a+b")
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                for attempt in range(20):
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if attempt == 19:
                            raise
                        # Windows can briefly retain a byte-range lock after the
                        # prior handle closes; the process lock still serializes
                        # local callers while this bounded retry bridges handoff.
                        time.sleep(0.005)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if handle is not None:
                handle.close()
            self.entry.lock.release()
            _release_process_lock(self.registry_key, self.entry)
            self.entry = None
            raise HTTPException(409, detail={"code": "DRAFT_BUSY"}) from exc
        self.handle = handle
        return self

    def __exit__(self, *args: object) -> None:
        try:
            if self.handle is not None:
                try:
                    self.handle.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
                self.handle.close()
                self.handle = None
        finally:
            assert self.entry is not None
            self.entry.lock.release()
            _release_process_lock(self.registry_key, self.entry)
            self.entry = None


@dataclass(frozen=True, slots=True)
class ImportLimits:
    max_files: int = DEFAULT_IMPORT_MAX_FILES
    max_file_bytes: int = DEFAULT_IMPORT_MAX_FILE_BYTES
    max_total_bytes: int = DEFAULT_IMPORT_MAX_TOTAL_BYTES
    max_compression_ratio: int = DEFAULT_IMPORT_MAX_COMPRESSION_RATIO

    def __post_init__(self) -> None:
        values = {
            "max_files": self.max_files,
            "max_file_bytes": self.max_file_bytes,
            "max_total_bytes": self.max_total_bytes,
            "max_compression_ratio": self.max_compression_ratio,
        }
        invalid = next((name for name, value in values.items() if type(value) is not int), None)
        if invalid is not None:
            raise TypeError(f"{invalid} must be an integer")
        if self.max_files < 1:
            raise ValueError("max_files must be at least 1")
        if self.max_file_bytes < 1 or self.max_total_bytes < 1:
            raise ValueError("byte limits must be positive")
        if self.max_file_bytes > self.max_total_bytes:
            raise ValueError("max_file_bytes cannot exceed max_total_bytes")
        if self.max_compression_ratio < 1:
            raise ValueError("max_compression_ratio must be at least 1")

    @classmethod
    def from_settings(cls, settings: Settings) -> ImportLimits:
        return cls(
            settings.import_max_files,
            settings.import_max_file_bytes,
            settings.import_max_total_bytes,
            settings.import_max_compression_ratio,
        )


class DraftFile(ImportFilePreview):
    content_preview: str = Field(default="", max_length=4000)


class DraftChapter(ImportChapterPreview):
    source_path: str = Field(min_length=1, max_length=4096)
    start: int = Field(ge=0)
    end: int = Field(ge=0)


class ImportDraftRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft_id: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=240)
    source_kind: ImportSourceKind
    manifest: Payload = Field(default_factory=dict)
    files: list[DraftFile] = Field(default_factory=list)
    chapters: list[DraftChapter] = Field(default_factory=list, max_length=_MAX_CHAPTERS)
    chapter_inventory: list[DraftChapter] = Field(default_factory=list, max_length=_MAX_CHAPTERS)
    continuation: ImportContinuation = Field(default_factory=ImportContinuation)
    objective: str = Field(default="", max_length=16_000)
    revision: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime
    warnings: list[str] = Field(default_factory=list)
    ignored: list[dict[str, str]] = Field(default_factory=list)
    committed_project_id: str | None = Field(default=None, min_length=1, max_length=64)
    committed_batch_id: str | None = Field(default=None, min_length=1, max_length=64)
    committed_fingerprint: str | None = Field(default=None, min_length=64, max_length=64)

    @model_validator(mode="after")
    def restore_legacy_chapter_inventory(self) -> ImportDraftRecord:
        if not self.chapter_inventory and self.chapters:
            self.chapter_inventory = [item.model_copy(deep=True) for item in self.chapters]
        for item in self.files:
            if not item.audit_id:
                item.audit_id = _audit_id(self.draft_id, item.relative_path, item.byte_hash)
        for collection in (self.chapter_inventory, self.chapters):
            for item in collection:
                if not item.draft_chapter_id:
                    item.draft_chapter_id = _draft_chapter_id(
                        self.draft_id, item.source_path, item.start, item.end
                    )
        return self

    @property
    def title(self) -> str:
        return self.display_name


def public_import_draft_payload(record: ImportDraftRecord) -> dict[str, Any]:
    """Serialize a draft through the sole API-safe public projection."""
    public = ImportDraftRead.model_validate(record, from_attributes=True)
    payload = public.model_dump(mode="json")
    for item in payload["files"]:
        if item["category"] not in {"task", "other"}:
            item["content_preview"] = ""
    for chapter in payload["chapters"]:
        chapter["content_preview"] = ""
    return payload


class _ManifestProject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: StrictStr | None = Field(default=None, min_length=1, max_length=240)
    display_name: StrictStr | None = Field(default=None, min_length=1, max_length=240)
    objective: StrictStr | None = Field(default=None, max_length=16_000)


class _ManifestFileOverride(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: StrictStr | None = Field(default=None, min_length=1, max_length=4096)
    relative_path: StrictStr | None = Field(default=None, min_length=1, max_length=4096)
    category: ImportCategory | None = None
    title: StrictStr | None = Field(default=None, min_length=1, max_length=240)
    selected: StrictBool | None = None

    @model_validator(mode="after")
    def require_path(self) -> _ManifestFileOverride:
        if self.path is None and self.relative_path is None:
            raise ValueError("manifest file path is required")
        if (
            self.path is not None
            and self.relative_path is not None
            and self.path != self.relative_path
        ):
            raise ValueError("manifest file path aliases disagree")
        return self


class _ManifestChapterOverride(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: StrictStr | None = Field(default=None, min_length=1, max_length=4096)
    relative_path: StrictStr | None = Field(default=None, min_length=1, max_length=4096)
    source_path: StrictStr | None = Field(default=None, min_length=1, max_length=4096)
    source_index: StrictInt | None = Field(default=None, ge=0)
    chapter_index: StrictInt | None = Field(default=None, ge=0)
    title: StrictStr | None = Field(default=None, min_length=1, max_length=240)
    order_index: StrictInt | None = Field(default=None, ge=0)
    existing_chapter_id: StrictStr | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def require_path(self) -> _ManifestChapterOverride:
        paths = [
            value
            for value in (self.path, self.relative_path, self.source_path)
            if value is not None
        ]
        if not paths:
            raise ValueError("manifest chapter path is required")
        if any(value != paths[0] for value in paths[1:]):
            raise ValueError("manifest chapter path aliases disagree")
        if (
            self.source_index is not None
            and self.chapter_index is not None
            and self.source_index != self.chapter_index
        ):
            raise ValueError("manifest chapter index aliases disagree")
        return self


class _ImportManifestV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: StrictInt
    project: _ManifestProject = Field(default_factory=_ManifestProject)
    files: list[_ManifestFileOverride] = Field(default_factory=list, max_length=10_000)
    chapters: list[_ManifestChapterOverride] = Field(
        default_factory=list, max_length=_MAX_CHAPTERS
    )
    continuation: ImportContinuation = Field(default_factory=ImportContinuation)

    @model_validator(mode="after")
    def require_supported_version(self) -> _ImportManifestV1:
        if self.version != 1:
            raise ValueError("unsupported manifest version")
        return self


@dataclass(frozen=True, slots=True)
class ChapterSegment:
    source_path: str
    title: str
    start: int
    end: int
    content: str
    boundary_uncertain: bool = False


def normalize_relative_path(path: str) -> str:
    """Return a stable POSIX relative path or reject an unsafe name."""
    return _normalize_relative_path(path, error_code="UNSAFE_IMPORT_PATH")


def _normalize_relative_path(path: str, *, error_code: str) -> str:
    if not isinstance(path, str) or not path or "\x00" in path:
        raise _import_error(error_code, "Import paths must be non-empty text.")
    replaced = unicodedata.normalize("NFC", path.replace("\\", "/"))
    if replaced.startswith("/") or _DRIVE_PREFIX.match(replaced):
        raise _import_error(error_code, f"Unsafe import path: {path!r}.")
    raw_parts = replaced.split("/")
    if any(part == "" for part in raw_parts):
        raise _import_error(error_code, f"Unsafe import path: {path!r}.")
    normalized_parts: list[str] = []
    for part in raw_parts:
        if part == "..":
            raise _import_error(error_code, f"Unsafe import path: {path!r}.")
        if part == ".":
            continue
        reserved_stem = (
            part.split(".", 1)[0]
            .rstrip(" .")
            .translate(str.maketrans({"¹": "1", "²": "2", "³": "3"}))
            .casefold()
        )
        if (
            _DRIVE_PREFIX.match(part)
            or part.endswith((".", " "))
            or len(part.encode("utf-8")) > 255
            or reserved_stem in _WINDOWS_RESERVED_NAMES
            or any(character in _WINDOWS_INVALID_CHARS for character in part)
            or any(ord(character) < 32 for character in part)
        ):
            raise _import_error(error_code, f"Unsafe import path: {path!r}.")
        normalized_parts.append(part)
    if not normalized_parts:
        raise _import_error(error_code, f"Unsafe import path: {path!r}.")
    normalized = PurePosixPath(*normalized_parts).as_posix()
    if len(normalized.encode("utf-8")) > 4096:
        raise _import_error(error_code, f"Unsafe import path: {path!r}.")
    return normalized


def decode_text(raw: bytes) -> tuple[str, str]:
    """Decode supported import text without replacement or data loss."""
    if raw.startswith(b"\xef\xbb\xbf"):
        try:
            return raw.decode("utf-8-sig", errors="strict"), "utf-8-sig"
        except UnicodeDecodeError as exc:
            raise _import_error(
                "IMPORT_DECODE_FAILED", "A UTF-8 BOM file contains invalid UTF-8."
            ) from exc
    for encoding in ("utf-8", "gb18030"):
        try:
            return raw.decode(encoding, errors="strict"), encoding
        except UnicodeDecodeError:
            continue
    raise _import_error(
        "IMPORT_DECODE_FAILED", "Text must be valid UTF-8, UTF-8 BOM, or GB18030."
    )


_CATEGORY_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "manuscript": (
        re.compile(r"(?:^|[/_. -])(manuscript|chapter|chapters)(?:$|[/_. -])", re.I),
        re.compile(r"正文|章节|小说稿|文稿"),
    ),
    "task": (
        re.compile(r"(?:^|[/_. -])(task|tasks|todo|next)(?:$|[/_. -])", re.I),
        re.compile(r"下一步|任务|待办"),
    ),
    "outline": (
        re.compile(r"(?:^|[/_. -])(outline|plot)(?:$|[/_. -])", re.I),
        re.compile(r"大纲|纲要|情节线"),
    ),
    "world": (
        re.compile(r"(?:^|[/_. -])(world|setting|settings|lore)(?:$|[/_. -])", re.I),
        re.compile(r"世界观|世界设定|背景设定|地理"),
    ),
    "character": (
        re.compile(r"(?:^|[/_. -])(character|characters|cast)(?:$|[/_. -])", re.I),
        re.compile(r"人物|角色|小传"),
    ),
    "style": (
        re.compile(r"(?:^|[/_. -])(style|voice)(?:$|[/_. -])", re.I),
        re.compile(r"文风|语言风格|写作风格"),
    ),
}


def classify_document(relative_path: str, text: str) -> str:
    """Classify from finite path/title signals; ambiguity deliberately becomes other."""
    normalized = normalize_relative_path(relative_path)
    bounded_text = text[:16_000]
    heading_titles = [
        match.group("title").strip()[:240]
        for match in _MARKDOWN_HEADING.finditer(bounded_text)
    ]
    sample = " ".join((normalized, *heading_titles))
    signals = {
        category
        for category, patterns in _CATEGORY_PATTERNS.items()
        if any(pattern.search(sample) for pattern in patterns)
    }
    if _CHAPTER_ANCHOR.search(bounded_text) or re.search(
        r"(?m)^#{1,6}[ \t]+第[^\r\n]{0,80}[章卷]", bounded_text
    ):
        signals.add("manuscript")
    return signals.pop() if len(signals) == 1 else "other"


def split_chapters(
    relative_path: str, text: str, *, max_segments: int | None = None
) -> list[ChapterSegment]:
    """Split source text at line-anchored headings while retaining exact offsets."""
    source_path = normalize_relative_path(relative_path)
    matches: list[tuple[int, str]] = []
    is_markdown = Path(source_path).suffix.casefold() == ".md"
    offset = 0
    preamble_count = 0
    for line in StringIO(text):
        candidate = line.rstrip("\r\n")
        heading = _MARKDOWN_HEADING.match(candidate) if is_markdown else None
        anchor = _CHAPTER_ANCHOR.fullmatch(candidate) if heading is None else None
        if heading is not None or anchor is not None:
            match = heading or anchor
            assert match is not None
            if not matches:
                preamble_count = int(offset > 0 and bool(text[:offset].strip()))
            prospective_count = preamble_count + len(matches) + 1
            if max_segments is not None and prospective_count > max_segments:
                raise _import_error(
                    "IMPORT_CHAPTER_LIMIT", "The import contains too many chapters."
                )
            matches.append((offset, match.group("title").strip()))
        offset += len(line)

    if not matches:
        if max_segments is not None and max_segments < 1:
            raise _import_error(
                "IMPORT_CHAPTER_LIMIT", "The import contains too many chapters."
            )
        return [ChapterSegment(source_path, "待命名章节", 0, len(text), text, True)]

    segments: list[ChapterSegment] = []
    if preamble_count:
        end = matches[0][0]
        segments.append(
            ChapterSegment(source_path, "待命名章节", 0, end, text[:end], True)
        )
    for index, (start, title) in enumerate(matches):
        end = matches[index + 1][0] if index + 1 < len(matches) else len(text)
        safe_title = title[:240].strip() or "待命名章节"
        segments.append(ChapterSegment(source_path, safe_title, start, end, text[start:end]))
    return segments


def _title_from_text(relative_path: str, text: str) -> str:
    for line in text.splitlines()[:20]:
        title = re.sub(r"^#{1,6}[ \t]+", "", line).strip()
        if title:
            return title[:240]
    return Path(relative_path).stem[:240] or "未命名文档"


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _append_file_warning(item: DraftFile, message: str) -> None:
    combined = f"{item.warning}；{message}" if item.warning else message
    if len(combined) > _MAX_FILE_WARNING_CHARS:
        raise RuntimeError("file warning exceeded its validated character budget")
    item.warning = combined


def _duplicate_content_warning(paths: list[str], max_chars: int) -> str:
    guidance = (
        f"检测到 {len(paths)} 份文件具有相同解码内容；"
        "系统不会自动删除副本，请确认要保留的文件。"
    )
    prefix = f"{guidance}对应路径（稳定排序）："
    complete = f"{prefix}{'、'.join(paths)}。"
    if len(complete) <= max_chars:
        return complete
    for shown_count in range(len(paths) - 1, 0, -1):
        omitted_count = len(paths) - shown_count
        candidate = (
            f"{prefix}{'、'.join(paths[:shown_count])}；"
            f"另有 {omitted_count} 项路径已省略。"
        )
        if len(candidate) <= max_chars:
            return candidate
    omitted_all = f"{guidance}另有 {len(paths)} 项路径已省略。"
    if len(omitted_all) <= max_chars:
        return omitted_all
    raise RuntimeError("file warning budget cannot retain duplicate guidance")


def _apply_parse_warnings(
    files: list[DraftFile], uncertain_paths: set[str], limits: ImportLimits
) -> None:
    empty_content_hash = _hash(b"")
    for item in files:
        if item.content_hash == empty_content_hash:
            _append_file_warning(item, "文件解码后为空。")
        if (
            item.size_bytes * 100
            >= limits.max_file_bytes * _LARGE_FILE_WARNING_PERCENT
        ):
            _append_file_warning(
                item,
                "文件大小接近单文件有效上限"
                f"（{item.size_bytes}/{limits.max_file_bytes} 字节，"
                f"警告阈值 {_LARGE_FILE_WARNING_PERCENT}%）。",
            )
        if item.category == "manuscript" and item.relative_path.casefold() in uncertain_paths:
            _append_file_warning(
                item,
                "章节边界不确定：未识别到明确章节标题，请在预览中确认名称和顺序。",
            )

    duplicate_groups: dict[str, list[DraftFile]] = {}
    for item in files:
        duplicate_groups.setdefault(item.content_hash, []).append(item)
    for group in duplicate_groups.values():
        if len(group) < 2:
            continue
        ordered = sorted(group, key=lambda item: item.relative_path.casefold())
        separator_chars = max(1 if item.warning else 0 for item in ordered)
        available_chars = min(
            _MAX_FILE_WARNING_CHARS - len(item.warning) - separator_chars
            for item in ordered
        )
        message = _duplicate_content_warning(
            [item.relative_path for item in ordered], available_chars
        )
        for item in ordered:
            _append_file_warning(item, message)


def _source_id(relative_path: str) -> str:
    return hashlib.sha256(relative_path.casefold().encode()).hexdigest()[:32]


def _audit_id(draft_id: str, relative_path: str, byte_hash: str) -> str:
    return str(
        uuid5(
            UUID(draft_id),
            f"source:{normalize_relative_path(relative_path).casefold()}:{byte_hash}",
        )
    )


def _draft_chapter_id(draft_id: str, source_path: str, start: int, end: int) -> str:
    return str(
        uuid5(
            UUID(draft_id),
            f"chapter:{normalize_relative_path(source_path).casefold()}:{start}:{end}",
        )
    )


class ImportDraftStore:
    """Persist import drafts below one fixed workspace root."""

    def __init__(self, root: Path, limits: ImportLimits | None = None) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.root = root.resolve()
        self.limits = limits or ImportLimits()
        self._cleanup_discard_tombstones()
        self._cleanup_stale_uploads()

    @classmethod
    def from_settings(cls, settings: Settings) -> ImportDraftStore:
        return cls(settings.data_dir / "imports", ImportLimits.from_settings(settings))

    def create_from_files(
        self,
        display_name: str,
        files: list[tuple[str, bytes]],
        source_kind: ImportSourceKind = "folder",
    ) -> ImportDraftRecord:
        name = display_name.strip()
        if not name or len(name) > 240:
            raise _import_error("IMPORT_NAME_INVALID", "Import display name is required.")
        normalized_files = self._validate_files(files)
        manifest = self._load_manifest(normalized_files)
        draft_id = str(uuid4())
        draft_dir = self._draft_dir(draft_id)
        raw_dir = draft_dir / "raw"
        try:
            raw_dir.mkdir(parents=True, exist_ok=False)
            for relative_path, raw in normalized_files:
                destination = self._raw_destination(raw_dir, relative_path)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("xb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                _fsync_directory_chain(destination.parent, raw_dir)
            record = self._build_record(draft_id, name, source_kind, normalized_files, manifest)
            self._write_json(draft_dir, record)
            return record
        except BaseException:
            if draft_dir.exists() and draft_dir.parent == self.root:
                shutil.rmtree(draft_dir)
            raise

    def create_upload_staging_dir(self) -> Path:
        """Create one verified staging directory without following reparse points."""
        upload_root = self._verified_upload_root(create=True)
        assert upload_root is not None
        candidate = upload_root / uuid4().hex
        try:
            candidate.mkdir(exist_ok=False)
            self._verify_upload_child(upload_root, candidate)
        except BaseException:
            self._remove_upload_child(candidate, ignore_missing=True)
            raise
        return candidate

    def cleanup_upload_staging_dir(self, candidate: Path) -> None:
        """Remove exactly one verified staging child and its empty root."""
        upload_root = self._verified_upload_root(create=False)
        if upload_root is None:
            return
        if candidate.parent != upload_root:
            raise RuntimeError("IMPORT_UPLOAD_STAGING_UNSAFE")
        try:
            parsed = UUID(candidate.name)
        except ValueError as exc:
            raise RuntimeError("IMPORT_UPLOAD_STAGING_UNSAFE") from exc
        if candidate.name not in {parsed.hex, str(parsed)}:
            raise RuntimeError("IMPORT_UPLOAD_STAGING_UNSAFE")
        if not _path_is_reparse_point(candidate):
            self._verify_upload_child(upload_root, candidate)
        self._remove_upload_child(candidate)
        try:
            upload_root.rmdir()
        except OSError:
            pass

    def create_from_zip(self, display_name: str, raw_zip: bytes) -> ImportDraftRecord:
        files: list[tuple[str, bytes]] = []
        try:
            with zipfile.ZipFile(BytesIO(raw_zip)) as archive:
                entries = self._precheck_zip(archive.infolist())
                for info, relative_path in entries:
                    content = self._bounded_zip_read(archive, info)
                    files.append((relative_path, content))
        except HTTPException:
            raise
        except (OSError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
            raise _import_error("IMPORT_ARCHIVE_INVALID", "The ZIP archive is invalid.") from exc
        return self.create_from_files(display_name, files, source_kind="zip")

    def create_from_staged_files(
        self,
        display_name: str,
        files: list[tuple[str, Path]],
    ) -> ImportDraftRecord:
        """Revalidate staged folder uploads before creating a durable draft."""
        name = display_name.strip()
        if not name or len(name) > 240:
            raise _import_error("IMPORT_NAME_INVALID", "Import display name is required.")
        if len(files) > self.limits.max_files:
            raise _import_error("IMPORT_FILE_LIMIT", "The import contains too many files.")
        prepared: list[tuple[str, Path]] = []
        seen: set[str] = set()
        for path, staged_path in files:
            relative_path = normalize_relative_path(path)
            duplicate_key = relative_path.casefold()
            if duplicate_key in seen:
                raise _import_error("UNSAFE_IMPORT_PATH", "Duplicate normalized import path.")
            seen.add(duplicate_key)
            if not staged_path.is_file() or staged_path.is_symlink():
                raise _import_error("IMPORT_FILE_INVALID", "A staged upload is unreadable.")
            prepared.append((relative_path, staged_path))

        draft_id = str(uuid4())
        draft_dir = self._draft_dir(draft_id)
        raw_dir = draft_dir / "raw"
        try:
            raw_dir.mkdir(parents=True, exist_ok=False)
            total = 0
            for relative_path, staged_path in prepared:
                destination = self._raw_destination(raw_dir, relative_path)
                destination.parent.mkdir(parents=True, exist_ok=True)
                total += self._copy_staged_file(
                    staged_path, destination, relative_path, self.limits.max_total_bytes - total
                )
                _fsync_directory_chain(destination.parent, raw_dir)
            paths = [relative_path for relative_path, _ in prepared]
            manifest = self._load_stored_manifest(raw_dir, paths)
            record = self._build_record_from_paths(
                draft_id, name, "folder", raw_dir, paths, manifest
            )
            self._write_json(draft_dir, record)
            return record
        except BaseException:
            if draft_dir.exists() and draft_dir.parent == self.root:
                shutil.rmtree(draft_dir)
            raise

    def create_from_zip_path(self, display_name: str, staged_path: Path) -> ImportDraftRecord:
        """Revalidate a staged ZIP body and all of its central-directory entries."""
        name = display_name.strip()
        if not name or len(name) > 240:
            raise _import_error("IMPORT_NAME_INVALID", "Import display name is required.")
        if not staged_path.is_file() or staged_path.is_symlink():
            raise _import_error("IMPORT_FILE_INVALID", "A staged upload is unreadable.")
        try:
            if staged_path.stat().st_size > self.limits.max_total_bytes:
                raise _import_error(
                    "IMPORT_TOTAL_TOO_LARGE", "The ZIP upload is too large."
                )
        except OSError as exc:
            raise _import_error("IMPORT_FILE_INVALID", "A staged upload is unreadable.") from exc

        draft_id = str(uuid4())
        draft_dir = self._draft_dir(draft_id)
        raw_dir = draft_dir / "raw"
        completed = False
        try:
            with zipfile.ZipFile(staged_path) as archive:
                entries = self._precheck_zip(archive.infolist())
                raw_dir.mkdir(parents=True, exist_ok=False)
                total = 0
                paths: list[str] = []
                for info, relative_path in entries:
                    destination = self._raw_destination(raw_dir, relative_path)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    actual = self._extract_zip_entry(
                        archive,
                        info,
                        destination,
                        self.limits.max_total_bytes - total,
                    )
                    total += actual
                    paths.append(relative_path)
                    _fsync_directory_chain(destination.parent, raw_dir)
                manifest = self._load_stored_manifest(raw_dir, paths)
                record = self._build_record_from_paths(
                    draft_id, name, "zip", raw_dir, paths, manifest
                )
                self._write_json(draft_dir, record)
                completed = True
                return record
        except HTTPException:
            raise
        except (OSError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
            raise _import_error("IMPORT_ARCHIVE_INVALID", "The ZIP archive is invalid.") from exc
        finally:
            if draft_dir.exists() and not completed:
                shutil.rmtree(draft_dir)

    def _copy_staged_file(
        self, source_path: Path, destination: Path, label: str, total_remaining: int
    ) -> int:
        actual = 0
        try:
            with source_path.open("rb") as source, destination.open("xb") as target:
                while chunk := source.read(64 * 1024):
                    actual += len(chunk)
                    if actual > self.limits.max_file_bytes:
                        raise _import_error(
                            "IMPORT_FILE_TOO_LARGE", f"File is too large: {label}."
                        )
                    if actual > total_remaining:
                        raise _import_error(
                            "IMPORT_TOTAL_TOO_LARGE", "Expanded import is too large."
                        )
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
        except HTTPException:
            raise
        except OSError as exc:
            raise _import_error("IMPORT_FILE_INVALID", "A staged upload is unreadable.") from exc
        return actual

    def _extract_zip_entry(
        self,
        archive: zipfile.ZipFile,
        info: zipfile.ZipInfo,
        destination: Path,
        total_remaining: int,
    ) -> int:
        actual = 0
        with archive.open(info, "r") as source, destination.open("xb") as target:
            while chunk := source.read(64 * 1024):
                actual += len(chunk)
                if actual > self.limits.max_file_bytes:
                    raise _import_error(
                        "IMPORT_FILE_TOO_LARGE", f"File is too large: {info.filename}."
                    )
                if actual > total_remaining:
                    raise _import_error(
                        "IMPORT_TOTAL_TOO_LARGE", "Expanded archive is too large."
                    )
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        if actual != info.file_size:
            raise _import_error(
                "IMPORT_ARCHIVE_INVALID",
                "ZIP entry size does not match its directory record.",
            )
        return actual

    def _read_staged_file(self, path: Path, label: str) -> bytes:
        chunks: list[bytes] = []
        actual = 0
        try:
            if not path.is_file() or path.is_symlink():
                raise OSError("staged upload is not a regular file")
            with path.open("rb") as source:
                while chunk := source.read(64 * 1024):
                    actual += len(chunk)
                    if actual > self.limits.max_file_bytes:
                        raise _import_error(
                            "IMPORT_FILE_TOO_LARGE", f"File is too large: {label}."
                        )
                    chunks.append(chunk)
        except HTTPException:
            raise
        except OSError as exc:
            raise _import_error("IMPORT_FILE_INVALID", "A staged upload is unreadable.") from exc
        return b"".join(chunks)

    def get(self, draft_id: str) -> ImportDraftRecord:
        self._draft_dir(draft_id)
        with _DraftLifecycleLock(self.root, draft_id):
            return self._get_unlocked(draft_id)

    @contextmanager
    def lifecycle(self, draft_id: str):
        """Hold the same cross-thread/process lock used by get/update/discard."""
        self._draft_dir(draft_id)
        with _DraftLifecycleLock(self.root, draft_id):
            yield

    def _get_unlocked(self, draft_id: str) -> ImportDraftRecord:
        draft_dir = self._existing_draft_dir(draft_id)
        metadata_path = draft_dir / "draft.json"
        try:
            raw = metadata_path.read_text(encoding="utf-8")
            return ImportDraftRecord.model_validate_json(raw)
        except FileNotFoundError as exc:
            raise HTTPException(404, detail={"code": "IMPORT_DRAFT_NOT_FOUND"}) from exc
        except (OSError, ValidationError, ValueError) as exc:
            raise HTTPException(503, detail={"code": "IMPORT_DRAFT_UNREADABLE"}) from exc

    def update(self, draft_id: str, patch: ImportDraftPatch | dict[str, Any]) -> ImportDraftRecord:
        try:
            changes = (
                patch
                if isinstance(patch, ImportDraftPatch)
                else ImportDraftPatch.model_validate(patch)
            )
        except ValidationError as exc:
            raise _import_error(
                "IMPORT_DRAFT_PATCH_INVALID", "The draft patch is invalid."
            ) from exc
        self._draft_dir(draft_id)
        with _DraftLifecycleLock(self.root, draft_id):
            draft_dir = self._existing_draft_dir(draft_id)
            current = self._get_unlocked(draft_id)
            if current.committed_project_id is not None:
                raise HTTPException(409, detail={"code": "IMPORT_DRAFT_COMMITTED"})
            if changes.revision != current.revision:
                raise HTTPException(
                    409,
                    detail={
                        "code": "DRAFT_REVISION_CONFLICT",
                        "current": public_import_draft_payload(current),
                    },
                )
            updated = current.model_copy(deep=True)
            if changes.title is not None:
                updated.display_name = changes.title.strip()
            if changes.files is not None:
                updated.files = self._merge_patched_files(current.files, changes.files)
            inventory = [item.model_copy(deep=True) for item in current.chapter_inventory]
            if changes.chapters is not None:
                order_indexes = [chapter.order_index for chapter in changes.chapters]
                if len(order_indexes) != len(set(order_indexes)):
                    raise _import_error(
                        "IMPORT_CHAPTER_ORDER_INVALID",
                        "Chapter order indexes must be unique.",
                    )
                inventory = self._merge_patched_chapters(inventory, changes.chapters)
            inventory = self._normalize_chapter_inventory(inventory)
            inventory_paths = {
                normalize_relative_path(item.relative_path).casefold() for item in inventory
            }
            for item in updated.files:
                path_key = normalize_relative_path(item.relative_path).casefold()
                if item.category != "manuscript" or path_key in inventory_paths:
                    continue
                remaining = _MAX_CHAPTERS - len(inventory)
                discovered = self._split_stored_source(draft_dir, item, remaining)
                for segment in discovered:
                    inventory.append(
                        DraftChapter(
                            source_document_id=_source_id(segment.source_path),
                            draft_chapter_id=_draft_chapter_id(
                                updated.draft_id,
                                segment.source_path,
                                segment.start,
                                segment.end,
                            ),
                            relative_path=segment.source_path,
                            source_path=segment.source_path,
                            title=segment.title,
                            order_index=len(inventory),
                            existing_chapter_id=None,
                            content_preview=segment.content[:4000],
                            start=segment.start,
                            end=segment.end,
                        )
                    )
                inventory_paths.add(path_key)
            if len(inventory) > _MAX_CHAPTERS:
                raise _import_error(
                    "IMPORT_CHAPTER_LIMIT", "The import contains too many chapters."
                )
            updated.chapter_inventory = inventory
            updated.chapters = self._active_chapters(updated.files, inventory)
            if changes.continuation is not None:
                updated.continuation = changes.continuation
            if changes.objective is not None:
                updated.objective = changes.objective
            updated.revision += 1
            updated.updated_at = datetime.now(UTC)
            self._write_json(draft_dir, updated)
            return updated

    def discard(self, draft_id: str) -> None:
        self._draft_dir(draft_id)
        tombstone: Path | None = None
        with _DraftLifecycleLock(self.root, draft_id):
            draft_dir = self._existing_draft_dir(draft_id)
            current = self._get_unlocked(draft_id)
            if current.committed_project_id is not None:
                raise HTTPException(409, detail={"code": "IMPORT_DRAFT_COMMITTED"})
            tombstone = self.root / f".discarded-{draft_id}-{uuid4().hex}"
            if tombstone.parent != self.root or tombstone.exists():
                raise HTTPException(409, detail={"code": "DRAFT_BUSY"})
            os.replace(draft_dir, tombstone)
            _fsync_directory(self.root)
        if tombstone is not None:
            self._remove_tombstone(tombstone)

    def _validate_files(self, files: list[tuple[str, bytes]]) -> list[tuple[str, bytes]]:
        if len(files) > self.limits.max_files:
            raise _import_error("IMPORT_FILE_LIMIT", "The import contains too many files.")
        seen: set[str] = set()
        total = 0
        normalized: list[tuple[str, bytes]] = []
        for path, raw in files:
            relative_path = normalize_relative_path(path)
            duplicate_key = relative_path.casefold()
            if duplicate_key in seen:
                raise _import_error("UNSAFE_IMPORT_PATH", "Duplicate normalized import path.")
            seen.add(duplicate_key)
            if not isinstance(raw, bytes):
                raise _import_error("IMPORT_FILE_INVALID", "Imported file content must be bytes.")
            if len(raw) > self.limits.max_file_bytes:
                raise _import_error("IMPORT_FILE_TOO_LARGE", f"File is too large: {relative_path}.")
            total += len(raw)
            if total > self.limits.max_total_bytes:
                raise _import_error("IMPORT_TOTAL_TOO_LARGE", "Expanded import is too large.")
            normalized.append((relative_path, raw))
        return normalized

    @staticmethod
    def _merge_patched_files(
        current: list[DraftFile], patched: list[ImportFilePreview]
    ) -> list[DraftFile]:
        current_by_path = {
            normalize_relative_path(item.relative_path).casefold(): item for item in current
        }
        seen: set[str] = set()
        merged: list[DraftFile] = []
        for item in patched:
            path_key = normalize_relative_path(item.relative_path).casefold()
            if path_key in seen:
                raise _import_error(
                    "IMPORT_FILE_DUPLICATE", "A file appears more than once in the patch."
                )
            seen.add(path_key)
            existing = current_by_path.get(path_key)
            if existing is None:
                raise _import_error(
                    "IMPORT_FILE_UNKNOWN", "The patch refers to an unknown import file."
                )
            if item.audit_id and item.audit_id != existing.audit_id:
                raise _import_error(
                    "IMPORT_FILE_UNKNOWN", "The patch refers to an unknown import file."
                )
            merged.append(
                DraftFile(
                    relative_path=existing.relative_path,
                    audit_id=existing.audit_id,
                    category=item.category,
                    title=item.title,
                    encoding=existing.encoding,
                    size_bytes=existing.size_bytes,
                    byte_hash=existing.byte_hash,
                    content_hash=existing.content_hash,
                    selected=item.selected,
                    warning=existing.warning,
                    content_preview=existing.content_preview,
                )
            )
        if seen != set(current_by_path):
            raise _import_error(
                "IMPORT_FILE_MISSING", "The patch must include every imported source file."
            )
        return merged

    @staticmethod
    def _merge_patched_chapters(
        current: list[DraftChapter], patched: list[ImportChapterPreview]
    ) -> list[DraftChapter]:
        merged = [item.model_copy(deep=True) for item in current]
        used: set[int] = set()
        for item in patched:
            match_index = next(
                (
                    index
                    for index, existing in enumerate(merged)
                    if index not in used
                    if (
                        (
                            existing.draft_chapter_id == item.draft_chapter_id
                            and existing.source_document_id
                            == item.source_document_id
                            and existing.relative_path.casefold()
                            == item.relative_path.casefold()
                        )
                        if item.draft_chapter_id
                        else (
                            existing.source_document_id == item.source_document_id
                            and existing.relative_path.casefold()
                            == item.relative_path.casefold()
                            and existing.content_preview == item.content_preview
                        )
                    )
                ),
                None,
            )
            if match_index is None:
                raise _import_error(
                    "IMPORT_CHAPTER_UNKNOWN",
                    "Patched chapters must refer to a discovered source segment.",
                )
            used.add(match_index)
            existing = merged[match_index]
            merged[match_index] = DraftChapter(
                source_document_id=existing.source_document_id,
                draft_chapter_id=existing.draft_chapter_id,
                relative_path=existing.relative_path,
                source_path=existing.source_path,
                title=item.title,
                order_index=item.order_index,
                existing_chapter_id=item.existing_chapter_id,
                content_preview=existing.content_preview,
                start=existing.start,
                end=existing.end,
            )
        return merged

    @staticmethod
    def _active_chapters(
        files: list[DraftFile], inventory: list[DraftChapter]
    ) -> list[DraftChapter]:
        allowed_paths = {
            normalize_relative_path(item.relative_path).casefold()
            for item in files
            if item.selected and item.category == "manuscript"
        }
        active = [
            item.model_copy(deep=True)
            for item in inventory
            if normalize_relative_path(item.relative_path).casefold() in allowed_paths
        ]
        active.sort(key=lambda item: item.order_index)
        for order_index, chapter in enumerate(active):
            chapter.order_index = order_index
        return active

    @staticmethod
    def _normalize_chapter_inventory(
        inventory: list[DraftChapter],
    ) -> list[DraftChapter]:
        order_indexes = [chapter.order_index for chapter in inventory]
        if len(order_indexes) != len(set(order_indexes)):
            raise _import_error(
                "IMPORT_CHAPTER_ORDER_INVALID",
                "Chapter order indexes must be unique.",
            )
        normalized = sorted(inventory, key=lambda item: item.order_index)
        for order_index, chapter in enumerate(normalized):
            chapter.order_index = order_index
        return normalized

    @staticmethod
    def _split_stored_source(
        draft_dir: Path, item: DraftFile, remaining: int
    ) -> list[ChapterSegment]:
        raw_dir = draft_dir / "raw"
        destination = ImportDraftStore._raw_destination(raw_dir, item.relative_path)
        try:
            if not destination.is_file() or destination.is_symlink():
                raise OSError("stored source is not a regular file")
            raw = destination.read_bytes()
        except OSError as exc:
            raise HTTPException(503, detail={"code": "IMPORT_DRAFT_UNREADABLE"}) from exc
        if len(raw) != item.size_bytes or _hash(raw) != item.byte_hash:
            raise HTTPException(503, detail={"code": "IMPORT_DRAFT_UNREADABLE"})
        text, _ = decode_text(raw)
        return split_chapters(item.relative_path, text, max_segments=remaining)

    def _load_manifest(self, files: list[tuple[str, bytes]]) -> dict[str, Any]:
        raw_manifest = next(
            (raw for path, raw in files if path.casefold() == _MANIFEST_NAME), None
        )
        if raw_manifest is None:
            return {}
        try:
            manifest = json.loads(raw_manifest.decode("utf-8-sig", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _import_error(
                "IMPORT_MANIFEST_INVALID", "novel-import.json is invalid UTF-8 JSON."
            ) from exc
        if not isinstance(manifest, dict):
            raise _import_error("IMPORT_MANIFEST_INVALID", "Manifest root must be an object.")
        if type(manifest.get("version")) is int and manifest["version"] != 1:
            raise _import_error("IMPORT_MANIFEST_VERSION", "Only manifest version 1 is supported.")
        prepared = dict(manifest)
        file_value = prepared.get("files")
        if isinstance(file_value, dict):
            converted: list[dict[str, Any]] = []
            for path, override in file_value.items():
                if not isinstance(override, dict) or any(
                    key in override for key in ("path", "relative_path")
                ):
                    raise _import_error(
                        "IMPORT_MANIFEST_INVALID", "Manifest file overrides are invalid."
                    )
                converted.append({"path": path, **override})
            prepared["files"] = converted
        try:
            validated = _ImportManifestV1.model_validate(prepared)
        except ValidationError as exc:
            raise _import_error(
                "IMPORT_MANIFEST_INVALID", "novel-import.json does not match version 1."
            ) from exc
        return validated.model_dump(mode="json", exclude_none=True, exclude_unset=True)

    def _load_stored_manifest(
        self, raw_dir: Path, paths: list[str]
    ) -> dict[str, Any]:
        manifest_path = next(
            (path for path in paths if path.casefold() == _MANIFEST_NAME), None
        )
        if manifest_path is None:
            return {}
        destination = self._raw_destination(raw_dir, manifest_path)
        return self._load_manifest(
            [(manifest_path, self._read_staged_file(destination, manifest_path))]
        )

    def _build_record(
        self,
        draft_id: str,
        display_name: str,
        source_kind: ImportSourceKind,
        raw_files: list[tuple[str, bytes]],
        manifest: dict[str, Any],
    ) -> ImportDraftRecord:
        source_raw = [
            (path, raw)
            for path, raw in raw_files
            if path.casefold() != _MANIFEST_NAME
            and Path(path).suffix.casefold() in _TEXT_EXTENSIONS
        ]
        source_paths = {path.casefold(): path for path, _ in source_raw}
        file_overrides = self._file_overrides(manifest.get("files", []), source_paths)
        files: list[DraftFile] = []
        decoded: dict[str, str] = {}
        for relative_path, raw in sorted(source_raw, key=lambda item: item[0].casefold()):
            text, encoding = decode_text(raw)
            decoded[relative_path] = text
            override = file_overrides.get(relative_path.casefold(), {})
            category = override.get("category", classify_document(relative_path, text))
            if category not in {
                "manuscript",
                "task",
                "outline",
                "world",
                "character",
                "style",
                "other",
            }:
                raise _import_error("IMPORT_MANIFEST_INVALID", "Manifest file category is invalid.")
            text_bytes = text.encode("utf-8")
            files.append(
                DraftFile(
                    relative_path=relative_path,
                    audit_id=_audit_id(draft_id, relative_path, _hash(raw)),
                    category=category,
                    title=override.get("title", _title_from_text(relative_path, text)),
                    encoding=encoding,
                    size_bytes=len(raw),
                    byte_hash=_hash(raw),
                    content_hash=_hash(text_bytes),
                    selected=override.get("selected", True),
                    warning="",
                    content_preview=text[:4000],
                )
            )

        chapter_segments: list[ChapterSegment] = []
        for item in files:
            if item.category == "manuscript":
                remaining = _MAX_CHAPTERS - len(chapter_segments)
                discovered = split_chapters(
                    item.relative_path,
                    decoded[item.relative_path],
                    max_segments=remaining,
                )
                chapter_segments.extend(discovered)
        chapter_overrides = self._chapter_overrides(
            manifest.get("chapters", []), source_paths, chapter_segments
        )
        per_path_index: dict[str, int] = {}
        chapters: list[DraftChapter] = []
        uncertain_paths: set[str] = set()
        for segment in chapter_segments:
            path_key = segment.source_path.casefold()
            source_index = per_path_index.get(path_key, 0)
            per_path_index[path_key] = source_index + 1
            override = chapter_overrides.get((path_key, source_index), {})
            if segment.boundary_uncertain and not override:
                uncertain_paths.add(path_key)
            chapters.append(
                DraftChapter(
                    source_document_id=_source_id(segment.source_path),
                    draft_chapter_id=_draft_chapter_id(
                        draft_id, segment.source_path, segment.start, segment.end
                    ),
                    relative_path=segment.source_path,
                    source_path=segment.source_path,
                    title=override.get("title", segment.title),
                    order_index=override.get("order_index", len(chapters)),
                    existing_chapter_id=override.get("existing_chapter_id"),
                    content_preview=segment.content[:4000],
                    start=segment.start,
                    end=segment.end,
                )
            )
        chapters.sort(key=lambda item: (item.order_index, item.source_path.casefold(), item.start))
        for order_index, chapter in enumerate(chapters):
            chapter.order_index = order_index
        _apply_parse_warnings(files, uncertain_paths, self.limits)
        active_chapters = self._active_chapters(files, chapters)

        ignored = [
            {"relative_path": path, "reason": "UNSUPPORTED_FILE_TYPE"}
            for path, _ in sorted(raw_files, key=lambda item: item[0].casefold())
            if path.casefold() != _MANIFEST_NAME
            and Path(path).suffix.casefold() not in _TEXT_EXTENSIONS
        ]
        warnings = [f"Ignored unsupported file: {item['relative_path']}" for item in ignored]
        warnings.extend(
            f"{item.relative_path}: {item.warning}" for item in files if item.warning
        )
        project = manifest.get("project", {})
        objective = project.get("objective", "")
        continuation_data = manifest.get("continuation", {})
        try:
            continuation = ImportContinuation.model_validate(continuation_data)
        except ValidationError as exc:
            raise _import_error(
                "IMPORT_MANIFEST_INVALID", "Manifest continuation is invalid."
            ) from exc
        now = datetime.now(UTC)
        return ImportDraftRecord(
            draft_id=draft_id,
            display_name=display_name,
            source_kind=source_kind,
            manifest=manifest,
            files=files,
            chapters=active_chapters,
            chapter_inventory=[item.model_copy(deep=True) for item in chapters],
            continuation=continuation,
            objective=objective,
            revision=1,
            created_at=now,
            updated_at=now,
            warnings=warnings,
            ignored=ignored,
        )

    def _build_record_from_paths(
        self,
        draft_id: str,
        display_name: str,
        source_kind: ImportSourceKind,
        raw_dir: Path,
        raw_paths: list[str],
        manifest: dict[str, Any],
    ) -> ImportDraftRecord:
        """Build previews one stored source at a time without retaining full book text."""
        source_paths = {
            path.casefold(): path
            for path in raw_paths
            if path.casefold() != _MANIFEST_NAME
            and Path(path).suffix.casefold() in _TEXT_EXTENSIONS
        }
        file_overrides = self._file_overrides(manifest.get("files", []), source_paths)
        files: list[DraftFile] = []
        chapter_candidates: list[DraftChapter] = []
        uncertain_chapter_candidates: set[tuple[str, int, int]] = set()
        for relative_path in sorted(source_paths.values(), key=str.casefold):
            destination = self._raw_destination(raw_dir, relative_path)
            raw = self._read_staged_file(destination, relative_path)
            text, encoding = decode_text(raw)
            override = file_overrides.get(relative_path.casefold(), {})
            category = override.get("category", classify_document(relative_path, text))
            if category not in {
                "manuscript",
                "task",
                "outline",
                "world",
                "character",
                "style",
                "other",
            }:
                raise _import_error(
                    "IMPORT_MANIFEST_INVALID", "Manifest file category is invalid."
                )
            text_bytes = text.encode("utf-8")
            files.append(
                DraftFile(
                    relative_path=relative_path,
                    audit_id=_audit_id(draft_id, relative_path, _hash(raw)),
                    category=category,
                    title=override.get("title", _title_from_text(relative_path, text)),
                    encoding=encoding,
                    size_bytes=len(raw),
                    byte_hash=_hash(raw),
                    content_hash=_hash(text_bytes),
                    selected=override.get("selected", True),
                    warning="",
                    content_preview=text[:4000],
                )
            )
            if category == "manuscript":
                remaining = _MAX_CHAPTERS - len(chapter_candidates)
                for segment in split_chapters(
                    relative_path, text, max_segments=remaining
                ):
                    if segment.boundary_uncertain:
                        uncertain_chapter_candidates.add(
                            (segment.source_path.casefold(), segment.start, segment.end)
                        )
                    chapter_candidates.append(
                        DraftChapter(
                            source_document_id=_source_id(segment.source_path),
                            draft_chapter_id=_draft_chapter_id(
                                draft_id,
                                segment.source_path,
                                segment.start,
                                segment.end,
                            ),
                            relative_path=segment.source_path,
                            source_path=segment.source_path,
                            title=segment.title,
                            order_index=len(chapter_candidates),
                            existing_chapter_id=None,
                            content_preview=segment.content[:4000],
                            start=segment.start,
                            end=segment.end,
                        )
                    )

        chapter_overrides = self._chapter_overrides(
            manifest.get("chapters", []), source_paths, chapter_candidates
        )
        per_path_index: dict[str, int] = {}
        chapters: list[DraftChapter] = []
        uncertain_paths: set[str] = set()
        for candidate in chapter_candidates:
            path_key = candidate.source_path.casefold()
            source_index = per_path_index.get(path_key, 0)
            per_path_index[path_key] = source_index + 1
            override = chapter_overrides.get((path_key, source_index), {})
            if (
                (path_key, candidate.start, candidate.end)
                in uncertain_chapter_candidates
                and not override
            ):
                uncertain_paths.add(path_key)
            chapters.append(
                candidate.model_copy(
                    update={
                        "title": override.get("title", candidate.title),
                        "order_index": override.get("order_index", len(chapters)),
                        "existing_chapter_id": override.get("existing_chapter_id"),
                    }
                )
            )
        chapters.sort(
            key=lambda item: (item.order_index, item.source_path.casefold(), item.start)
        )
        for order_index, chapter in enumerate(chapters):
            chapter.order_index = order_index
        _apply_parse_warnings(files, uncertain_paths, self.limits)
        active_chapters = self._active_chapters(files, chapters)

        ignored = [
            {"relative_path": path, "reason": "UNSUPPORTED_FILE_TYPE"}
            for path in sorted(raw_paths, key=str.casefold)
            if path.casefold() != _MANIFEST_NAME
            and Path(path).suffix.casefold() not in _TEXT_EXTENSIONS
        ]
        warnings = [f"Ignored unsupported file: {item['relative_path']}" for item in ignored]
        warnings.extend(
            f"{item.relative_path}: {item.warning}" for item in files if item.warning
        )
        project = manifest.get("project", {})
        objective = project.get("objective", "")
        continuation_data = manifest.get("continuation", {})
        try:
            continuation = ImportContinuation.model_validate(continuation_data)
        except ValidationError as exc:
            raise _import_error(
                "IMPORT_MANIFEST_INVALID", "Manifest continuation is invalid."
            ) from exc
        now = datetime.now(UTC)
        return ImportDraftRecord(
            draft_id=draft_id,
            display_name=display_name,
            source_kind=source_kind,
            manifest=manifest,
            files=files,
            chapters=active_chapters,
            chapter_inventory=[item.model_copy(deep=True) for item in chapters],
            continuation=continuation,
            objective=objective,
            revision=1,
            created_at=now,
            updated_at=now,
            warnings=warnings,
            ignored=ignored,
        )

    def _file_overrides(
        self, value: object, source_paths: dict[str, str]
    ) -> dict[str, dict[str, Any]]:
        if not value:
            return {}
        entries: list[dict[str, Any]]
        if isinstance(value, dict):
            entries = [
                dict(override, path=path)
                for path, override in value.items()
                if isinstance(override, dict)
            ]
            if len(entries) != len(value):
                raise _import_error(
                    "IMPORT_MANIFEST_INVALID", "Manifest file overrides are invalid."
                )
        elif isinstance(value, list) and all(isinstance(item, dict) for item in value):
            entries = value
        else:
            raise _import_error("IMPORT_MANIFEST_INVALID", "Manifest files are invalid.")
        result: dict[str, dict[str, Any]] = {}
        for entry in entries:
            if set(entry) - {"path", "relative_path", "category", "title", "selected"}:
                raise _import_error(
                    "IMPORT_MANIFEST_INVALID", "Manifest file override has unknown fields."
                )
            path_value = entry.get("path", entry.get("relative_path"))
            if not isinstance(path_value, str):
                raise _import_error("IMPORT_MANIFEST_INVALID", "Manifest file path is required.")
            path = normalize_relative_path(path_value)
            key = path.casefold()
            if key not in source_paths:
                raise _import_error(
                    "IMPORT_MANIFEST_UNKNOWN_PATH", f"Unknown manifest path: {path}."
                )
            if key in result:
                raise _import_error("IMPORT_MANIFEST_INVALID", f"Duplicate file override: {path}.")
            if "selected" in entry and not isinstance(entry["selected"], bool):
                raise _import_error("IMPORT_MANIFEST_INVALID", "Manifest selected must be boolean.")
            result[key] = entry
        return result

    def _chapter_overrides(
        self,
        value: object,
        source_paths: dict[str, str],
        segments: list[ChapterSegment] | list[DraftChapter],
    ) -> dict[tuple[str, int], dict[str, Any]]:
        if not value:
            return {}
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise _import_error("IMPORT_MANIFEST_INVALID", "Manifest chapters must be a list.")
        counts: dict[str, int] = {}
        for segment in segments:
            key = segment.source_path.casefold()
            counts[key] = counts.get(key, 0) + 1
        result: dict[tuple[str, int], dict[str, Any]] = {}
        for entry in value:
            allowed = {
                "path",
                "relative_path",
                "source_path",
                "source_index",
                "chapter_index",
                "title",
                "order_index",
                "existing_chapter_id",
            }
            if set(entry) - allowed:
                raise _import_error(
                    "IMPORT_MANIFEST_INVALID", "Manifest chapter has unknown fields."
                )
            path_value = entry.get("path", entry.get("source_path", entry.get("relative_path")))
            if not isinstance(path_value, str):
                raise _import_error("IMPORT_MANIFEST_INVALID", "Manifest chapter path is required.")
            path = normalize_relative_path(path_value)
            path_key = path.casefold()
            if path_key not in source_paths:
                raise _import_error(
                    "IMPORT_MANIFEST_UNKNOWN_PATH", f"Unknown manifest path: {path}."
                )
            index = entry.get("source_index", entry.get("chapter_index", 0))
            if type(index) is not int or index < 0 or index >= counts.get(path_key, 0):
                raise _import_error(
                    "IMPORT_MANIFEST_UNKNOWN_PATH", "Manifest chapter index is invalid."
                )
            key = (path_key, index)
            if key in result:
                raise _import_error(
                    "IMPORT_MANIFEST_DUPLICATE_CHAPTER", "Duplicate manifest chapter mapping."
                )
            if "order_index" in entry and (
                type(entry["order_index"]) is not int or entry["order_index"] < 0
            ):
                raise _import_error("IMPORT_MANIFEST_INVALID", "Manifest chapter order is invalid.")
            result[key] = entry
        return result

    def _precheck_zip(self, infos: list[zipfile.ZipInfo]) -> list[tuple[zipfile.ZipInfo, str]]:
        entries: list[tuple[zipfile.ZipInfo, str]] = []
        seen: set[str] = set()
        total = 0
        for entry_index, info in enumerate(infos, start=1):
            if entry_index > self.limits.max_files:
                raise _import_error("IMPORT_FILE_LIMIT", "The archive has too many entries.")
            if info.flag_bits & 0x1:
                raise _import_error(
                    "IMPORT_ARCHIVE_INVALID", "Encrypted ZIP entries are not supported."
                )
            is_directory = info.filename.endswith("/")
            raw_name = (
                info.filename[:-1]
                if is_directory and info.filename.endswith("/")
                else info.filename
            )
            relative_path = _normalize_relative_path(
                raw_name, error_code="UNSAFE_ARCHIVE_PATH"
            )
            mode = info.external_attr >> 16
            file_type = stat.S_IFMT(mode)
            if info.create_system == 3 and stat.S_ISLNK(mode):
                raise _import_error("ARCHIVE_LINK_FORBIDDEN", "ZIP symlinks are forbidden.")
            if info.create_system == 3 and file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                raise _import_error(
                    "ARCHIVE_LINK_FORBIDDEN", "Non-regular ZIP entries are forbidden."
                )
            if info.create_system == 3:
                type_mismatch = (
                    is_directory and file_type not in {0, stat.S_IFDIR}
                ) or (not is_directory and file_type == stat.S_IFDIR)
                if type_mismatch:
                    raise _import_error(
                        "IMPORT_ARCHIVE_INVALID",
                        "ZIP entry name and Unix type disagree.",
                    )
            if is_directory and info.file_size != 0:
                raise _import_error(
                    "IMPORT_ARCHIVE_INVALID", "ZIP directories must be empty entries."
                )
            key = relative_path.casefold()
            if key in seen:
                raise _import_error("UNSAFE_ARCHIVE_PATH", "Duplicate normalized ZIP path.")
            seen.add(key)
            if is_directory:
                continue
            entries.append((info, relative_path))
            if info.file_size > self.limits.max_file_bytes:
                raise _import_error("IMPORT_FILE_TOO_LARGE", f"File is too large: {relative_path}.")
            total += info.file_size
            if total > self.limits.max_total_bytes:
                raise _import_error("IMPORT_TOTAL_TOO_LARGE", "Expanded archive is too large.")
            if info.compress_size:
                ratio = info.file_size / info.compress_size
            else:
                ratio = float("inf") if info.file_size else 0
            if ratio > self.limits.max_compression_ratio:
                raise _import_error(
                    "IMPORT_COMPRESSION_RATIO",
                    f"Compression ratio is too high: {relative_path}.",
                )
        return entries

    def _bounded_zip_read(self, archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
        chunks: list[bytes] = []
        actual = 0
        with archive.open(info, "r") as source:
            while chunk := source.read(64 * 1024):
                actual += len(chunk)
                if actual > self.limits.max_file_bytes:
                    raise _import_error(
                        "IMPORT_FILE_TOO_LARGE", f"File is too large: {info.filename}."
                    )
                chunks.append(chunk)
        if actual != info.file_size:
            raise _import_error(
                "IMPORT_ARCHIVE_INVALID",
                "ZIP entry size does not match its directory record.",
            )
        return b"".join(chunks)

    def _write_json(self, draft_dir: Path, record: ImportDraftRecord) -> None:
        destination = draft_dir / "draft.json"
        temporary = draft_dir / f".draft.json.{uuid4().hex}.tmp"
        payload = record.model_dump_json(indent=2).encode("utf-8")
        try:
            with temporary.open("wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            _fsync_directory(draft_dir)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _cleanup_discard_tombstones(self) -> None:
        for candidate in self.root.iterdir():
            match = _DISCARD_TOMBSTONE.fullmatch(candidate.name)
            if match is None or not candidate.is_dir() or candidate.is_symlink():
                continue
            try:
                parsed = UUID(match.group("draft_id"))
            except ValueError:
                continue
            if str(parsed) != match.group("draft_id"):
                continue
            try:
                resolved = candidate.resolve()
                if resolved.parent != self.root:
                    continue
                self._remove_tombstone(resolved)
            except OSError as exc:
                logger.warning(
                    "Could not inspect discarded import draft %s: %s", candidate, exc
                )

    def _cleanup_stale_uploads(self) -> None:
        upload_root = self._verified_upload_root(create=False)
        if upload_root is None:
            return
        try:
            candidates = list(upload_root.iterdir())
        except OSError as exc:
            logger.warning("Could not inspect stale import uploads: %s", exc)
            return

        for candidate in candidates:
            try:
                parsed = UUID(candidate.name)
            except ValueError:
                logger.warning(
                    "Invalid import upload staging entry skipped: %s", candidate.name
                )
                continue
            if candidate.name not in {parsed.hex, str(parsed)}:
                logger.warning(
                    "Invalid import upload staging entry skipped: %s", candidate.name
                )
                continue
            try:
                if not _path_is_reparse_point(candidate):
                    self._verify_upload_child(upload_root, candidate)
                self._remove_upload_child(candidate)
            except RuntimeError:
                logger.warning(
                    "Invalid import upload staging entry skipped: %s", candidate.name
                )
            except OSError as exc:
                logger.warning(
                    "Could not remove stale import upload %s: %s", candidate, exc
                )
        try:
            upload_root.rmdir()
        except OSError:
            pass

    def _verified_upload_root(self, *, create: bool) -> Path | None:
        upload_root = self.root / ".uploads"
        try:
            metadata = upload_root.lstat()
        except FileNotFoundError:
            if not create:
                return None
            try:
                upload_root.mkdir(exist_ok=False)
                metadata = upload_root.lstat()
            except OSError as exc:
                raise RuntimeError("IMPORT_UPLOAD_STAGING_UNSAFE") from exc
        except OSError as exc:
            raise RuntimeError("IMPORT_UPLOAD_STAGING_UNSAFE") from exc
        try:
            if _path_is_reparse_point(upload_root) or not stat.S_ISDIR(metadata.st_mode):
                raise RuntimeError("IMPORT_UPLOAD_STAGING_UNSAFE")
            resolved = upload_root.resolve(strict=True)
        except OSError as exc:
            raise RuntimeError("IMPORT_UPLOAD_STAGING_UNSAFE") from exc
        if resolved != upload_root or resolved.parent != self.root:
            raise RuntimeError("IMPORT_UPLOAD_STAGING_UNSAFE")
        return upload_root

    @staticmethod
    def _verify_upload_child(upload_root: Path, candidate: Path) -> None:
        try:
            metadata = candidate.lstat()
            if not stat.S_ISDIR(metadata.st_mode):
                raise RuntimeError("IMPORT_UPLOAD_STAGING_UNSAFE")
            if _path_is_reparse_point(candidate):
                raise RuntimeError("IMPORT_UPLOAD_STAGING_UNSAFE")
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise RuntimeError("IMPORT_UPLOAD_STAGING_UNSAFE") from exc
        if (
            candidate.parent != upload_root
            or resolved != candidate
            or resolved.parent != upload_root
        ):
            raise RuntimeError("IMPORT_UPLOAD_STAGING_UNSAFE")

    @staticmethod
    def _remove_upload_child(candidate: Path, *, ignore_missing: bool = False) -> None:
        try:
            if candidate.is_symlink():
                candidate.unlink()
            elif _path_is_reparse_point(candidate):
                candidate.rmdir()
            elif candidate.is_dir():
                shutil.rmtree(candidate)
            elif candidate.exists():
                candidate.unlink()
        except FileNotFoundError:
            if not ignore_missing:
                raise

    def _remove_tombstone(self, tombstone: Path) -> None:
        try:
            shutil.rmtree(tombstone)
        except OSError as exc:
            logger.warning(
                "Could not remove discarded import draft %s: %s", tombstone, exc
            )
            return
        _fsync_directory(self.root)

    def _draft_dir(self, draft_id: str) -> Path:
        try:
            parsed = UUID(draft_id)
        except (ValueError, TypeError, AttributeError) as exc:
            raise HTTPException(404, detail={"code": "IMPORT_DRAFT_NOT_FOUND"}) from exc
        if str(parsed) != draft_id:
            raise HTTPException(404, detail={"code": "IMPORT_DRAFT_NOT_FOUND"})
        candidate = self.root / draft_id
        if candidate.parent != self.root:
            raise HTTPException(404, detail={"code": "IMPORT_DRAFT_NOT_FOUND"})
        return candidate

    def _existing_draft_dir(self, draft_id: str) -> Path:
        candidate = self._draft_dir(draft_id)
        if not candidate.is_dir() or candidate.is_symlink():
            raise HTTPException(404, detail={"code": "IMPORT_DRAFT_NOT_FOUND"})
        resolved = candidate.resolve()
        if resolved.parent != self.root:
            raise HTTPException(404, detail={"code": "IMPORT_DRAFT_NOT_FOUND"})
        return resolved

    @staticmethod
    def _raw_destination(raw_dir: Path, relative_path: str) -> Path:
        destination = raw_dir.joinpath(*PurePosixPath(relative_path).parts)
        resolved_parent = destination.parent.resolve()
        if not resolved_parent.is_relative_to(raw_dir.resolve()):
            raise _import_error("UNSAFE_IMPORT_PATH", "Import path escapes the raw directory.")
        return destination
