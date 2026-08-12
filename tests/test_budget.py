import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.budget as budget
from app.models import Base, DailyBudget, UsageEvent
from app.services.providers import ProviderUsage


async def budget_factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'budget.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def test_concurrent_reservations_cannot_both_cross_token_limit(
    tmp_path, monkeypatch
):
    engine, factory = await budget_factory(tmp_path)

    async def limits(_session):
        return {
            "daily_media_minutes_limit": 60.0,
            "daily_llm_token_limit": 1_000,
        }

    monkeypatch.setattr(budget, "get_runtime_settings", limits)

    async def attempt():
        async with factory() as session:
            try:
                await budget.reserve(session, works=0, llm_tokens=600)
                await session.commit()
                return "reserved"
            except budget.BudgetExceeded:
                await session.rollback()
                return "rejected"

    assert sorted(await asyncio.gather(attempt(), attempt())) == [
        "rejected",
        "reserved",
    ]
    async with factory() as session:
        counter = await session.get(DailyBudget, budget.local_day())
        assert counter is not None
        assert counter.llm_tokens_reserved == 600
    await engine.dispose()


async def test_startup_recovery_clears_only_reserved_units(tmp_path, monkeypatch):
    engine, factory = await budget_factory(tmp_path)

    async def limits(_session):
        return {
            "daily_media_minutes_limit": 60.0,
            "daily_llm_token_limit": 10_000,
        }

    monkeypatch.setattr(budget, "get_runtime_settings", limits)
    async with factory() as session:
        reservation = await budget.reserve(
            session, works=1, media_minutes=3.5, llm_tokens=900
        )
        await session.commit()
        await budget.consume(session, reservation, actual_llm_tokens=400)
        await session.commit()
        await budget.reserve(session, works=2, media_minutes=4.0, llm_tokens=700)
        await session.commit()

    async with factory() as session:
        recovered = await budget.recover_stale_reservations(session)
        await session.commit()
        counter = await session.get(DailyBudget, budget.local_day())
        assert recovered == budget.Reservation(2, 4.0, 700)
        assert counter is not None
        assert counter.works_reserved == 0
        assert counter.media_minutes_reserved == 0
        assert counter.llm_tokens_reserved == 0
        assert counter.works_used == 1
        assert counter.media_minutes_used == pytest.approx(3.5)
        assert counter.llm_tokens_used == 400
    await engine.dispose()


async def test_failed_work_consumes_real_usage_without_counting_success(
    tmp_path, monkeypatch
):
    engine, factory = await budget_factory(tmp_path)

    async def limits(_session):
        return {
            "daily_media_minutes_limit": 60.0,
            "daily_llm_token_limit": 10_000,
        }

    monkeypatch.setattr(budget, "get_runtime_settings", limits)
    async with factory() as session:
        reservation = await budget.reserve(
            session, works=1, media_minutes=3.5, llm_tokens=900
        )
        await session.commit()
        await budget.consume(
            session,
            reservation,
            actual_works=0,
            actual_llm_tokens=125,
        )
        await session.commit()
        counter = await session.get(DailyBudget, budget.local_day())

    assert counter is not None
    assert counter.works_reserved == 0
    assert counter.works_used == 0
    assert counter.media_minutes_used == pytest.approx(3.5)
    assert counter.llm_tokens_used == 125
    await engine.dispose()


async def test_unknown_model_usage_is_explicitly_unpriced(tmp_path):
    engine, factory = await budget_factory(tmp_path)
    usage = ProviderUsage("future-model", 12, 4, 0, quantity=16)
    assert usage.priced is False

    async with factory() as session:
        await budget.record_usage(
            session,
            model=usage.model,
            metric=usage.metric,
            quantity=usage.quantity,
            unit=usage.unit,
            estimated_cost_cny=usage.cost_cny,
        )
        await session.commit()
        event = await session.scalar(select(UsageEvent))
        assert event is not None
        assert event.estimated_cost_cny == 0
        assert event.metadata_json["price_status"] == "unpriced"
        assert event.metadata_json["unpriced"] is True

    await engine.dispose()
