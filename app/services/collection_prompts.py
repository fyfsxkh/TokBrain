"""Resolve the collection-specific summary prompt for a work."""

from __future__ import annotations

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Collection, CollectionMembership


async def summary_prompt_for_work(
    session: AsyncSession,
    work_id: int,
    fallback: str,
) -> str:
    """Use the most recently assigned collection, falling back to global rules."""

    prompts = (
        (
            await session.execute(
                select(Collection.summary_prompt)
                .join(
                    CollectionMembership,
                    CollectionMembership.collection_id == Collection.id,
                )
                .where(CollectionMembership.work_id == work_id)
                .order_by(
                    desc(CollectionMembership.created_at),
                    desc(CollectionMembership.id),
                )
                .limit(1)
            )
        )
        .scalars()
        .all()
    )
    prompt = str(prompts[0] or "").strip() if prompts else ""
    return prompt or fallback
