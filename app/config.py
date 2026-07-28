"""Application configuration for the Windows-first local TokBrain service."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "TokBrain"
    app_host: Literal["127.0.0.1", "localhost"] = "127.0.0.1"
    app_port: int = 8000
    debug: bool = False
    database_url: str = f"sqlite+aiosqlite:///{(DATA_DIR / 'douyin_rag.db').as_posix()}"
    frontend_origins: str = "http://127.0.0.1:3000,http://localhost:3000"

    daily_media_minutes_limit: float = Field(default=60.0, ge=1.0, le=100000.0)
    daily_llm_token_limit: int = Field(default=500_000, ge=1000)
    monthly_warning_cny: float = Field(default=50.0, ge=0)
    max_work_duration_seconds: int = Field(default=7200, ge=30)
    max_download_megabytes: int = Field(default=1024, ge=10)
    max_temp_megabytes: int = Field(default=2048, ge=100)

    scene_threshold: float = Field(default=0.40, ge=0.05, le=0.95)
    max_scene_candidates: int = Field(default=120, ge=12, le=1000)
    max_keyframes: int = Field(default=12, ge=1, le=48)
    ocr_concurrency: int = Field(default=3, ge=1, le=6)
    min_keyframe_gap_seconds: float = Field(default=2.0, ge=0.2, le=60.0)
    keyframe_analysis_width: int = Field(default=480, ge=160, le=1920)

    dashscope_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    asr_model: str = "paraformer-v2"
    ocr_model: str = "qwen3.5-ocr"
    enrichment_model: str = "qwen3.6-flash"
    chat_model: str = "qwen3.7-plus"
    embedding_model: str = "text-embedding-v4"
    rerank_model: str = "qwen3-rerank"
    embedding_dimensions: int = 1024

    @property
    def allowed_origins(self) -> list[str]:
        return [
            item.strip() for item in self.frontend_origins.split(",") if item.strip()
        ]


settings = Settings()


def ensure_directories() -> None:
    for path in (
        DATA_DIR,
        DATA_DIR / "media",
        DATA_DIR / "keyframes",
        DATA_DIR / "tmp",
        DATA_DIR / "source-assets",
        BASE_DIR / "logs",
    ):
        path.mkdir(parents=True, exist_ok=True)


DPAPI_WARNING = (
    "模型密钥和可选账单密钥已安全加密，并绑定当前 Windows 用户。"
    "重装系统或更换 Windows 用户后将无法读取；知识库仍会保留，"
    "但需要重新输入密钥。可选解析 Cookie 与模型密钥均只在当前 Windows 用户下加密保存。"
)
