"""Unit-based hard limits and application-local cost estimates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppSetting, DailyBudget, DailyLinkQuota, UsageEvent
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


def local_day() -> date:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


async def _counter(session: AsyncSession) -> DailyBudget:
    day = local_day()
    counter = await session.get(DailyBudget, day)
    if not counter:
        counter = DailyBudget(day=day)
        session.add(counter)
        await session.flush()
    return counter


async def reserve(
    session: AsyncSession,
    *,
    works: int = 1,
    media_minutes: float = 0,
    llm_tokens: int = 0,
) -> Reservation:
    limits = await get_runtime_settings(session)
    counter = await _counter(session)
    if (
        counter.media_minutes_used + counter.media_minutes_reserved + media_minutes
        > limits["daily_media_minutes_limit"]
    ):
        raise BudgetExceeded("已达到今日音视频分钟上限")
    if (
        counter.llm_tokens_used + counter.llm_tokens_reserved + llm_tokens
        > limits["daily_llm_token_limit"]
    ):
        raise BudgetExceeded("已达到今日 AI 用量上限")
    counter.works_reserved += works
    counter.media_minutes_reserved += media_minutes
    counter.llm_tokens_reserved += llm_tokens
    await session.flush()
    return Reservation(works, media_minutes, llm_tokens)


async def consume(
    session: AsyncSession,
    reservation: Reservation,
    *,
    actual_llm_tokens: int | None = None,
) -> None:
    counter = await _counter(session)
    counter.works_reserved = max(0, counter.works_reserved - reservation.works)
    counter.media_minutes_reserved = max(0, counter.media_minutes_reserved - reservation.media_minutes)
    counter.llm_tokens_reserved = max(0, counter.llm_tokens_reserved - reservation.llm_tokens)
    counter.works_used += reservation.works
    counter.media_minutes_used += reservation.media_minutes
    counter.llm_tokens_used += (
        reservation.llm_tokens if actual_llm_tokens is None else max(0, actual_llm_tokens)
    )


async def release(session: AsyncSession, reservation: Reservation) -> None:
    counter = await _counter(session)
    counter.works_reserved = max(0, counter.works_reserved - reservation.works)
    counter.media_minutes_reserved = max(0, counter.media_minutes_reserved - reservation.media_minutes)
    counter.llm_tokens_reserved = max(0, counter.llm_tokens_reserved - reservation.llm_tokens)


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
    session.add(
        UsageEvent(
            job_id=job_id,
            work_id=work_id,
            model=model,
            metric=metric,
            quantity=quantity,
            unit=unit,
            estimated_cost_cny=estimated_cost_cny,
            metadata_json=metadata or {},
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
    official_value = official.value if official and isinstance(official.value, dict) else {}
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
            limits["monthly_warning_cny"] > 0 and estimate_value >= limits["monthly_warning_cny"]
        ),
        "estimate_notice": ESTIMATE_NOTICE,
    }
