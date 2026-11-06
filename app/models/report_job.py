"""Persistent report work claimed and updated by the report worker."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ReportJob(Base):
    __tablename__ = "report_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'generating', 'retrying', 'ready', 'failed', 'incomplete')",
            name="ck_report_jobs_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_report_jobs_attempt_count"),
        Index("ix_report_jobs_status_next_attempt", "status", "next_attempt_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    # One job per interview, matching the existing one-report-per-interview rule.
    # Ownership is determined through interview.user_id.
    interview_id: Mapped[str] = mapped_column(
        String, ForeignKey("interviews.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    status: Mapped[str] = mapped_column(String, default="queued", server_default="queued", nullable=False)
    stage: Mapped[str | None] = mapped_column(String, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)

    # Capture the inputs when the job is queued so later interview edits cannot
    # change what a retry evaluates. A completed AI response can be checkpointed
    # separately before creating the final report.
    input_snapshot: Mapped[dict] = mapped_column(JSON(none_as_null=True), nullable=False)
    evaluation_result: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, server_default=func.now(), nullable=False
    )

    # The worker reserves the job with a fresh token and expiry. The
    # token lets it reject stale writes after another worker recovers the job.
    lease_token: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now,
        server_default=func.now(), nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    interview: Mapped["Interview"] = relationship(back_populates="report_job")
