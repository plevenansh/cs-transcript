from __future__ import annotations

from datetime import datetime, timezone
from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.database import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TranscriptCache(Base):
    __tablename__ = "transcript_cache"
    __table_args__ = (
        UniqueConstraint("video_id", "language_request", name="uq_transcript_video_language_request"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    video_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    language_request: Mapped[str] = mapped_column(String(255), nullable=False)
    language: Mapped[str] = mapped_column(String(255), nullable=False)
    language_code: Mapped[str] = mapped_column(String(32), nullable=False)
    is_generated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    segments: Mapped[list[dict]] = mapped_column(JSON, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    source_status: Mapped[str] = mapped_column(String(64), default="ok", nullable=False)
    error_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )


class ApiRequestLog(Base):
    __tablename__ = "api_request_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    method: Mapped[str] = mapped_column(String(16), nullable=False)
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    query: Mapped[str] = mapped_column(Text, default="", nullable=False)
    client_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    client_host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
