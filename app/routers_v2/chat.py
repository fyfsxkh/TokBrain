from __future__ import annotations

import asyncio
import json
import re
import time

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas import ChatAnswer, ChatRequest
from app.services.budget import BudgetExceeded, consume, record_usage, release, reserve
from app.services.knowledge import search
from app.services.pricing import PRICE_VERSION
from app.services.providers import DashScopeProvider, ProviderUsage
from app.services.runtime_settings import get_runtime_settings
from app.services.secrets import get_secret


router = APIRouter(prefix="/api/chat", tags=["chat"])


def _model_for(mode: str, runtime: dict) -> str:
    return str(
        runtime["chat_deep_model"] if mode == "deep" else runtime["chat_fast_model"]
    )


def _source_views(sources: list[dict]) -> list[dict]:
    return [
        {
            "work_id": item["work_id"],
            "platform_work_id": item["platform_work_id"],
            "title": item["title"],
            "collection": item.get("collection"),
            "timestamp_seconds": item.get("timestamp_seconds"),
            "external_url": item.get("external_url"),
            "source_kind": item["source_kind"],
        }
        for item in sources
    ]


def _build_context(payload: ChatRequest, sources: list[dict]) -> str:
    source_context = "\n\n".join(
        f"[来源{index}] {item['title']}\n{item['text']}"
        for index, item in enumerate(sources, 1)
    )
    conversation = "\n".join(
        f"{'用户' if turn.role == 'user' else '助手'}：{turn.content}"
        for turn in payload.history[-12:]
    )[-16000:]
    return (
        f"最近对话（仅用于理解当前问题）：\n{conversation}\n\n检索来源：\n{source_context}"
        if conversation
        else source_context
    )


async def _record_chat_usage(
    session: AsyncSession, usage: ProviderUsage, *, recovered: bool = False
) -> None:
    await record_usage(
        session,
        model=usage.model,
        metric=usage.metric,
        quantity=usage.quantity,
        unit=usage.unit,
        estimated_cost_cny=usage.cost_cny,
        metadata={
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            **({"recovered_after_commit_failure": True} if recovered else {}),
        },
        price_version=PRICE_VERSION,
    )


async def _settle_chat_failure(
    session: AsyncSession, reservation, usage: ProviderUsage | None
) -> None:
    """Make a failed response reusable without treating a completed model call as free."""

    await session.rollback()
    try:
        if usage is None:
            await release(session, reservation)
        else:
            await _record_chat_usage(session, usage, recovered=True)
            await consume(
                session,
                reservation,
                actual_llm_tokens=usage.input_tokens + usage.output_tokens,
            )
        await session.commit()
    except Exception:
        await session.rollback()
        logger.exception("回答失败后的用量结算未能持久化")


async def _retrieve(payload: ChatRequest, session: AsyncSession) -> list[dict]:
    # Concrete short queries must stand on their own. Mixing several previous
    # questions into the embedding can make an unrelated prior topic dominate.
    retrieval_question = payload.question.strip()[-4000:]
    if re.fullmatch(
        r"(这个|那个|上述|前面|它|这点|那点|为什么|怎么做)[？?]?", retrieval_question
    ):
        recent = [turn.content for turn in payload.history[-8:] if turn.role == "user"][
            -1:
        ]
        retrieval_question = "\n".join([*recent, retrieval_question])[-4000:]
    limit = min(payload.top_k, 8 if payload.mode == "deep" else 6)
    return await search(session, retrieval_question, limit)


@router.post("/search")
async def semantic_search(
    payload: ChatRequest, session: AsyncSession = Depends(get_db)
):
    try:
        results = await _retrieve(payload, session)
    except BudgetExceeded as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    await session.commit()
    return {"results": results}


@router.post("/ask", response_model=ChatAnswer)
async def ask(payload: ChatRequest, session: AsyncSession = Depends(get_db)):
    api_key = await get_secret(session, "dashscope_api_key")
    if not api_key:
        raise HTTPException(status_code=409, detail="请先在设置中配置百炼 API Key")
    try:
        sources = await _retrieve(payload, session)
    except BudgetExceeded as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    if not sources:
        await session.commit()
        return ChatAnswer(answer="知识库中还没有可用于回答的内容。", sources=[])
    context = _build_context(payload, sources)
    runtime = await get_runtime_settings(session)
    model = _model_for(payload.mode, runtime)
    estimated_tokens = max(4000, len(context) // 2 + 2000)
    try:
        reservation = await reserve(session, works=0, llm_tokens=estimated_tokens)
        await session.commit()
    except BudgetExceeded as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    usage: ProviderUsage | None = None
    try:
        answer, usage = await DashScopeProvider(api_key).chat(
            payload.question, context, model=model
        )
        await _record_chat_usage(session, usage)
        await consume(
            session,
            reservation,
            actual_llm_tokens=usage.input_tokens + usage.output_tokens,
        )
        await session.commit()
        return ChatAnswer(answer=answer, sources=_source_views(sources))
    except Exception:
        await _settle_chat_failure(session, reservation, usage)
        raise


def _event(event_type: str, **values: object) -> bytes:
    return (
        json.dumps({"type": event_type, **values}, ensure_ascii=False) + "\n"
    ).encode("utf-8")


@router.post("/ask/stream")
async def ask_stream(payload: ChatRequest, session: AsyncSession = Depends(get_db)):
    api_key = await get_secret(session, "dashscope_api_key")
    if not api_key:
        raise HTTPException(status_code=409, detail="请先在设置中配置百炼 API Key")

    async def generate():
        started = time.perf_counter()
        reservation = None
        usage: ProviderUsage | None = None
        try:
            yield _event("stage", stage="retrieving", message="正在查找相关作品…")
            retrieval_started = time.perf_counter()
            sources = await _retrieve(payload, session)
            retrieval_ms = round((time.perf_counter() - retrieval_started) * 1000)
            if not sources:
                await session.commit()
                yield _event("delta", text="知识库中还没有可用于回答的内容。")
                yield _event("sources", sources=[])
                yield _event(
                    "done", timing_ms={"retrieval": retrieval_ms, "total": retrieval_ms}
                )
                return
            yield _event("sources", sources=_source_views(sources))
            yield _event("stage", stage="generating", message="正在组织回答…")
            context = _build_context(payload, sources)
            runtime = await get_runtime_settings(session)
            model = _model_for(payload.mode, runtime)
            pending_reservation = await reserve(
                session, works=0, llm_tokens=max(4000, len(context) // 2 + 2000)
            )
            await session.commit()
            reservation = pending_reservation
            first_token_ms = None
            provider = DashScopeProvider(api_key)
            async for kind, value in provider.chat_stream(
                payload.question, context, model=model
            ):
                if kind == "delta":
                    if first_token_ms is None:
                        first_token_ms = round((time.perf_counter() - started) * 1000)
                    yield _event("delta", text=str(value))
                else:
                    usage = value if isinstance(value, ProviderUsage) else None
            usage = usage or ProviderUsage(model, 0, 0, 0)
            await _record_chat_usage(session, usage)
            await consume(
                session,
                reservation,
                actual_llm_tokens=usage.input_tokens + usage.output_tokens,
            )
            await session.commit()
            reservation = None
            yield _event(
                "done",
                timing_ms={
                    "retrieval": retrieval_ms,
                    "first_token": first_token_ms,
                    "total": round((time.perf_counter() - started) * 1000),
                },
            )
        except asyncio.CancelledError:
            if reservation:
                await _settle_chat_failure(session, reservation, usage)
            raise
        except Exception as exc:
            if reservation:
                await _settle_chat_failure(session, reservation, usage)
            logger.exception("流式回答生成失败: {}", exc)
            yield _event("error", message="回答生成失败，请稍后重试")

    return StreamingResponse(
        generate(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
