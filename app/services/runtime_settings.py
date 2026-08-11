"""Mutable runtime settings stored separately from deployment configuration."""

from __future__ import annotations

import math
from typing import Any, Literal, TypedDict, cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import AppSetting
from app.services.prompts import DEFAULT_SUMMARY_PROMPT


RUNTIME_KEY = "runtime"
AnswerFormat = Literal["rich", "markdown", "plain"]


class RuntimeSettings(TypedDict):
    daily_media_minutes_limit: float
    daily_llm_token_limit: int
    monthly_warning_cny: float
    scene_threshold: float
    max_scene_candidates: int
    max_keyframes: int
    min_keyframe_gap_seconds: float
    default_answer_format: AnswerFormat
    summary_prompt: str
    processing_model: str
    chat_fast_model: str
    chat_deep_model: str


MUTABLE_FIELDS = {
    "daily_media_minutes_limit",
    "daily_llm_token_limit",
    "monthly_warning_cny",
    "scene_threshold",
    "max_scene_candidates",
    "max_keyframes",
    "min_keyframe_gap_seconds",
    "default_answer_format",
    "summary_prompt",
    "processing_model",
    "chat_fast_model",
    "chat_deep_model",
}
PROCESSING_MODEL_OPTIONS = (
    "qwen3.6-flash",
    "qwen3.7-plus",
    "qwen3.7-max",
    "qwen3.7-flash",
    "qwen-plus",
    "qwen-plus-2025-07-28",
    "qwen-flash",
    "qwen-max",
    "qwen-turbo",
)
CHAT_MODEL_OPTIONS = (
    *PROCESSING_MODEL_OPTIONS,
    "qwen-math-turbo",
    "deepseek-r1-distill-qwen-7b",
    "deepseek-v4-flash",
    "deepseek-v4-pro",
    "glm-5",
    "glm-5.1",
    "glm-5.2",
)
PROCESSING_MODELS = frozenset(PROCESSING_MODEL_OPTIONS)
CHAT_MODELS = frozenset(CHAT_MODEL_OPTIONS)
ANSWER_FORMATS = frozenset({"rich", "markdown", "plain"})
NUMERIC_RULES: dict[str, tuple[float, float | None, bool]] = {
    "daily_media_minutes_limit": (1, 100000, False),
    "daily_llm_token_limit": (1000, None, True),
    "monthly_warning_cny": (0, None, False),
    "scene_threshold": (0.05, 0.95, False),
    "max_scene_candidates": (12, 1000, True),
    "max_keyframes": (1, 48, True),
    "min_keyframe_gap_seconds": (0.2, 60, False),
}


def defaults() -> RuntimeSettings:
    return {
        "daily_media_minutes_limit": settings.daily_media_minutes_limit,
        "daily_llm_token_limit": settings.daily_llm_token_limit,
        "monthly_warning_cny": settings.monthly_warning_cny,
        "scene_threshold": settings.scene_threshold,
        "max_scene_candidates": settings.max_scene_candidates,
        "max_keyframes": settings.max_keyframes,
        "min_keyframe_gap_seconds": settings.min_keyframe_gap_seconds,
        "default_answer_format": "rich",
        "summary_prompt": DEFAULT_SUMMARY_PROMPT,
        "processing_model": settings.enrichment_model,
        "chat_fast_model": settings.enrichment_model,
        "chat_deep_model": settings.chat_model,
    }


def _normalize_numeric_settings(result: dict[str, Any]) -> None:
    """Protect every numeric setting from invalid values stored by older builds."""

    fallback = defaults()
    for field, (minimum, maximum, integer) in NUMERIC_RULES.items():
        try:
            value = float(result.get(field, fallback[field]))
            if not math.isfinite(value):
                raise ValueError("not finite")
        except (TypeError, ValueError):
            value = float(fallback[field])
        value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        result[field] = int(value) if integer else value


def _normalize_settings(result: dict[str, Any]) -> RuntimeSettings:
    """Return a complete, response-safe settings object from persisted input."""

    _normalize_numeric_settings(result)
    fallback = defaults()
    prompt = str(result.get("summary_prompt") or "").strip()
    result["summary_prompt"] = prompt[:12_000] or DEFAULT_SUMMARY_PROMPT
    answer_format = str(result.get("default_answer_format") or "").strip()
    result["default_answer_format"] = (
        answer_format
        if answer_format in ANSWER_FORMATS
        else fallback["default_answer_format"]
    )
    allowed_by_field = {
        "processing_model": PROCESSING_MODELS,
        "chat_fast_model": CHAT_MODELS,
        "chat_deep_model": CHAT_MODELS,
    }
    for field, allowed in allowed_by_field.items():
        model = str(result.get(field) or "").strip()
        result[field] = model if model in allowed else str(fallback[field])
    return cast(RuntimeSettings, result)


async def get_runtime_settings(session: AsyncSession) -> RuntimeSettings:
    result = defaults()
    record = await session.get(AppSetting, RUNTIME_KEY)
    if record and isinstance(record.value, dict):
        result.update({k: v for k, v in record.value.items() if k in MUTABLE_FIELDS})
    return _normalize_settings(result)


async def update_runtime_settings(
    session: AsyncSession, values: dict[str, Any]
) -> RuntimeSettings:
    current = await get_runtime_settings(session)
    current.update(
        {k: v for k, v in values.items() if k in MUTABLE_FIELDS and v is not None}
    )
    current = _normalize_settings(current)
    record = await session.get(AppSetting, RUNTIME_KEY)
    stored = {k: current[k] for k in MUTABLE_FIELDS if k in current}
    if record:
        record.value = stored
    else:
        session.add(AppSetting(key=RUNTIME_KEY, value=stored))
    await session.flush()
    return current
