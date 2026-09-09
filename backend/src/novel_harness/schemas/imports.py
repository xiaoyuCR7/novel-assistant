"""Contracts for importing an existing manuscript into a project Vault."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from novel_harness.schemas.common import ORMModel
from novel_harness.schemas.projects import ProjectRead

ImportCategory = Literal[
    "manuscript",
    "task",
    "outline",
    "world",
    "character",
    "style",
    "other",
]
ImportBatchStatus = Literal[
    "imported",
    "analyzing",
    "paused",
    "partially_analyzed",
    "analyzed",
    "analysis_failed",
]
ImportAnalysisStatus = Literal["queued", "running", "succeeded", "failed"]
MemoryCandidateKind = Literal[
    "entity",
    "relation",
    "canon",
    "timeline",
    "plot",
    "node",
    "node_update",
    "style_rule",
    "idea",
    "entity_state",
    "chapter_summary",
]
MemoryCandidateStatus = Literal["pending", "confirmed", "rejected", "conflict"]
MemoryCandidateDecisionValue = Literal["confirm", "reject"]
ImportSourceKind = Literal["folder", "zip"]

PathText = Annotated[str, Field(min_length=1, max_length=4096)]
TitleText = Annotated[str, Field(min_length=1, max_length=240)]
Identifier = Annotated[str, Field(min_length=1, max_length=64)]
HashText = Annotated[str, Field(min_length=64, max_length=64)]
JsonKey = Annotated[str, Field(min_length=1, max_length=160)]
JsonText = Annotated[str, Field(max_length=16_000)]
JsonInteger = Annotated[int, Field(strict=True, ge=-(2**63), le=2**63 - 1)]


def _require_float_leaf(value: object) -> object:
    if type(value) is not float:
        raise ValueError("JSON float leaves must be floats")
    return value


JsonFloat = Annotated[
    float,
    BeforeValidator(_require_float_leaf),
    Field(ge=-1e308, le=1e308, allow_inf_nan=False),
]
type BoundedJson = (
    JsonText
    | bool
    | JsonInteger
    | JsonFloat
    | None
    | Annotated[list[BoundedJson], Field(max_length=200)]
    | Annotated[dict[JsonKey, BoundedJson], Field(max_length=64)]
)
Payload = Annotated[dict[JsonKey, BoundedJson], Field(max_length=64)]
EvidenceItem = Annotated[dict[JsonKey, BoundedJson], Field(max_length=20)]
Evidence = Annotated[list[EvidenceItem], Field(max_length=200)]
NonNegativeInt = Annotated[int, Field(ge=0)]
StrictNonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
StrictRevision = Annotated[int, Field(strict=True, ge=1)]
StrictBool = Annotated[bool, Field(strict=True)]
StrictPositiveInt = Annotated[int, Field(strict=True, ge=1)]


class ImportLimitsRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_files: StrictPositiveInt
    max_file_bytes: StrictPositiveInt
    max_total_bytes: StrictPositiveInt
    max_compression_ratio: StrictPositiveInt


class ImportFilePreview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relative_path: PathText
    audit_id: str = Field(default="", max_length=64)
    category: ImportCategory
    title: TitleText
    encoding: str = Field(default="utf-8", min_length=1, max_length=24)
    size_bytes: StrictNonNegativeInt = 0
    byte_hash: str = Field(default="", max_length=64)
    content_hash: str = Field(default="", max_length=64)
    selected: StrictBool = True
    warning: str = Field(default="", max_length=2000)
    content_preview: str = Field(default="", max_length=4000)


class ImportChapterPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_document_id: str | None = Field(default=None, min_length=1, max_length=64)
    draft_chapter_id: str = Field(default="", max_length=64)
    relative_path: PathText
    title: TitleText
    order_index: StrictNonNegativeInt
    existing_chapter_id: str | None = Field(default=None, min_length=1, max_length=64)
    content_preview: str = Field(default="", max_length=4000)


class ImportContinuation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmed: StrictBool = False
    completed_through_node_id: str | None = Field(default=None, min_length=1, max_length=64)
    current_chapter_id: str | None = Field(default=None, min_length=1, max_length=64)
    objective: str = Field(default="", max_length=16_000)
    source_document_ids: list[Identifier] = Field(
        default_factory=list,
        max_length=10_000,
        description=(
            "Immutable import-source audit identifiers; resolve them against live "
            "source documents before runtime use."
        ),
    )
    revision: StrictRevision = 1


class ImportDraftRead(BaseModel):
    draft_id: Identifier
    title: TitleText
    source_kind: ImportSourceKind
    manifest: Payload = Field(default_factory=dict)
    files: list[ImportFilePreview] = Field(default_factory=list, max_length=10_000)
    chapters: list[ImportChapterPreview] = Field(default_factory=list, max_length=10_000)
    continuation: ImportContinuation = Field(default_factory=ImportContinuation)
    objective: str = Field(default="", max_length=16_000)
    revision: int = Field(ge=1)


class ImportDraftPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: StrictRevision
    title: TitleText | None = None
    files: list[ImportFilePreview] | None = Field(default=None, max_length=10_000)
    chapters: list[ImportChapterPreview] | None = Field(default=None, max_length=10_000)
    continuation: ImportContinuation | None = None
    objective: str | None = Field(default=None, max_length=16_000)

    @field_validator("title")
    @classmethod
    def require_nonblank_title(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("project title must not be blank")
        return value


class ImportCommitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: StrictRevision


class ImportCommitResponse(BaseModel):
    project: ProjectRead
    import_batch_id: Identifier


class ImportBatchRead(ORMModel):
    id: Identifier
    project_id: Identifier
    source_kind: ImportSourceKind
    manifest: Payload
    status: ImportBatchStatus
    completed_units: NonNegativeInt
    total_units: NonNegativeInt
    last_error: str = Field(max_length=4000)
    revision: int = Field(default=1, ge=1)
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_status(cls, value):
        if isinstance(value, dict):
            value = dict(value)
            value["status"] = {
                "queued": "analyzing",
                "completed": "analyzed",
                "failed": "analysis_failed",
            }.get(value.get("status"), value.get("status"))
        return value


class ImportAnalysisRead(ORMModel):
    id: Identifier
    project_id: Identifier
    import_batch_id: Identifier
    source_document_id: Identifier | None
    chapter_id: Identifier | None
    chunk_key: Annotated[str, Field(min_length=1, max_length=240)] | None
    unit_key: Annotated[str, Field(min_length=1, max_length=160)]
    kind: Annotated[str, Field(min_length=1, max_length=32)]
    source_hash: HashText
    status: ImportAnalysisStatus
    attempt_count: NonNegativeInt
    worker_epoch: Annotated[str, Field(min_length=1, max_length=36)] | None
    error_code: Annotated[str, Field(min_length=1, max_length=80)] | None
    error_message: str = Field(max_length=4000)
    result: Payload
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_status(cls, value):
        if isinstance(value, dict):
            value = dict(value)
            value["status"] = {
                "completed": "succeeded",
                "paused": "queued",
            }.get(value.get("status"), value.get("status"))
        return value


class ImportAnalysisAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: StrictRevision
    adopt_current_provider: StrictBool = False


class ImportAnalysisProgress(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: NonNegativeInt
    completed: NonNegativeInt
    failed: NonNegativeInt
    queued: NonNegativeInt
    running: NonNegativeInt


class ImportAnalysisStatusRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: Identifier
    project_id: Identifier
    status: ImportBatchStatus
    revision: StrictRevision
    progress: ImportAnalysisProgress
    current_unit: ImportAnalysisRead | None = None
    last_error: str = Field(default="", max_length=4000)
    created_at: datetime
    updated_at: datetime


class MemoryCandidateRead(ORMModel):
    id: Identifier
    project_id: Identifier
    import_batch_id: Identifier
    source_document_id: Identifier
    chapter_id: Identifier | None
    source_version_id: Identifier | None
    kind: MemoryCandidateKind
    payload: Payload
    evidence: Evidence
    source_hash: HashText
    dedupe_key: HashText
    status: MemoryCandidateStatus
    revision: int = Field(ge=1)
    conflict: Payload
    promoted_type: Annotated[str, Field(min_length=1, max_length=32)] | None
    promoted_record_id: Identifier | None
    promotion_fingerprint: HashText | None = None
    created_at: datetime
    updated_at: datetime


class MemoryCandidatePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: StrictRevision
    kind: MemoryCandidateKind | None = None
    payload: Payload | None = None
    evidence: Evidence | None = None

    @model_validator(mode="after")
    def requires_nonnull_edit(self):
        editable = {"kind", "payload", "evidence"}
        provided = self.model_fields_set & editable
        if not provided or any(getattr(self, field) is None for field in provided):
            raise ValueError("a non-null candidate edit is required")
        return self


class MemoryCandidateDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: StrictRevision
    decision: MemoryCandidateDecisionValue | None = None
    resolution: Literal["create_separate"] | None = None


class MemoryCandidateBulkConfirmEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: Identifier
    revision: StrictRevision


class MemoryCandidateBulkConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: list[MemoryCandidateBulkConfirmEntry] = Field(min_length=1, max_length=100)


class MemoryCandidatePage(BaseModel):
    items: list[MemoryCandidateRead] = Field(max_length=100)
    total: int = Field(ge=0)
    counts: dict[MemoryCandidateStatus, NonNegativeInt] = Field(default_factory=dict, max_length=4)
    next_cursor: str | None = Field(default=None, max_length=4096)
