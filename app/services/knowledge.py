"""Small local vector/lexical retrieval layer for personal collections."""

from __future__ import annotations

import asyncio
import math
import re

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Collection,
    CollectionMembership,
    KnowledgeChunk,
    Work,
)
from app.services.budget import consume, record_usage, release, reserve
from app.services.pricing import PRICE_VERSION
from app.services.providers import DashScopeProvider
from app.services.secrets import get_secret


ORIGINAL_EVIDENCE_KINDS = {"subtitle", "transcript", "ocr", "visual"}
RETRIEVABLE_SOURCE_KINDS = ORIGINAL_EVIDENCE_KINDS | {"notes"}


def cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return -1.0
    dot = sum(a * b for a, b in zip(left, right))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / norm if norm else -1.0


def lexical_score(question: str, text: str) -> float:
    normalized_question = re.sub(r"\s+", "", question.lower())
    lower = re.sub(r"\s+", "", text.lower())
    if not normalized_question or not lower:
        return 0.0
    terms: set[str] = set()
    for token in re.findall(r"[\w\u4e00-\u9fff]{2,}", normalized_question):
        terms.add(token)
        for sequence in re.findall(r"[\u4e00-\u9fff]+", token):
            for width in (2, 3):
                terms.update(
                    sequence[index : index + width]
                    for index in range(max(0, len(sequence) - width + 1))
                )
    matched = sum(1.0 for term in terms if term in lower) / max(1, len(terms))
    exact_phrase = (
        1.5 if len(normalized_question) >= 2 and normalized_question in lower else 0
    )
    return matched + exact_phrase


def _score_chunks(
    question: str,
    query_vector: list[float] | None,
    rows: list[tuple[KnowledgeChunk, Work]],
) -> list[tuple[float, bool, float, KnowledgeChunk, Work]]:
    """CPU-only ranking kept outside the API event loop for large libraries."""

    scored: list[tuple[float, bool, float, KnowledgeChunk, Work]] = []
    for chunk, work in rows:
        semantic = bool(query_vector and chunk.embedding)
        lexical = lexical_score(
            question,
            f"{work.title}\n{work.description}\n{chunk.text}",
        )
        semantic_score = cosine(query_vector, chunk.embedding) if semantic else -1.0
        score = semantic_score + min(2.0, lexical) * 0.6 if semantic else lexical
        scored.append((score, semantic, lexical, chunk, work))
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored


async def search(session: AsyncSession, question: str, top_k: int = 8) -> list[dict]:
    grounded_work_ids = (
        select(KnowledgeChunk.work_id)
        .where(KnowledgeChunk.source_kind.in_(ORIGINAL_EVIDENCE_KINDS))
        .distinct()
    )
    rows = (
        await session.execute(
            select(KnowledgeChunk, Work)
            .join(Work, Work.id == KnowledgeChunk.work_id)
            .where(
                Work.library_state == "in_library",
                Work.processing_state == "processed",
                Work.evidence_state.in_({"sufficient", "unverified"}),
                KnowledgeChunk.source_kind.in_(RETRIEVABLE_SOURCE_KINDS),
                Work.id.in_(grounded_work_ids),
            )
        )
    ).all()
    if not rows:
        return []
    work_ids = {work.id for _, work in rows}
    collection_map: dict[int, str] = {}
    collection_rows = (
        await session.execute(
            select(CollectionMembership.work_id, Collection.title)
            .join(Collection, Collection.id == CollectionMembership.collection_id)
            .where(
                CollectionMembership.work_id.in_(work_ids),
            )
            .order_by(Collection.sort_order)
        )
    ).all()
    for work_id, title in collection_rows:
        collection_map.setdefault(work_id, title)
    query_vector: list[float] | None = None
    api_key = await get_secret(session, "dashscope_api_key")
    if api_key and any(chunk.embedding for chunk, _ in rows):
        reservation = await reserve(
            session, works=0, llm_tokens=max(512, len(question) // 2 + 256)
        )
        try:
            provider = DashScopeProvider(api_key)
            vectors, usage = await provider.embed([question])
            query_vector = vectors[0] if vectors else None
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
                },
                price_version=PRICE_VERSION,
            )
            await consume(
                session,
                reservation,
                actual_llm_tokens=usage.input_tokens + usage.output_tokens,
            )
        except Exception as exc:
            await release(session, reservation)
            logger.warning("向量检索不可用，已降级为本地词法检索: {}", exc)
            query_vector = None
    scored = await asyncio.to_thread(_score_chunks, question, query_vector, list(rows))
    grouped: dict[int, dict] = {}
    for score, semantic, lexical, chunk, work in scored:
        semantic_score = score - min(2.0, lexical) * 0.6 if semantic else -1.0
        if not (lexical > 0 or (semantic and semantic_score >= 0.35)):
            continue
        item = grouped.get(work.id)
        if not item:
            item = {
                "score": round(score, 4),
                "texts": [],
                "source_kind": chunk.source_kind,
                "timestamp_seconds": chunk.start_seconds,
                "work_id": work.id,
                "platform_work_id": work.platform_work_id,
                "title": work.title,
                "collection": collection_map.get(work.id),
                "external_url": work.source_url,
            }
            grouped[work.id] = item
        if len(item["texts"]) < 2 and chunk.text not in item["texts"]:
            item["texts"].append(chunk.text)
    results = sorted(grouped.values(), key=lambda item: item["score"], reverse=True)[
        :top_k
    ]
    for item in results:
        item["text"] = "\n\n".join(item.pop("texts"))
    return results
