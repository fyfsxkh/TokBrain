"""Database models for TokBrain's local, user-initiated import workflow."""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class AppSetting(Base):
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class SecretRecord(Base):
    __tablename__ = "secret_records"
    name: Mapped[str] = mapped_column(String(100), primary_key=True)
    encrypted_value: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Collection(Base):
    """A static local label. Historical Douyin folders are never refreshed."""

    __tablename__ = "collections"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(300))
    cover_url: Mapped[str | None] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Work(Base):
    __tablename__ = "works"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    platform: Mapped[str] = mapped_column(String(20), default="douyin", index=True)
    platform_work_id: Mapped[str] = mapped_column(String(100), index=True)
    kind: Mapped[str] = mapped_column(String(20), default="video")
    title: Mapped[str] = mapped_column(String(500), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    author_id: Mapped[str | None] = mapped_column(String(120))
    author_name: Mapped[str | None] = mapped_column(String(200))
    duration_seconds: Mapped[float] = mapped_column(Float, default=0)
    cover_url: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    media_urls: Mapped[list] = mapped_column(JSON, default=list)
    image_urls: Mapped[list] = mapped_column(JSON, default=list)
    raw_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    content_text: Mapped[str] = mapped_column(Text, default="")
    library_state: Mapped[str] = mapped_column(
        String(20), default="pending", index=True
    )
    processing_state: Mapped[str] = mapped_column(
        String(30), default="discovered", index=True
    )
    process_error: Mapped[str | None] = mapped_column(Text)
    last_error_code: Mapped[str | None] = mapped_column(String(50), index=True)
    process_attempts: Mapped[int] = mapped_column(Integer, default=0)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    __table_args__ = (
        UniqueConstraint("platform", "platform_work_id", name="uq_work_platform_id"),
    )


class CollectionMembership(Base):
    __tablename__ = "collection_memberships"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    collection_id: Mapped[int] = mapped_column(
        ForeignKey("collections.id", ondelete="CASCADE"), index=True
    )
    work_id: Mapped[int] = mapped_column(
        ForeignKey("works.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        UniqueConstraint("collection_id", "work_id", name="uq_membership"),
    )


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_type: Mapped[str] = mapped_column(String(30), index=True)
    state: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    scope: Mapped[dict] = mapped_column(JSON, default=dict)
    progress: Mapped[dict] = mapped_column(JSON, default=dict)
    total_items: Mapped[int] = mapped_column(Integer, default=0)
    processed_items: Mapped[int] = mapped_column(Integer, default=0)
    failed_items: Mapped[int] = mapped_column(Integer, default=0)
    cancelled_items: Mapped[int] = mapped_column(Integer, default=0)
    deferred_items: Mapped[int] = mapped_column(Integer, default=0)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ImportBatch(Base):
    __tablename__ = "import_batches"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), unique=True, index=True
    )
    raw_input: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    total_items: Mapped[int] = mapped_column(Integer, default=0)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    error_code: Mapped[str | None] = mapped_column(String(50))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ImportItem(Base):
    __tablename__ = "import_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(
        ForeignKey("import_batches.id", ondelete="CASCADE"), index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    input_url: Mapped[str] = mapped_column(Text)
    normalized_url: Mapped[str] = mapped_column(Text)
    canonical_url: Mapped[str | None] = mapped_column(Text)
    platform_work_id: Mapped[str | None] = mapped_column(String(100), index=True)
    kind: Mapped[str | None] = mapped_column(String(20))
    title: Mapped[str | None] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    author_id: Mapped[str | None] = mapped_column(String(120))
    author_name: Mapped[str | None] = mapped_column(String(200))
    duration_seconds: Mapped[float] = mapped_column(Float, default=0)
    cover_url: Mapped[str | None] = mapped_column(Text)
    media_urls: Mapped[list] = mapped_column(JSON, default=list)
    image_urls: Mapped[list] = mapped_column(JSON, default=list)
    raw_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    error_code: Mapped[str | None] = mapped_column(String(50), index=True)
    error_message: Mapped[str | None] = mapped_column(Text)
    existing_work_id: Mapped[int | None] = mapped_column(
        ForeignKey("works.id", ondelete="SET NULL")
    )
    worker_id: Mapped[int | None] = mapped_column(Integer)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class DailyLinkQuota(Base):
    __tablename__ = "daily_link_quotas"
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    attempted: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class WorkSourceAsset(Base):
    __tablename__ = "work_source_assets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    import_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("import_items.id", ondelete="SET NULL"), index=True
    )
    work_id: Mapped[int | None] = mapped_column(
        ForeignKey("works.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(20))
    path: Mapped[str] = mapped_column(Text)
    mime_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UsageEvent(Base):
    __tablename__ = "usage_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str | None] = mapped_column(String(36), index=True)
    work_id: Mapped[int | None] = mapped_column(Integer, index=True)
    provider: Mapped[str] = mapped_column(String(50), default="dashscope")
    model: Mapped[str] = mapped_column(String(100))
    metric: Mapped[str] = mapped_column(String(50))
    quantity: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(30))
    estimated_cost_cny: Mapped[float] = mapped_column(Float, default=0)
    price_version: Mapped[str] = mapped_column(String(30), default="manual")
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class DailyBudget(Base):
    __tablename__ = "daily_budgets"
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    works_reserved: Mapped[int] = mapped_column(Integer, default=0)
    works_used: Mapped[int] = mapped_column(Integer, default=0)
    media_minutes_reserved: Mapped[float] = mapped_column(Float, default=0)
    media_minutes_used: Mapped[float] = mapped_column(Float, default=0)
    llm_tokens_reserved: Mapped[int] = mapped_column(Integer, default=0)
    llm_tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_cny: Mapped[float] = mapped_column(Float, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Keyframe(Base):
    __tablename__ = "keyframes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    work_id: Mapped[int] = mapped_column(
        ForeignKey("works.id", ondelete="CASCADE"), index=True
    )
    timestamp_seconds: Mapped[float] = mapped_column(Float)
    scene_score: Mapped[float] = mapped_column(Float, default=0)
    path: Mapped[str] = mapped_column(Text)
    perceptual_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    work_id: Mapped[int] = mapped_column(
        ForeignKey("works.id", ondelete="CASCADE"), index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer)
    source_kind: Mapped[str] = mapped_column(String(30), default="metadata")
    text: Mapped[str] = mapped_column(Text)
    start_seconds: Mapped[float | None] = mapped_column(Float)
    end_seconds: Mapped[float | None] = mapped_column(Float)
    embedding: Mapped[list | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        UniqueConstraint("work_id", "chunk_index", name="uq_work_chunk"),
    )


class WorkSummary(Base):
    __tablename__ = "work_summaries"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    work_id: Mapped[int] = mapped_column(
        ForeignKey("works.id", ondelete="CASCADE"), unique=True, index=True
    )
    status: Mapped[str] = mapped_column(String(20), default="ready", index=True)
    one_sentence: Mapped[str] = mapped_column(Text, default="")
    content_json: Mapped[dict] = mapped_column(JSON, default=dict)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    asset_ids: Mapped[list] = mapped_column(JSON, default=list)
    model: Mapped[str] = mapped_column(String(100), default="")
    prompt_version: Mapped[str] = mapped_column(String(30), default="summary-v1")
    source_digest: Mapped[str] = mapped_column(String(64), default="")
    error: Mapped[str | None] = mapped_column(Text)
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
