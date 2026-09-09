"""Per-Vault durable task control; never indexed as narrative memory."""

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from novel_harness.db.base import Base
from novel_harness.db.models import Timestamped


class AIJobControl(Timestamped, Base):
    __tablename__ = "ai_job_controls"
    __table_args__ = (UniqueConstraint("operation", "idempotency_key"),)

    job_id: Mapped[str] = mapped_column(
        ForeignKey("ai_jobs.id", ondelete="CASCADE"), primary_key=True
    )
    operation: Mapped[str] = mapped_column(String(40))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    command: Mapped[dict] = mapped_column(JSON)
    source_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    provider_identity: Mapped[dict] = mapped_column(JSON, default=dict)
    embedding_identity: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    control_revision: Mapped[int] = mapped_column(Integer, default=1)
    format_version: Mapped[int] = mapped_column(Integer, default=1)
    worker_epoch: Mapped[str | None] = mapped_column(String(36), nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    context_ready: Mapped[bool] = mapped_column(Boolean, default=False)
    recovery_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    authorized_stage: Mapped[str | None] = mapped_column(String(128), nullable=True)
    authorized_attempt_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    effects: Mapped[dict] = mapped_column(JSON, default=dict)


class AIJobStageAttempt(Timestamped, Base):
    __tablename__ = "ai_job_stage_attempts"

    job_id: Mapped[str] = mapped_column(
        ForeignKey("ai_jobs.id", ondelete="CASCADE"), primary_key=True
    )
    stage_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    attempt_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(24))
    input_hash: Mapped[str] = mapped_column(String(64))
    input_payload: Mapped[dict] = mapped_column(JSON)
    artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("generation_artifacts.id"), nullable=True
    )
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(24), nullable=True)


class AIJobActionReceipt(Timestamped, Base):
    __tablename__ = "ai_job_action_receipts"

    job_id: Mapped[str] = mapped_column(
        ForeignKey("ai_jobs.id", ondelete="CASCADE"), primary_key=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict] = mapped_column(JSON)


class JobSchemaVersion(Base):
    __tablename__ = "job_schema_versions"

    feature: Mapped[str] = mapped_column(String(40), primary_key=True)
    version: Mapped[int] = mapped_column(Integer)
