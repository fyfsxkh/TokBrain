"""Unit-based hard limits and application-local cost estimates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppSetting, DailyBudget, DailyLinkQuota, UsageEvent
from app.services.pricing import ASR_CNY_PER_SECOND, TOKEN_PRICES
from app.services.runtime_settings import get_runtime_settings


ESTIMATE_NOTICE = (
    "金额是本应用按已记录的模型调用和价格表计算的预估值，不是阿里云结算账单；"
    "免费额度、缓存、阶梯价与价格变更可能造成差异。"
)


class BudgetExceeded(RuntimeError):
    pass


@dataclass(slots=True)
class Reservation:
    works: int
    media_minutes: float
    llm_tokens: int


def estimate_video_ingest_units(
    duration_seconds: float,
    runtime: dict[str, object],
    *,
    summary_max_output_tokens: int,
) -> Reservation:
    """Calculate the conservative units later reserved by the ingest worker."""

    candidate_frame_count = min(
        int(runtime["max_scene_candidates"]),
        max(12, min(48, int(runtime["max_keyframes"]) * 2)),
    )
    estimated_tokens = min(
        int(runtime["daily_llm_token_limit"]),
        max(
            summary_max_output_tokens + 12_000,
            int(float(duration_seconds) * 12)
            + candidate_frame_count * 1_800
            + summary_max_output_tokens
            + 8_000,
        ),
    )
    return Reservation(
        works=1,
        media_minutes=max(0.0, float(duration_seconds) / 60),
        llm_tokens=estimated_tokens,
    )


def local_day() -> date:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


async def _counter(session: AsyncSession) -> DailyBudget:
    day = local_day()
    await _ensure_counter(session, day)
    counter = await session.get(DailyBudget, day, populate_existing=True)
    if counter is None:  # Defensive: the upsert above must always create this row.
        raise RuntimeError("无法创建当日额度计数器")
    return counter


async def _ensure_counter(session: AsyncSession, day: date) -> None:
    """Create the daily row atomically when concurrent requests arrive at midnight."""

    await session.execute(
        sqlite_insert(DailyBudget)
        .values(day=day)
        .on_conflict_do_nothing(index_elements=[DailyBudget.day])
    )


async def reserve(
    session: AsyncSession,
    *,
    works: int = 1,
    media_minutes: float = 0,
    llm_tokens: int = 0,
) -> Reservation:
    if works < 0 or media_minutes < 0 or llm_tokens < 0:
        raise ValueError("预算预留数量不能为负数")
    limits = await get_runtime_settings(session)
    day = local_day()
    await _ensure_counter(session, day)
    result = await session.execute(
        update(DailyBudget)
        .where(
            DailyBudget.day == day,
            DailyBudget.media_minutes_used
            + DailyBudget.media_minutes_reserved
            + media_minutes
            <= float(limits["daily_media_minutes_limit"]),
            DailyBudget.llm_tokens_used
            + DailyBudget.llm_tokens_reserved
            + llm_tokens
            <= int(limits["daily_llm_token_limit"]),
        )
        .values(
            works_reserved=DailyBudget.works_reserved + works,
            media_minutes_reserved=DailyBudget.media_minutes_reserved + media_minutes,
            llm_tokens_reserved=DailyBudget.llm_tokens_reserved + llm_tokens,
        )
        .execution_options(synchronize_session=False)
    )
    if int(result.rowcount or 0) != 1:
        counter = await session.get(DailyBudget, day, populate_existing=True)
        if counter and (
            counter.media_minutes_used
            + counter.media_minutes_reserved
            + media_minutes
            > float(limits["daily_media_minutes_limit"])
        ):
            raise BudgetExceeded("已达到今日音视频分钟上限")
        raise BudgetExceeded("已达到今日 AI 用量上限")
    return Reservation(works, media_minutes, llm_tokens)


async def consume(
    session: AsyncSession,
    reservation: Reservation,
    *,
    actual_works: int | None = None,
    actual_llm_tokens: int | None = None,
) -> None:
    settled_works = (
        reservation.works
        if actual_works is None
        else min(reservation.works, max(0, int(actual_works)))
    )
    day = local_day()
    await _ensure_counter(session, day)
    await session.execute(
        update(DailyBudget)
        .where(DailyBudget.day == day)
        .values(
            works_reserved=func.max(
                0, DailyBudget.works_reserved - reservation.works
            ),
            media_minutes_reserved=func.max(
                0, DailyBudget.media_minutes_reserved - reservation.media_minutes
            ),
            llm_tokens_reserved=func.max(
                0, DailyBudget.llm_tokens_reserved - reservation.llm_tokens
            ),
            works_used=DailyBudget.works_used + settled_works,
            media_minutes_used=(
                DailyBudget.media_minutes_used + reservation.media_minutes
            ),
            llm_tokens_used=DailyBudget.llm_tokens_used
            + (
                reservation.llm_tokens
                if actual_llm_tokens is None
                else max(0, actual_llm_tokens)
            ),
        )
        .execution_options(synchronize_session=False)
    )


async def release(session: AsyncSession, reservation: Reservation) -> None:
    day = local_day()
    await _ensure_counter(session, day)
    await session.execute(
        update(DailyBudget)
        .where(DailyBudget.day == day)
        .values(
            works_reserved=func.max(
                0, DailyBudget.works_reserved - reservation.works
            ),
            media_minutes_reserved=func.max(
                0, DailyBudget.media_minutes_reserved - reservation.media_minutes
            ),
            llm_tokens_reserved=func.max(
                0, DailyBudget.llm_tokens_reserved - reservation.llm_tokens
            ),
        )
        .execution_options(synchronize_session=False)
    )


async def recover_stale_reservations(session: AsyncSession) -> Reservation:
    """Clear reservations left by a previously terminated single app process."""

    day = local_day()
    await _ensure_counter(session, day)
    counter = await session.get(DailyBudget, day, populate_existing=True)
    recovered = Reservation(
        works=int(counter.works_reserved if counter else 0),
        media_minutes=float(counter.media_minutes_reserved if counter else 0),
        llm_tokens=int(counter.llm_tokens_reserved if counter else 0),
    )
    if recovered.works or recovered.media_minutes or recovered.llm_tokens:
        await session.execute(
            update(DailyBudget)
            .where(DailyBudget.day == day)
            .values(
                works_reserved=0,
                media_minutes_reserved=0,
                llm_tokens_reserved=0,
            )
            .execution_options(synchronize_session=False)
        )
    return recovered


async def record_usage(
    session: AsyncSession,
    *,
    model: str,
    metric: str,
    quantity: float,
    unit: str,
    estimated_cost_cny: float,
    job_id: str | None = None,
    work_id: int | None = None,
    metadata: dict | None = None,
    price_version: str = "2026-07-manual",
) -> None:
    event_metadata = dict(metadata or {})
    if metric == "tokens":
        priced = model in TOKEN_PRICES
    elif metric == "audio_seconds":
        priced = model in ASR_CNY_PER_SECOND
    else:
        priced = False
    event_metadata["price_status"] = "priced" if priced else "unpriced"
    event_metadata["unpriced"] = not priced
    session.add(
        UsageEvent(
            job_id=job_id,
            work_id=work_id,
            model=model,
            metric=metric,
            quantity=quantity,
            unit=unit,
            estimated_cost_cny=estimated_cost_cny,
            metadata_json=event_metadata,
            price_version=price_version,
        )
    )


async def usage_summary(session: AsyncSession) -> dict:
    now_local = datetime.now(ZoneInfo("Asia/Shanghai"))
    month_start = datetime(
        now_local.year, now_local.month, 1, tzinfo=ZoneInfo("Asia/Shanghai")
    ).astimezone(timezone.utc)
    estimate = await session.scalar(
        select(func.coalesce(func.sum(UsageEvent.estimated_cost_cny), 0.0)).where(
            UsageEvent.created_at >= month_start
        )
    )
    counter = await _counter(session)
    limits = await get_runtime_settings(session)
    link_quota = await session.get(DailyLinkQuota, local_day())
    official = await session.get(AppSetting, "official_bill")
    official_value = (
        official.value if official and isinstance(official.value, dict) else {}
    )
    estimate_value = float(estimate or 0)
    return {
        "month_estimated_cny": round(estimate_value, 4),
        "official_billed_cny": official_value.get("amount_cny"),
        "official_data_as_of": official_value.get("data_as_of"),
        "official_status": official_value.get("status", "not_configured"),
        "daily_works_used": counter.works_used,
        "daily_works_reserved": counter.works_reserved,
        "daily_links_used": int(link_quota.attempted if link_quota else 0),
        "daily_links_limit": 150,
        "daily_media_minutes_used": round(counter.media_minutes_used, 2),
        "daily_media_minutes_reserved": round(counter.media_minutes_reserved, 2),
        "daily_media_minutes_limit": limits["daily_media_minutes_limit"],
        "daily_llm_tokens_used": counter.llm_tokens_used,
        "daily_llm_tokens_reserved": counter.llm_tokens_reserved,
        "daily_llm_tokens_limit": limits["daily_llm_token_limit"],
        "warning_reached": bool(
            limits["monthly_warning_cny"] > 0
            and estimate_value >= limits["monthly_warning_cny"]
        ),
        "estimate_notice": ESTIMATE_NOTICE,
    }
