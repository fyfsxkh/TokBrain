"""Public API schemas for the local import application."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class SettingsView(BaseModel):
    daily_media_minutes_limit: float
    daily_llm_token_limit: int
    monthly_warning_cny: float
    scene_threshold: float
    max_scene_candidates: int
    max_keyframes: int
    min_keyframe_gap_seconds: float
    dpapi_warning: str
    has_dashscope_key: bool
    has_bss_credentials: bool
    has_f2_cookie: bool = False
    security_cleanup_required: bool = False
    security_cleanup_message: str = ""
    default_answer_format: Literal["rich", "markdown", "plain"] = "rich"
    summary_prompt: str
    default_summary_prompt: str
    processing_model: str
    chat_fast_model: str
    chat_deep_model: str
    processing_model_options: list[str] = Field(default_factory=list)
    chat_model_options: list[str] = Field(default_factory=list)
    ocr_model: str
    asr_model: str
    embedding_model: str
    import_batch_limit: int = 10
    import_daily_limit: int = 150
    import_worker_count: int = 3
    import_network_concurrency: int = 1
    import_cooldown_min_seconds: int = 4
    import_cooldown_max_seconds: int = 8


class SettingsUpdate(BaseModel):
    daily_media_minutes_limit: float | None = Field(default=None, ge=1, le=100000)
    daily_llm_token_limit: int | None = Field(default=None, ge=1000)
    monthly_warning_cny: float | None = Field(default=None, ge=0)
    scene_threshold: float | None = Field(default=None, ge=0.05, le=0.95)
    max_scene_candidates: int | None = Field(default=None, ge=12, le=1000)
    max_keyframes: int | None = Field(default=None, ge=1, le=48)
    min_keyframe_gap_seconds: float | None = Field(default=None, ge=0.2, le=60)
    dashscope_api_key: str | None = Field(default=None, max_length=500)
    bss_access_key_id: str | None = Field(default=None, max_length=200)
    bss_access_key_secret: str | None = Field(default=None, max_length=500)
    f2_cookie: str | None = Field(default=None, max_length=20000)
    clear_f2_cookie: bool = False
    default_answer_format: Literal["rich", "markdown", "plain"] | None = None
    summary_prompt: str | None = Field(default=None, min_length=1, max_length=12000)
    processing_model: str | None = Field(default=None, min_length=1, max_length=100)
    chat_fast_model: str | None = Field(default=None, min_length=1, max_length=100)
    chat_deep_model: str | None = Field(default=None, min_length=1, max_length=100)


class SystemProbe(BaseModel):
    probe: str
    status: Literal["healthy", "degraded", "down"]
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class SystemHealth(BaseModel):
    overall: Literal["healthy", "degraded", "down"]
    summary: str
    checked_at: datetime
    probes: list[SystemProbe]


class UsageSummary(BaseModel):
    month_estimated_cny: float
    official_billed_cny: float | None = None
    official_data_as_of: datetime | None = None
    official_status: str
    daily_works_used: int
    daily_works_reserved: int
    daily_links_used: int
    daily_links_limit: int
    daily_media_minutes_used: float
    daily_media_minutes_reserved: float
    daily_media_minutes_limit: float
    daily_llm_tokens_used: int
    daily_llm_tokens_reserved: int
    daily_llm_tokens_limit: int
    warning_reached: bool
    estimate_notice: str


class ImportBatchCreate(BaseModel):
    text: str = Field(min_length=1, max_length=100000)


class ImportSelection(BaseModel):
    item_id: int
    collection_id: int


class ImportConfirm(BaseModel):
    # Kept for compatibility. Items without an explicit collection use 手动导入.
    item_ids: list[int] = Field(default_factory=list, max_length=10)
    items: list[ImportSelection] = Field(default_factory=list, max_length=10)


class JobView(BaseModel):
    id: str
    job_type: str
    state: Literal[
        "queued",
        "running",
        "cancelling",
        "cancelled",
        "succeeded",
        "partial",
        "failed",
    ]
    message: str
    total_items: int
    processed_items: int
    failed_items: int
    cancelled_items: int
    deferred_items: int
    cancel_requested: bool
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    progress: dict[str, Any] = Field(default_factory=dict)


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=12000)


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=8, ge=1, le=20)
    history: list[ChatTurn] = Field(default_factory=list, max_length=40)
    mode: Literal["fast", "deep"] = "fast"


class ChatSource(BaseModel):
    work_id: int
    platform_work_id: str
    title: str
    collection: str | None = None
    timestamp_seconds: float | None = None
    external_url: str | None = None
    source_kind: str


class ChatAnswer(BaseModel):
    answer: str
    sources: list[ChatSource]


class SummaryCreate(BaseModel):
    work_ids: list[int] = Field(min_length=1, max_length=1000)


class CollectionCreate(BaseModel):
    title: str = Field(min_length=1, max_length=100)


class CollectionUpdate(BaseModel):
    summary_prompt: str | None = Field(default=None, max_length=12000)


class CollectionAssignment(BaseModel):
    work_ids: list[int] = Field(min_length=1, max_length=1000)


class IngestCreate(BaseModel):
    work_ids: list[int] = Field(min_length=1, max_length=1000)


class RetryBatchRequest(BaseModel):
    work_ids: list[int] | None = Field(default=None, max_length=1000)
    error_code: str | None = Field(default=None, max_length=50)
    collection_id: int | None = None


class ObsidianManifestRequest(BaseModel):
    work_ids: list[int] = Field(min_length=1, max_length=1000)
