from datetime import datetime, timezone
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base

JOB_EXTERNAL_ID_MAX_LENGTH = 500
JOB_LOCATION_MAX_LENGTH = 500

def now():
    return datetime.now(timezone.utc)

class Board(Base):
    __tablename__ = "boards"
    id: Mapped[int] = mapped_column(primary_key=True)
    company: Mapped[str] = mapped_column(String(200))
    provider: Mapped[str] = mapped_column(String(32))
    slug: Mapped[str] = mapped_column(String(150))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("provider", "slug", name="uq_board_provider_slug"),)

class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    board_id: Mapped[int] = mapped_column(ForeignKey("boards.id"), index=True)
    external_id: Mapped[str] = mapped_column(String(JOB_EXTERNAL_ID_MAX_LENGTH))
    title: Mapped[str] = mapped_column(String(500))
    location: Mapped[str] = mapped_column(String(JOB_LOCATION_MAX_LENGTH), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    apply_url: Mapped[str] = mapped_column(Text)
    hash: Mapped[str] = mapped_column(String(64))
    is_open: Mapped[bool] = mapped_column(Boolean, default=True)
    missing_count: Mapped[int] = mapped_column(Integer, default=0)
    score: Mapped[int] = mapped_column(Integer, default=0)
    score_reason: Mapped[str] = mapped_column(Text, default="")
    score_method: Mapped[str] = mapped_column(String(32), default="heuristic")
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (UniqueConstraint("board_id", "external_id", name="uq_job_board_external"), Index("idx_jobs_score", "score"))

class JobVersion(Base):
    __tablename__ = "job_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    hash: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(500))
    location: Mapped[str] = mapped_column(String(JOB_LOCATION_MAX_LENGTH))
    description: Mapped[str] = mapped_column(Text)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class Application(Base):
    __tablename__ = "applications"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), unique=True)
    status: Mapped[str] = mapped_column(String(32), default="prepared")
    packet: Mapped[dict] = mapped_column(JSON, default=dict)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

class ApplicationEvent(Base):
    __tablename__ = "application_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(64))
    details: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class Lead(Base):
    __tablename__ = "leads"
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(60))
    title: Mapped[str] = mapped_column(String(250))
    company: Mapped[str] = mapped_column(String(200), default="")
    url: Mapped[str] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class GmailMessage(Base):
    __tablename__ = "gmail_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    provider_message_id: Mapped[str] = mapped_column(String(255), unique=True)
    rfc_message_id: Mapped[str | None] = mapped_column(Text)
    thread_id: Mapped[str] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String(32), default="gmail")
    subject: Mapped[str] = mapped_column(Text, default="")
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    snippet: Mapped[str] = mapped_column(String(500), default="")
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    classification_status: Mapped[str] = mapped_column(String(32), default="pending")

class GmailSyncState(Base):
    __tablename__ = "gmail_sync_state"
    id: Mapped[int] = mapped_column(primary_key=True)
    history_id: Mapped[str] = mapped_column(String(255))

class DiscoveryState(Base):
    __tablename__ = "discovery_state"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class DigestJob(Base):
    __tablename__ = "digest_jobs"
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), primary_key=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
