"""Persistent domain models for projects and story knowledge."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    desc,
)
from sqlalchemy.orm import Mapped, mapped_column

from novel_harness.db.base import Base, utc_now
from novel_harness.db.version_schema import VERSION_PAGE_INDEX_NAME


def new_id() -> str:
    return str(uuid4())


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=utc_now, onupdate=utc_now)


class LibraryRecord:
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)
    purge_after: Mapped[datetime | None] = mapped_column(nullable=True)


class Project(Timestamped, Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(240))
    premise: Mapped[str] = mapped_column(Text, default="")
    genre: Mapped[str] = mapped_column(String(120), default="")
    target_words: Mapped[int] = mapped_column(Integer, default=100_000)
    daily_goal: Mapped[int] = mapped_column(Integer, default=1_500)
    status: Mapped[str] = mapped_column(String(32), default="active")


class ProjectPreparation(Timestamped, Base):
    __tablename__ = "project_preparations"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_project_preparations_revision"),
        CheckConstraint("round >= 0 AND round <= 2", name="ck_project_preparations_round"),
        CheckConstraint(
            "status IN ('not_started','in_progress','completed','skipped')",
            name="ck_project_preparations_status",
        ),
    )

    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    status: Mapped[str] = mapped_column(String(24), default="not_started")
    round: Mapped[int] = mapped_column(Integer, default=0)
    source_hash: Mapped[str] = mapped_column(String(64), default="")
    questions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    generation_job_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    impact_notice: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class Idea(LibraryRecord, Timestamped, Base):
    __tablename__ = "ideas"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(240))
    content: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    source: Mapped[str] = mapped_column(String(240), default="")
    status: Mapped[str] = mapped_column(String(32), default="captured")


class StoryNode(LibraryRecord, Timestamped, Base):
    __tablename__ = "story_nodes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("story_nodes.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(240))
    summary: Mapped[str] = mapped_column(Text, default="")
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    target_words: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="planned")
    pov_entity_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class Entity(LibraryRecord, Timestamped, Base):
    __tablename__ = "entities"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(240), index=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    profile: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class EntityState(Timestamped, Base):
    __tablename__ = "entity_states"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'confirmed', 'retracted')",
            name="ck_entity_states_status",
        ),
        CheckConstraint(
            "legacy_transition IS NULL OR legacy_transition IN ('baseline', 'retire')",
            name="ck_entity_states_legacy_transition",
        ),
        CheckConstraint("revision >= 1", name="ck_entity_states_revision"),
        Index(
            "ix_entity_states_project_entity_created_id",
            "project_id",
            "entity_id",
            "created_at",
            "id",
        ),
        Index("ix_entity_states_entity_status", "entity_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    entity_id: Mapped[str] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), index=True
    )
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    valid_from_node_id: Mapped[str] = mapped_column(ForeignKey("story_nodes.id"))
    valid_to_node_id: Mapped[str | None] = mapped_column(
        ForeignKey("story_nodes.id"), nullable=True
    )
    source_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("chapter_versions.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), default="confirmed", index=True)
    legacy_transition: Mapped[str | None] = mapped_column(String(16), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")


class EntityRelation(LibraryRecord, Timestamped, Base):
    __tablename__ = "entity_relations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    source_entity_id: Mapped[str] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), index=True
    )
    target_entity_id: Mapped[str] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), index=True
    )
    relation_type: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(Text, default="")


class CanonFact(LibraryRecord, Timestamped, Base):
    __tablename__ = "canon_facts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    subject_entity_id: Mapped[str | None] = mapped_column(
        ForeignKey("entities.id"), nullable=True, index=True
    )
    predicate: Mapped[str] = mapped_column(String(120), index=True)
    value: Mapped[Any] = mapped_column(JSON)
    source_note: Mapped[str] = mapped_column(Text, default="")
    source_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    valid_from_node_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    valid_to_node_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="confirmed")


class TimelineEvent(LibraryRecord, Timestamped, Base):
    __tablename__ = "timeline_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    chapter_id: Mapped[str | None] = mapped_column(ForeignKey("story_nodes.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(240))
    story_time: Mapped[str] = mapped_column(String(160), default="")
    sort_key: Mapped[int] = mapped_column(Integer, default=0, index=True)
    description: Mapped[str] = mapped_column(Text, default="")


class PlotThread(LibraryRecord, Timestamped, Base):
    __tablename__ = "plot_threads"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(240))
    promise: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="active")
    start_node_id: Mapped[str | None] = mapped_column(ForeignKey("story_nodes.id"), nullable=True)
    due_node_id: Mapped[str | None] = mapped_column(ForeignKey("story_nodes.id"), nullable=True)
    payoff: Mapped[str] = mapped_column(Text, default="")


class StyleProfile(LibraryRecord, Timestamped, Base):
    __tablename__ = "style_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(160))
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class StyleRule(LibraryRecord, Timestamped, Base):
    __tablename__ = "style_rules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    style_profile_id: Mapped[str | None] = mapped_column(
        ForeignKey("style_profiles.id"), nullable=True
    )
    rule_type: Mapped[str] = mapped_column(String(40), default="preference")
    instruction: Mapped[str] = mapped_column(Text)
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    status: Mapped[str] = mapped_column(String(32), default="confirmed")
    source_feedback_ids: Mapped[list[str]] = mapped_column(JSON, default=list)


class ChapterDocument(Timestamped, Base):
    __tablename__ = "chapter_documents"

    chapter_id: Mapped[str] = mapped_column(
        ForeignKey("story_nodes.id", ondelete="CASCADE"), primary_key=True
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    content: Mapped[str] = mapped_column(Text, default="")
    contract: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    current_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")


class ChapterSummary(LibraryRecord, Timestamped, Base):
    __tablename__ = "chapter_summaries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    chapter_id: Mapped[str] = mapped_column(ForeignKey("story_nodes.id"), index=True)
    version_id: Mapped[str] = mapped_column(ForeignKey("chapter_versions.id"))
    title: Mapped[str] = mapped_column(String(240))
    content_hash: Mapped[str] = mapped_column(String(64))
    recap: Mapped[str] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    origin: Mapped[str] = mapped_column(String(32), default="ai_generated")
    status: Mapped[str] = mapped_column(String(32), default="valid")
    provider: Mapped[str] = mapped_column(String(80), default="")
    supersedes_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class ChapterVersion(Timestamped, Base):
    __tablename__ = "chapter_versions"
    __table_args__ = (
        Index(
            VERSION_PAGE_INDEX_NAME,
            "chapter_id",
            desc("created_at"),
            desc("id"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    chapter_id: Mapped[str] = mapped_column(
        ForeignKey("story_nodes.id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    parent_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("chapter_versions.id"), nullable=True
    )
    restored_from_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("chapter_versions.id"), nullable=True
    )
    generation_job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    content: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, default="")
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(32), default="manual")


class ImportBatch(Timestamped, Base):
    __tablename__ = "import_batches"
    __table_args__ = (
        Index("ix_import_batches_project_status", "project_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    source_kind: Mapped[str] = mapped_column(String(16))
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    analysis_provider_identity: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="imported", index=True)
    completed_units: Mapped[int] = mapped_column(Integer, default=0)
    total_units: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")


class SourceDocument(LibraryRecord, Timestamped, Base):
    __tablename__ = "source_documents"
    __table_args__ = (
        UniqueConstraint("import_batch_id", "relative_path", name="uq_source_documents_batch_path"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    import_batch_id: Mapped[str] = mapped_column(
        ForeignKey("import_batches.id", ondelete="CASCADE"), index=True
    )
    chapter_id: Mapped[str | None] = mapped_column(ForeignKey("story_nodes.id"), nullable=True)
    relative_path: Mapped[str] = mapped_column(Text)
    stored_path: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(240))
    content: Mapped[str] = mapped_column(Text)
    encoding: Mapped[str] = mapped_column(String(24))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    byte_hash: Mapped[str] = mapped_column(String(64))
    content_hash: Mapped[str] = mapped_column(String(64))
    content_revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")


class ProjectContinuation(Timestamped, Base):
    __tablename__ = "project_continuations"

    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    completed_through_node_id: Mapped[str | None] = mapped_column(
        ForeignKey("story_nodes.id"), nullable=True
    )
    current_chapter_id: Mapped[str | None] = mapped_column(
        ForeignKey("story_nodes.id"), nullable=True
    )
    objective: Mapped[str] = mapped_column(Text, default="")
    # Immutable import-source audit IDs. Resolve against live SourceDocument rows
    # before runtime use; this history is intentionally not an FK association.
    source_document_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")


class ImportAnalysisUnit(Timestamped, Base):
    __tablename__ = "import_analysis_units"
    __table_args__ = (
        UniqueConstraint("import_batch_id", "unit_key", name="uq_import_analysis_units_batch_key"),
        Index(
            "ix_import_analysis_units_status_created",
            "status",
            "created_at",
            "id",
        ),
        Index(
            "ix_import_analysis_units_project_status_created",
            "project_id",
            "status",
            "created_at",
            "id",
        ),
        Index(
            "ix_import_analysis_units_batch_status_created",
            "import_batch_id",
            "status",
            "created_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    import_batch_id: Mapped[str] = mapped_column(
        ForeignKey("import_batches.id", ondelete="CASCADE"), index=True
    )
    source_document_id: Mapped[str | None] = mapped_column(
        ForeignKey("source_documents.id"), nullable=True
    )
    chapter_id: Mapped[str | None] = mapped_column(ForeignKey("story_nodes.id"), nullable=True)
    chunk_key: Mapped[str | None] = mapped_column(String(240), nullable=True)
    unit_key: Mapped[str] = mapped_column(String(160))
    kind: Mapped[str] = mapped_column(String(32))
    source_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    worker_epoch: Mapped[str | None] = mapped_column(String(36), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_message: Mapped[str] = mapped_column(Text, default="")
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class MemoryCandidate(Timestamped, Base):
    __tablename__ = "memory_candidates"
    __table_args__ = (
        UniqueConstraint("project_id", "dedupe_key", name="uq_memory_candidates_project_dedupe"),
        Index(
            "ix_memory_candidates_import_analysis_identity",
            "import_batch_id",
            "kind",
            "analysis_identity",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    import_batch_id: Mapped[str] = mapped_column(
        ForeignKey("import_batches.id", ondelete="CASCADE"), index=True
    )
    source_document_id: Mapped[str] = mapped_column(ForeignKey("source_documents.id"))
    chapter_id: Mapped[str | None] = mapped_column(ForeignKey("story_nodes.id"), nullable=True)
    source_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("chapter_versions.id"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(32), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    source_hash: Mapped[str] = mapped_column(String(64))
    dedupe_key: Mapped[str] = mapped_column(String(64))
    analysis_identity: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    conflict: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    promoted_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    promoted_record_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    promotion_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)


class GeneratedMemoryCandidate(Timestamped, Base):
    """Evidence-backed proposal from accepted chapter text.

    Import analysis keeps using ``MemoryCandidate`` above.  Generated proposals
    have different source and evidence invariants, so their additive table does
    not weaken the established import schema.
    """

    __tablename__ = "generated_memory_candidates"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "identity_hash",
            name="uq_generated_memory_candidates_project_identity",
        ),
        CheckConstraint(
            "kind IN ('canon', 'entity_state', 'timeline', 'plot')",
            name="ck_generated_memory_candidates_kind",
        ),
        CheckConstraint(
            "status IN ('pending', 'confirmed', 'rejected')",
            name="ck_generated_memory_candidates_status",
        ),
        CheckConstraint("revision >= 1", name="ck_generated_memory_candidates_revision"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    chapter_id: Mapped[str | None] = mapped_column(ForeignKey("story_nodes.id"), nullable=True)
    source_job_id: Mapped[str | None] = mapped_column(ForeignKey("ai_jobs.id"), nullable=True)
    source_summary_id: Mapped[str | None] = mapped_column(
        ForeignKey("chapter_summaries.id"), nullable=True
    )
    source_version_id: Mapped[str] = mapped_column(ForeignKey("chapter_versions.id"))
    kind: Mapped[str] = mapped_column(String(32), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON)
    identity_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    promoted_record_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    promotion_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AIJob(Timestamped, Base):
    __tablename__ = "ai_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    chapter_id: Mapped[str | None] = mapped_column(ForeignKey("story_nodes.id"), nullable=True)
    task_type: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    prompt_version: Mapped[str] = mapped_column(String(80))
    instructions: Mapped[str] = mapped_column(Text, default="")
    token_budget: Mapped[int] = mapped_column(Integer, default=12_000)
    context_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retryable: Mapped[bool] = mapped_column(Boolean, default=False)
    accepted_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class GenerationArtifact(Timestamped, Base):
    __tablename__ = "generation_artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("ai_jobs.id", ondelete="CASCADE"), index=True)
    stage: Mapped[str] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(32))
    content: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Feedback(Timestamped, Base):
    __tablename__ = "feedback"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    chapter_id: Mapped[str | None] = mapped_column(ForeignKey("story_nodes.id"), nullable=True)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("ai_jobs.id"), nullable=True)
    rating: Mapped[int] = mapped_column(Integer)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    original_text: Mapped[str] = mapped_column(Text, default="")
    corrected_text: Mapped[str] = mapped_column(Text, default="")
    comment: Mapped[str] = mapped_column(Text, default="")


class PreferenceCandidate(Timestamped, Base):
    __tablename__ = "preference_candidates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    category: Mapped[str] = mapped_column(String(80), default="general")
    instruction: Mapped[str] = mapped_column(Text)
    diff_summary: Mapped[str] = mapped_column(Text, default="")
    source_feedback_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="candidate")
    linked_style_rule_id: Mapped[str | None] = mapped_column(
        ForeignKey("style_rules.id"), nullable=True
    )


class Conflict(Timestamped, Base):
    __tablename__ = "conflicts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    chapter_id: Mapped[str] = mapped_column(
        ForeignKey("story_nodes.id", ondelete="CASCADE"), index=True
    )
    code: Mapped[str] = mapped_column(String(80), index=True)
    severity: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text)
    evidence: Mapped[list[str]] = mapped_column(JSON, default=list)
    related_entity_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="open")
    selected_option_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    decision_note: Mapped[str] = mapped_column(Text, default="")


class ConflictOption(Timestamped, Base):
    __tablename__ = "conflict_options"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conflict_id: Mapped[str] = mapped_column(
        ForeignKey("conflicts.id", ondelete="CASCADE"), index=True
    )
    mode: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text)
    benefit: Mapped[str] = mapped_column(Text)
    risk: Mapped[str] = mapped_column(Text)
    ripple_effects: Mapped[list[str]] = mapped_column(JSON, default=list)
    affected_entity_ids: Mapped[list[str]] = mapped_column(JSON, default=list)


class Asset(LibraryRecord, Timestamped, Base):
    __tablename__ = "assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    entity_id: Mapped[str | None] = mapped_column(ForeignKey("entities.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    prompt: Mapped[str] = mapped_column(Text)
    size: Mapped[str] = mapped_column(String(32), default="1024x1024")
    relative_path: Mapped[str] = mapped_column(Text, default="")
    mime_type: Mapped[str] = mapped_column(String(80), default="image/png")
    provider: Mapped[str] = mapped_column(String(80), default="")
    model: Mapped[str] = mapped_column(String(120), default="")
    status: Mapped[str] = mapped_column(String(32), default="pending")
    error_message: Mapped[str] = mapped_column(Text, default="")


class ProgressGoal(Timestamped, Base):
    __tablename__ = "progress_goals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(40), default="daily_words")
    target_value: Mapped[int] = mapped_column(Integer)
    due_date: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active")


class ActivityEvent(Timestamped, Base):
    __tablename__ = "activity_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(60))
    entity_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    value: Mapped[int] = mapped_column(Integer, default=0)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
