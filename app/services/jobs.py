"""Local ingest and summary jobs for explicitly confirmed works."""

from __future__ import annotations

import asyncio
import uuid

from loguru import logger
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import DATA_DIR, settings
from app.database import async_session_factory
from app.models import (
    ImportItem,
    Job,
    KnowledgeChunk,
    UsageEvent,
    Work,
    WorkSourceAsset,
    utcnow,
)
from app.services.budget import BudgetExceeded, consume, record_usage, release, reserve
from app.services.content_pipeline import process_work
from app.services.errors import classify_error, safe_error_message
from app.services.f2_links import F2WorkClient, PublicLinkError
from app.services.import_queue import open_f2_circuit, resolve_submitted_link
from app.services.pricing import PRICE_VERSION
from app.services.providers import DashScopeProvider, SUMMARY_MAX_OUTPUT_TOKENS
from app.services.runtime_settings import get_runtime_settings
from app.services.secrets import get_secret
from app.services.summaries import (
    local_asset_names,
    mark_summary_failed,
    source_without_generated_notes,
    store_summary,
)
from app.services.temp_files import unlink_with_retries


ACTIVE_STATES = {"queued", "running", "cancelling"}
LOCAL_SUPPLEMENT_CODES = {
    "f2_cookie_required",
    "f2_response_invalid",
    "f2_contract_changed",
    "media_missing",
    "media_expired",
    "work_unavailable",
    "unsupported_content_type",
}


def _set_progress(job: Job, **values) -> None:
    current = dict(job.progress or {})
    current.update(values)
    current["updated_at"] = utcnow().isoformat()
    job.progress = current


async def _offer_local_supplement(
    session: AsyncSession, work: Work, *, code: str, message: str
) -> None:
    if code not in LOCAL_SUPPLEMENT_CODES:
        return
    await session.execute(
        update(ImportItem)
        .where(
            ImportItem.existing_work_id == work.id,
            ImportItem.status == "confirmed",
        )
        .values(
            status="needs_local_file",
            error_code=code,
            error_message=message,
        )
    )


def job_to_dict(job: Job) -> dict:
    return {
        "id": job.id,
        "job_type": job.job_type,
        "state": job.state,
        "message": job.message,
        "total_items": job.total_items,
        "processed_items": job.processed_items,
        "failed_items": job.failed_items,
        "cancelled_items": job.cancelled_items,
        "deferred_items": job.deferred_items,
        "cancel_requested": job.cancel_requested,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
        "progress": job.progress or {},
    }


async def _active_work_ids(session: AsyncSession) -> set[int]:
    """Return works already owned by the persistent processing queue."""

    scopes = (
        await session.execute(
            select(Job.scope).where(
                Job.job_type.in_({"ingest", "summarize"}),
                Job.state.in_(ACTIVE_STATES),
            )
        )
    ).scalars()
    result: set[int] = set()
    for scope in scopes:
        if not isinstance(scope, dict):
            continue
        for value in scope.get("work_ids") or []:
            try:
                result.add(int(value))
            except (TypeError, ValueError):
                continue
    return result


async def _queue_position(session: AsyncSession) -> int:
    queued_before = int(
        await session.scalar(
            select(func.count(Job.id)).where(
                Job.job_type.in_({"ingest", "summarize"}),
                Job.state.in_(ACTIVE_STATES),
            )
        )
        or 0
    )
    return queued_before + 1


async def enqueue_ingest_job(session: AsyncSession, work_ids: list[int]) -> Job:
    requested = sorted(set(int(item) for item in work_ids))
    requested = [
        work_id
        for work_id in requested
        if work_id not in await _active_work_ids(session)
    ]
    valid = list(
        (
            await session.execute(
                select(Work.id).where(
                    Work.id.in_(requested),
                    Work.library_state.in_({"pending", "issues"}),
                )
            )
        ).scalars()
    )
    if not valid:
        raise ValueError("所选作品已在任务队列中，或当前状态不可入库")
    queue_position = await _queue_position(session)
    job = Job(
        id=str(uuid.uuid4()),
        job_type="ingest",
        state="queued",
        scope={"work_ids": valid},
        total_items=len(valid),
        progress={
            "items_total": len(valid),
            "items_completed": 0,
            "queue_position": queue_position,
        },
        message=(
            f"等待入库 {len(valid)} 个作品"
            if queue_position == 1
            else f"已加入队列第 {queue_position} 位，共 {len(valid)} 个作品"
        ),
    )
    session.add(job)
    await session.flush()
    return job


async def enqueue_summary_job(session: AsyncSession, work_ids: list[int]) -> Job:
    requested = sorted(set(int(item) for item in work_ids))
    requested = [
        work_id
        for work_id in requested
        if work_id not in await _active_work_ids(session)
    ]
    valid = list(
        (
            await session.execute(
                select(Work.id).where(
                    Work.id.in_(requested), Work.library_state == "in_library"
                )
            )
        ).scalars()
    )
    if not valid:
        raise ValueError("所选作品已在任务队列中，或当前没有可整理的在库作品")
    queue_position = await _queue_position(session)
    job = Job(
        id=str(uuid.uuid4()),
        job_type="summarize",
        state="queued",
        scope={"work_ids": valid},
        total_items=len(valid),
        progress={
            "items_total": len(valid),
            "items_completed": 0,
            "queue_position": queue_position,
        },
        message=(
            f"等待补齐 {len(valid)} 个作品总结"
            if queue_position == 1
            else f"已加入队列第 {queue_position} 位，共 {len(valid)} 个作品"
        ),
    )
    session.add(job)
    await session.commit()
    return job


async def cancel_job(session: AsyncSession, job_id: str) -> Job:
    job = await session.get(Job, job_id)
    if not job:
        raise LookupError("任务不存在")
    if job.state == "queued":
        job.state = "cancelled"
        job.cancel_requested = True
        job.completed_at = utcnow()
        job.message = "任务已取消"
    elif job.state == "running":
        job.state = "cancelling"
        job.cancel_requested = True
        job.message = "正在安全停止：当前作品完成后停止"
    elif job.state != "cancelling":
        raise ValueError("该任务已经结束，不能再次停止")
    await session.commit()
    return job


async def _is_cancelling(session: AsyncSession, job: Job) -> bool:
    await session.refresh(job)
    return job.cancel_requested or job.state == "cancelling"


async def _finish_cancelled(session: AsyncSession, job: Job) -> None:
    job.state = "cancelled"
    job.message = "任务已在安全边界停止"
    job.completed_at = utcnow()
    _set_progress(job, phase="cancelled", current_work=None)
    await session.commit()


async def _refresh_f2_media(
    session: AsyncSession,
    work: Work,
    client: F2WorkClient,
) -> None:
    """Refresh expiring F2 media only after the user confirms ingestion."""

    local_asset = await session.scalar(
        select(WorkSourceAsset.id).where(WorkSourceAsset.work_id == work.id).limit(1)
    )
    if local_asset:
        return
    source_url = work.source_url or (
        f"https://www.douyin.com/"
        f"{'note' if work.kind == 'image' else 'video'}/{work.platform_work_id}"
    )
    result = await resolve_submitted_link(client, source_url)
    if result.platform_work_id != work.platform_work_id:
        raise PublicLinkError("f2_contract_changed")
    if (
        result.download_permission == "allowed"
        and not result.media_urls
        and not result.image_urls
    ):
        raise PublicLinkError("media_missing")
    work.kind = result.kind
    work.title = result.title
    work.description = result.description
    work.author_id = result.author_id
    work.author_name = result.author_name
    work.duration_seconds = result.duration_seconds
    work.cover_url = result.cover_url
    work.source_url = result.canonical_url
    # A fresh denied/unknown permission must clear any stale full-media URLs
    # captured by an earlier preview or an older application version.
    work.media_urls = list(result.media_urls)
    work.image_urls = list(result.image_urls)
    work.raw_metadata = dict(result.raw_metadata)
    await session.flush()


async def _ingest_works(session: AsyncSession, job: Job) -> bool:
    work_ids = [int(item) for item in (job.scope or {}).get("work_ids") or []]
    rows = (
        (
            await session.execute(
                select(Work).where(Work.id.in_(work_ids)).order_by(Work.id)
            )
        )
        .scalars()
        .all()
    )
    job.total_items = len(rows)
    runtime = await get_runtime_settings(session)
    f2_client = F2WorkClient()
    for index, work in enumerate(rows):
        if await _is_cancelling(session, job):
            return True
        job.message = f"正在入库 {index + 1}/{len(rows)}：{work.title[:60]}"
        _set_progress(
            job,
            phase="ingesting",
            items_total=len(rows),
            items_completed=index,
            current_work={"id": work.id, "title": work.title},
        )
        await session.commit()

        existing_chunks = int(
            await session.scalar(
                select(func.count(KnowledgeChunk.id)).where(
                    KnowledgeChunk.work_id == work.id
                )
            )
            or 0
        )
        if work.processing_state == "processed" and existing_chunks:
            work.library_state = "in_library"
            job.processed_items += 1
            _set_progress(job, items_completed=index + 1)
            await session.commit()
            continue
        try:
            await _refresh_f2_media(session, work, f2_client)
            await session.commit()
        except PublicLinkError as exc:
            work.processing_state = "failed"
            work.library_state = "issues"
            work.process_error = safe_error_message(exc)
            work.last_error_code = exc.code
            work.process_attempts += 1
            job.failed_items += 1
            await _offer_local_supplement(
                session,
                work,
                code=exc.code,
                message=work.process_error,
            )
            _set_progress(job, items_completed=index + 1)
            if exc.opens_circuit:
                await open_f2_circuit(
                    session,
                    error_code=exc.code,
                    message=work.process_error,
                )
                job.state = "failed"
                job.message = f"{work.process_error}；后续作品链接访问已停止"
                job.completed_at = utcnow()
                _set_progress(job, phase="failed")
                await session.commit()
                return False
            await session.commit()
            continue
        if work.duration_seconds > settings.max_work_duration_seconds:
            work.processing_state = "oversize"
            work.process_error = "作品时长超过单作品安全上限"
            work.last_error_code = "work_too_long"
            work.library_state = "issues"
            job.failed_items += 1
            _set_progress(job, items_completed=index + 1)
            await session.commit()
            continue

        media_minutes = max(0.0, work.duration_seconds / 60)
        estimated_tokens = min(
            int(runtime["daily_llm_token_limit"]),
            max(
                SUMMARY_MAX_OUTPUT_TOKENS + 8000,
                int(work.duration_seconds * 12) + SUMMARY_MAX_OUTPUT_TOKENS + 3000,
            ),
        )
        try:
            reservation = await reserve(
                session,
                works=1,
                media_minutes=media_minutes,
                llm_tokens=estimated_tokens,
            )
            await session.commit()
        except BudgetExceeded as exc:
            work.library_state = "pending"
            work.process_error = str(exc)
            work.last_error_code = "budget_deferred"
            job.deferred_items += 1
            _set_progress(job, items_completed=index + 1)
            await session.commit()
            continue

        try:
            state = await process_work(session, work, job.id)
            actual_tokens = int(
                await session.scalar(
                    select(func.coalesce(func.sum(UsageEvent.quantity), 0)).where(
                        UsageEvent.job_id == job.id,
                        UsageEvent.work_id == work.id,
                        UsageEvent.metric == "tokens",
                    )
                )
                or 0
            )
            if state == "processed":
                await consume(session, reservation, actual_llm_tokens=actual_tokens)
                work.library_state = "in_library"
                work.process_error = None
                work.last_error_code = None
                job.processed_items += 1
            else:
                await release(session, reservation)
                work.library_state = "issues"
                work.process_error = (
                    "请先配置模型 API Key"
                    if state == "waiting_for_key"
                    else "请先安装并配置 ffmpeg"
                )
                work.last_error_code = (
                    "model_not_configured"
                    if state == "waiting_for_key"
                    else "ffmpeg_unavailable"
                )
                job.failed_items += 1
            _set_progress(job, items_completed=index + 1)
            await session.commit()
        except PublicLinkError as exc:
            await release(session, reservation)
            work.processing_state = "failed"
            work.library_state = "issues"
            work.process_error = safe_error_message(exc)
            work.last_error_code = exc.code
            work.process_attempts += 1
            job.failed_items += 1
            if exc.opens_circuit:
                await open_f2_circuit(
                    session, error_code=exc.code, message=safe_error_message(exc)
                )
                job.message = f"{safe_error_message(exc)}；后续媒体访问已停止"
                job.state = "failed"
                job.completed_at = utcnow()
                _set_progress(job, items_completed=index + 1, phase="failed")
                await session.commit()
                return False
            _set_progress(job, items_completed=index + 1)
            await session.commit()
        except Exception as exc:
            logger.exception("处理本地作品失败: {}", work.platform_work_id)
            for suffix in (".mp4", ".asr.wav"):
                await asyncio.to_thread(
                    unlink_with_retries,
                    DATA_DIR / "tmp" / f"{work.platform_work_id}{suffix}",
                )
            await release(session, reservation)
            work.processing_state = "failed"
            work.library_state = "issues"
            work.process_error = safe_error_message(exc)
            work.last_error_code = classify_error(exc)
            await _offer_local_supplement(
                session,
                work,
                code=work.last_error_code,
                message=work.process_error,
            )
            work.process_attempts += 1
            job.failed_items += 1
            _set_progress(job, items_completed=index + 1)
            await session.commit()
    return await _is_cancelling(session, job)


async def _summarize_works(session: AsyncSession, job: Job) -> bool:
    work_ids = [int(item) for item in (job.scope or {}).get("work_ids") or []]
    rows = (
        (
            await session.execute(
                select(Work)
                .where(Work.id.in_(work_ids), Work.library_state == "in_library")
                .order_by(Work.id)
            )
        )
        .scalars()
        .all()
    )
    api_key = await get_secret(session, "dashscope_api_key")
    if not api_key:
        raise RuntimeError("请先在设置中配置模型 API Key")
    provider = DashScopeProvider(api_key)
    runtime = await get_runtime_settings(session)
    job.total_items = len(rows)
    for index, work in enumerate(rows):
        if await _is_cancelling(session, job):
            return True
        job.message = f"正在整理 {index + 1}/{len(rows)}：{work.title[:60]}"
        _set_progress(
            job,
            phase="summarizing",
            items_total=len(rows),
            items_completed=index,
            current_work={"id": work.id, "title": work.title},
        )
        await session.commit()
        source_text = source_without_generated_notes(work.content_text)
        if not source_text:
            chunks = list(
                (
                    await session.execute(
                        select(KnowledgeChunk.text)
                        .where(
                            KnowledgeChunk.work_id == work.id,
                            KnowledgeChunk.source_kind != "notes",
                        )
                        .order_by(KnowledgeChunk.chunk_index)
                    )
                ).scalars()
            )
            source_text = "\n\n".join(chunks)
        reservation = None
        try:
            reservation = await reserve(
                session,
                works=0,
                llm_tokens=max(
                    SUMMARY_MAX_OUTPUT_TOKENS + 4000,
                    min(
                        80000,
                        len(source_text) // 2 + SUMMARY_MAX_OUTPUT_TOKENS + 3000,
                    ),
                ),
            )
            await session.commit()
            payload, usage = await provider.summarize(
                source_text,
                asset_ids=local_asset_names(work),
                system_prompt=str(runtime.get("summary_prompt") or ""),
                model=str(runtime.get("processing_model") or settings.enrichment_model),
            )
            await record_usage(
                session,
                model=usage.model,
                metric=usage.metric,
                quantity=usage.quantity,
                unit=usage.unit,
                estimated_cost_cny=usage.cost_cny,
                job_id=job.id,
                work_id=work.id,
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
            reservation = None
            await store_summary(
                session, work, payload, model=usage.model, source_text=source_text
            )
            job.processed_items += 1
        except BudgetExceeded:
            job.deferred_items += 1
        except Exception as exc:
            if reservation:
                await release(session, reservation)
            await mark_summary_failed(session, work, safe_error_message(exc))
            job.failed_items += 1
        _set_progress(job, items_completed=index + 1)
        await session.commit()
    return await _is_cancelling(session, job)


async def run_job(session: AsyncSession, job: Job) -> None:
    claimed = await session.execute(
        update(Job)
        .where(Job.id == job.id, Job.state == "queued")
        .values(state="running", started_at=utcnow())
    )
    await session.commit()
    if int(claimed.rowcount or 0) != 1:
        return
    await session.refresh(job)
    try:
        cancelled = (
            await _ingest_works(session, job)
            if job.job_type == "ingest"
            else await _summarize_works(session, job)
        )
        if cancelled:
            await _finish_cancelled(session, job)
            return
        if job.state == "failed":
            return
        job.state = "partial" if job.failed_items or job.deferred_items else "succeeded"
        job.completed_at = utcnow()
        job.message = (
            f"任务完成：成功 {job.processed_items}，延后 {job.deferred_items}，"
            f"失败 {job.failed_items}"
        )
        _set_progress(job, phase="completed", current_work=None)
        await session.commit()
    except Exception as exc:
        logger.exception("本地任务失败: {}", job.id)
        job.state = "failed"
        job.completed_at = utcnow()
        job.message = safe_error_message(exc)
        _set_progress(job, phase="failed", current_work=None)
        await session.commit()


class JobCoordinator:
    def __init__(self) -> None:
        self.stop_event = asyncio.Event()
        self.task: asyncio.Task | None = None

    async def start(self) -> None:
        self.stop_event = asyncio.Event()
        self.task = asyncio.create_task(self._loop(), name="local-processing-worker")

    async def stop(self) -> None:
        self.stop_event.set()
        if self.task:
            await self.task
        self.task = None

    async def _loop(self) -> None:
        while not self.stop_event.is_set():
            async with async_session_factory() as session:
                job = (
                    await session.execute(
                        select(Job)
                        .where(
                            Job.job_type.in_({"ingest", "summarize"}),
                            Job.state == "queued",
                        )
                        .order_by(Job.created_at)
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if job:
                    await run_job(session, job)
                    continue
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass


coordinator = JobCoordinator()
