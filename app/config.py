"""Deployment settings and filesystem locations for TokBrain."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR.joinpath("data")
_LOCAL_FRONTENDS = ("http://127.0.0.1:3000", "http://localhost:3000")
_DATA_SUBDIRECTORIES = ("media", "keyframes", "tmp", "source-assets", "package-imports")


class ApplicationSettings(BaseSettings):
    """Values that may be supplied by the local deployment environment."""

    model_config = SettingsConfigDict(
        env_file=BASE_DIR.joinpath(".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "TokBrain"
    app_host: Literal["127.0.0.1", "localhost"] = "127.0.0.1"
    app_port: int = 8000
    debug: bool = False
    database_url: str = (
        "sqlite+aiosqlite:///" + DATA_DIR.joinpath("douyin_rag.db").as_posix()
    )
    frontend_origins: str = ",".join(_LOCAL_FRONTENDS)

    daily_media_minutes_limit: float = Field(60.0, ge=1.0, le=100_000.0)
    daily_llm_token_limit: int = Field(500_000, ge=1_000)
    monthly_warning_cny: float = Field(50.0, ge=0)
    max_work_duration_seconds: int = Field(7_200, ge=30)
    max_download_megabytes: int = Field(1_024, ge=10)
    max_temp_megabytes: int = Field(2_048, ge=100)

    scene_threshold: float = Field(0.40, ge=0.05, le=0.95)
    max_scene_candidates: int = Field(120, ge=12, le=1_000)
    max_keyframes: int = Field(12, ge=1, le=48)
    ocr_concurrency: int = Field(3, ge=1, le=6)
    min_keyframe_gap_seconds: float = Field(2.0, ge=0.2, le=60.0)
    keyframe_analysis_width: int = Field(480, ge=160, le=1_920)

    dashscope_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    asr_model: str = "paraformer-v2"
    ocr_model: str = "qwen3.5-ocr"
    enrichment_model: str = "qwen3.6-flash"
    chat_model: str = "qwen3.7-plus"
    embedding_model: str = "text-embedding-v4"
    rerank_model: str = "qwen3-rerank"
    embedding_dimensions: int = 1_024

    @property
    def allowed_origins(self) -> list[str]:
        return list(
            dict.fromkeys(
                filter(None, map(str.strip, self.frontend_origins.split(",")))
            )
        )


def _runtime_directories() -> tuple[Path, ...]:
    managed_data = tuple(DATA_DIR.joinpath(name) for name in _DATA_SUBDIRECTORIES)
    return (DATA_DIR, *managed_data)


def ensure_directories() -> None:
    """Create only the mutable directories used by the local application."""

    for directory in _runtime_directories():
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)


settings = ApplicationSettings()

DPAPI_WARNING = (
    "模型密钥和可选账单密钥已安全加密，并绑定当前 Windows 用户。"
    "重装系统或更换 Windows 用户后将无法读取；知识库仍会保留，"
    "但需要重新输入密钥。可选解析 Cookie 与模型密钥均只在当前 Windows 用户下加密保存。"
)
