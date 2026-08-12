"""Local ingest and summary jobs for explicitly confirmed works."""

from __future__ import annotations

import asyncio
import uuid

from loguru import logger
from sqlalchemy import and_, func, or_, select, update
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
from app.services.budget import (
    BudgetExceeded,
    consume,
    estimate_video_ingest_units,
    record_usage,
    recover_stale_reservations,
    release,
    reserve,
)
from app.services.content_pipeline import (
    finalize_file_promotions,
    process_work,
    rollback_file_promotions,
)
from app.services.collection_prompts import summary_prompt_for_work
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
WORKER_SHUTDOWN_TIMEOUT_SECONDS = 30.0
_JOB_ENQUEUE_LOCK = asyncio.Lock()


class JobPersistenceError(RuntimeError):
    """A database commit failed after the in-memory unit of work was prepared."""


async def _rollback_transaction(session: AsyncSession) -> None:
    rollback_error: Exception | None = None
    try:
        await session.rollback()
    except Exception as exc:  # pragma: no cover - a broken DB connection is rare
        rollback_error = exc
    try:
        await rollback_file_promotions(session)
    except Exception as exc:  # pragma: no cover - exceptional filesystem damage
        if rollback_error is None:
            rollback_error = exc
        else:
            logger.exception("数据库与媒体文件回滚均失败")
    if rollback_error is not None:
        raise rollback_error


async def _commit_transaction(session: AsyncSession) -> None:
    try:
        await session.commit()
    except asyncio.CancelledError:
        await _rollback_transaction(session)
        raise
    except Exception as exc:
        try:
            await _rollback_transaction(session)
        except Exception:
            logger.exception("提交失败后回滚未完成")
        raise JobPersistenceError(safe_error_message(exc)) from exc
    await finalize_file_promotions(session)


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
    work.supplement_state = "required"
    work.supplement_reason = (
        "image_set_incomplete" if work.kind == "image" else "full_video_unavailable"
    )
    if work.evidence_state != "sufficient":
        work.evidence_state = "insufficient"
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


async def _actual_provider_usage(
    session: AsyncSession, *, job_id: str, work_id: int
) -> tuple[int, float]:
    """Return billable usage already persisted/staged by one processing attempt."""

    tokens = int(
        await session.scalar(
            select(func.coalesce(func.sum(UsageEvent.quantity), 0)).where(
                UsageEvent.job_id == job_id,
                UsageEvent.work_id == work_id,
                UsageEvent.metric == "tokens",
            )
        )
        or 0
    )
    audio_seconds = float(
        await session.scalar(
            select(func.coalesce(func.sum(UsageEvent.quantity), 0)).where(
                UsageEvent.job_id == job_id,
                UsageEvent.work_id == work_id,
                UsageEvent.metric == "audio_seconds",
            )
        )
        or 0
    )
    return tokens, audio_seconds


async def _settle_failed_attempt(
    session: AsyncSession,
    reservation,
    *,
    job_id: str,
    work_id: int,
) -> None:
    """Consume completed paid work and release only attempts with no provider usage."""

    actual_tokens, audio_seconds = await _actual_provider_usage(
        session, job_id=job_id, work_id=work_id
    )
    if actual_tokens > 0 or audio_seconds > 0:
        await consume(
            session,
            reservation,
            actual_works=0,
            actual_llm_tokens=actual_tokens,
        )
    else:
        await release(session, reservation)


async def _usage_event_ids(
    session: AsyncSession, *, job_id: str, work_id: int
) -> set[int]:
    return set(
        (
            await session.execute(
                select(UsageEvent.id).where(
                    UsageEvent.job_id == job_id,
                    UsageEvent.work_id == work_id,
                )
            )
        ).scalars()
    )


async def _usage_snapshots(
    session: AsyncSession,
    *,
    job_id: str,
    work_id: int,
    exclude_ids: set[int],
) -> list[dict[str, object]]:
    rows = list(
        (
            await session.execute(
                select(UsageEvent).where(
                    UsageEvent.job_id == job_id,
                    UsageEvent.work_id == work_id,
                )
            )
        ).scalars()
    )
    return [
        {
            "job_id": row.job_id,
            "work_id": row.work_id,
            "provider": row.provider,
            "model": row.model,
            "metric": row.metric,
            "quantity": row.quantity,
            "unit": row.unit,
            "estimated_cost_cny": row.estimated_cost_cny,
            "price_version": row.price_version,
            "metadata_json": dict(row.metadata_json or {}),
            "created_at": row.created_at,
        }
        for row in rows
        if row.id not in exclude_ids
    ]


def _snapshot_token_total(snapshots: list[dict[str, object]]) -> int:
    return int(
        sum(
            float(item["quantity"])
            for item in snapshots
            if item["metric"] == "tokens"
        )
    )


def _restore_usage_snapshots(
    session: AsyncSession, snapshots: list[dict[str, object]]
) -> None:
    session.add_all(UsageEvent(**snapshot) for snapshot in snapshots)


async def _recover_ingest_commit_failure(
    session: AsyncSession,
    *,
    job_id: str,
    work_id: int,
    reservation,
    usage_snapshots: list[dict[str, object]],
    settlement: str,
    initial_library_state: str,
    preserve_existing_knowledge: bool,
    supplement_reprocess: bool,
    items_completed: int,
) -> None:
    work = await session.get(Work, work_id, populate_existing=True)
    job = await session.get(Job, job_id, populate_existing=True)
    if work is None or job is None:
        raise RuntimeError("任务提交失败后无法重新加载状态")
    _restore_usage_snapshots(session, usage_snapshots)
    if settlement == "release":
        await release(session, reservation)
    else:
        await consume(
            session,
            reservation,
            actual_works=0,
            actual_llm_tokens=_snapshot_token_total(usage_snapshots),
        )
    work.processing_state = "failed"
    work.library_state = (
        initial_library_state if preserve_existing_knowledge else "issues"
    )
    if supplement_reprocess:
        work.supplement_state = "failed"
    work.process_error = "处理结果保存失败，请重试"
    work.last_error_code = "persistence_failed"
    work.process_attempts += 1
    job.failed_items += 1
    _set_progress(job, items_completed=items_completed)
    await _commit_transaction(session)


async def _reset_failed_processing_transaction(
    session: AsyncSession,
    *,
    job_id: str,
    work_id: int,
    usage_baseline: set[int],
) -> tuple[Job, Work]:
    try:
        snapshots = await _usage_snapshots(
            session,
            job_id=job_id,
            work_id=work_id,
            exclude_ids=usage_baseline,
        )
    except Exception:
        snapshots = []
    await _rollback_transaction(session)
    work = await session.get(Work, work_id, populate_existing=True)
    job = await session.get(Job, job_id, populate_existing=True)
    if work is None or job is None:
        raise RuntimeError("处理失败后无法重新加载任务状态")
    _restore_usage_snapshots(session, snapshots)
    return job, work


async def enqueue_ingest_job(session: AsyncSession, work_ids: list[int]) -> Job:
    async with _JOB_ENQUEUE_LOCK:
        return await _enqueue_ingest_job(session, work_ids)


async def _enqueue_ingest_job(
    session: AsyncSession, work_ids: list[int]
) -> Job:
    requested = sorted(set(int(item) for item in work_ids))
    active_work_ids = await _active_work_ids(session)
    requested = [work_id for work_id in requested if work_id not in active_work_ids]
    valid = list(
        (
            await session.execute(
                select(Work.id).where(
                    Work.id.in_(requested),
                    or_(
                        Work.library_state.in_({"pending", "issues"}),
                        and_(
                            Work.library_state.in_({"in_library", "archived"}),
                            Work.supplement_state.in_({"uploaded", "processing"}),
                        ),
                    ),
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
    await session.execute(
        update(Work)
        .where(
            Work.id.in_(valid),
            Work.supplement_state == "uploaded",
        )
        .values(supplement_state="processing")
    )
    # Commit before releasing the process-local enqueue lock so the next request
    # sees this active scope instead of creating a concurrent duplicate.
    await _commit_transaction(session)
    return job


async def enqueue_summary_job(session: AsyncSession, work_ids: list[int]) -> Job:
    async with _JOB_ENQUEUE_LOCK:
        return await _enqueue_summary_job(session, work_ids)


async def _enqueue_summary_job(
    session: AsyncSession, work_ids: list[int]
) -> Job:
    requested = sorted(set(int(item) for item in work_ids))
    active_work_ids = await _active_work_ids(session)
    requested = [work_id for work_id in requested if work_id not in active_work_ids]
    valid = list(
        (
            await session.execute(
                select(Work.id).where(
                    Work.id.in_(requested),
                    Work.library_state == "in_library",
                    Work.evidence_state == "sufficient",
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
    await _commit_transaction(session)
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
    await _commit_transaction(session)
    return job


async def _is_cancelling(session: AsyncSession, job: Job) -> bool:
    await session.refresh(job)
    return job.cancel_requested or job.state == "cancelling"


async def _finish_cancelled(session: AsyncSession, job: Job) -> None:
    job.state = "cancelled"
    job.message = "任务已在安全边界停止"
    job.completed_at = utcnow()
    _set_progress(job, phase="cancelled", current_work=None)
    await _commit_transaction(session)


async def _refresh_f2_media(
    session: AsyncSession,
    work: Work,
    client: F2WorkClient,
) -> None:
    """Refresh expiring F2 media only after the user confirms ingestion."""

    if work.refresh_policy == "never":
        return
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
        result.kind == "video"
        and result.download_permission == "allowed"
        and not result.media_urls
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
    refreshed_metadata = dict(result.raw_metadata or {})
    refreshed_metadata.pop("import_provenance", None)
    previous_metadata = (
        dict(work.raw_metadata or {}) if isinstance(work.raw_metadata, dict) else {}
    )
    provenance = previous_metadata.get("import_provenance")
    if isinstance(provenance, dict):
        # Provenance is controlled by TokBrain at import time.  A later F2
        # refresh may replace discovery fields, but must not erase or spoof the
        # user's rights attestation.
        refreshed_metadata["import_provenance"] = dict(provenance)
    work.raw_metadata = refreshed_metadata
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
    row_ids = [int(work.id) for work in rows]
    job.total_items = len(row_ids)
    runtime = await get_runtime_settings(session)
    f2_client = F2WorkClient()
    for index, work_id in enumerate(row_ids):
        # A failed provider call rolls the transaction back, which expires all
        # ORM instances loaded for this batch.  Reload every work explicitly so
        # the next item never performs implicit async IO via an expired object.
        work = await session.get(Work, work_id, populate_existing=True)
        if work is None:
            raise RuntimeError(f"待处理作品不存在: {work_id}")
        if await _is_cancelling(session, job):
            return True
        initial_library_state = work.library_state
        initial_evidence_state = work.evidence_state
        supplement_reprocess = bool(
            initial_library_state in {"in_library", "archived"}
            and work.supplement_state in {"uploaded", "processing"}
        )
        preserve_existing_knowledge = bool(
            initial_library_state in {"in_library", "archived"}
            and initial_evidence_state == "sufficient"
        )
        job.message = f"正在入库 {index + 1}/{len(row_ids)}：{work.title[:60]}"
        _set_progress(
            job,
            phase="ingesting",
            items_total=len(row_ids),
            items_completed=index,
            current_work={"id": work.id, "title": work.title},
        )
        await _commit_transaction(session)

        existing_chunks = int(
            await session.scalar(
                select(func.count(KnowledgeChunk.id)).where(
                    KnowledgeChunk.work_id == work.id
                )
            )
            or 0
        )
        if (
            work.processing_state == "processed"
            and work.evidence_state == "sufficient"
            and existing_chunks
            and not supplement_reprocess
        ):
            work.library_state = "in_library"
            job.processed_items += 1
            _set_progress(job, items_completed=index + 1)
            await _commit_transaction(session)
            continue
        try:
            await _refresh_f2_media(session, work, f2_client)
            await _commit_transaction(session)
        except PublicLinkError as exc:
            work.processing_state = "failed"
            work.library_state = (
                initial_library_state if preserve_existing_knowledge else "issues"
            )
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
                await _commit_transaction(session)
                return False
            await _commit_transaction(session)
            continue
        if work.duration_seconds > settings.max_work_duration_seconds:
            work.processing_state = "oversize"
            work.process_error = "作品时长超过单作品安全上限"
            work.last_error_code = "work_too_long"
            work.library_state = (
                initial_library_state if preserve_existing_knowledge else "issues"
            )
            if supplement_reprocess:
                work.supplement_state = "failed"
            job.failed_items += 1
            _set_progress(job, items_completed=index + 1)
            await _commit_transaction(session)
            continue

        budget_estimate = estimate_video_ingest_units(
            work.duration_seconds,
            runtime,
            summary_max_output_tokens=SUMMARY_MAX_OUTPUT_TOKENS,
        )
        try:
            reservation = await reserve(
                session,
                works=0 if supplement_reprocess else budget_estimate.works,
                media_minutes=budget_estimate.media_minutes,
                llm_tokens=budget_estimate.llm_tokens,
            )
            await _commit_transaction(session)
        except BudgetExceeded as exc:
            work.library_state = (
                initial_library_state if preserve_existing_knowledge else "pending"
            )
            if supplement_reprocess:
                work.supplement_state = "uploaded"
            work.process_error = str(exc)
            work.last_error_code = "budget_deferred"
            job.deferred_items += 1
            _set_progress(job, items_completed=index + 1)
            await _commit_transaction(session)
            continue

        current_job_id = job.id
        current_work_id = work.id
        usage_baseline = await _usage_event_ids(
            session, job_id=current_job_id, work_id=current_work_id
        )
        usage_records: list[dict[str, object]] = []
        settlement = "consume"
        try:
            state = await process_work(session, work, current_job_id)
            actual_tokens, _ = await _actual_provider_usage(
                session, job_id=current_job_id, work_id=current_work_id
            )
            usage_records = await _usage_snapshots(
                session,
                job_id=current_job_id,
                work_id=current_work_id,
                exclude_ids=usage_baseline,
            )
            if state == "processed":
                await consume(session, reservation, actual_llm_tokens=actual_tokens)
                work.library_state = (
                    initial_library_state if supplement_reprocess else "in_library"
                )
                work.process_error = None
                work.last_error_code = None
                job.processed_items += 1
            elif state in {"evidence_insufficient", "supplement_failed"}:
                await _settle_failed_attempt(
                    session,
                    reservation,
                    job_id=job.id,
                    work_id=work.id,
                )
                work.library_state = initial_library_state
                work.process_error = "未提取到足够的原始内容，请补充完整素材"
                work.last_error_code = "evidence_insufficient"
                job.failed_items += 1
            else:
                settlement = "release"
                await release(session, reservation)
                work.library_state = (
                    initial_library_state if preserve_existing_knowledge else "issues"
                )
                if supplement_reprocess:
                    work.supplement_state = "failed"
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
            await _commit_transaction(session)
        except JobPersistenceError:
            await _recover_ingest_commit_failure(
                session,
                job_id=current_job_id,
                work_id=current_work_id,
                reservation=reservation,
                usage_snapshots=usage_records,
                settlement=settlement,
                initial_library_state=initial_library_state,
                preserve_existing_knowledge=preserve_existing_knowledge,
                supplement_reprocess=supplement_reprocess,
                items_completed=index + 1,
            )
            continue
        except PublicLinkError as exc:
            job, work = await _reset_failed_processing_transaction(
                session,
                job_id=current_job_id,
                work_id=current_work_id,
                usage_baseline=usage_baseline,
            )
            await _settle_failed_attempt(
                session,
                reservation,
                job_id=job.id,
                work_id=work.id,
            )
            work.processing_state = "failed"
            work.library_state = (
                initial_library_state if preserve_existing_knowledge else "issues"
            )
            if supplement_reprocess:
                work.supplement_state = "failed"
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
                await _commit_transaction(session)
                return False
            _set_progress(job, items_completed=index + 1)
            await _commit_transaction(session)
        except Exception as exc:
            job, work = await _reset_failed_processing_transaction(
                session,
                job_id=current_job_id,
                work_id=current_work_id,
                usage_baseline=usage_baseline,
            )
            logger.exception("处理本地作品失败: {}", work.platform_work_id)
            for suffix in (".mp4", ".asr.wav"):
                await asyncio.to_thread(
                    unlink_with_retries,
                    DATA_DIR / "tmp" / f"{work.platform_work_id}{suffix}",
                )
            await _settle_failed_attempt(
                session,
                reservation,
                job_id=job.id,
                work_id=work.id,
            )
            work.processing_state = "failed"
            work.library_state = (
                initial_library_state if preserve_existing_knowledge else "issues"
            )
            if supplement_reprocess:
                work.supplement_state = "failed"
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
            await _commit_transaction(session)
    return await _is_cancelling(session, job)


async def _summarize_works(session: AsyncSession, job: Job) -> bool:
    work_ids = [int(item) for item in (job.scope or {}).get("work_ids") or []]
    rows = (
        (
            await session.execute(
                select(Work)
                .where(
                    Work.id.in_(work_ids),
                    Work.library_state == "in_library",
                    Work.evidence_state == "sufficient",
                )
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
        await _commit_transaction(session)
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
        current_work_id = work.id
        current_job_id = job.id
        reservation = None
        usage = None
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
            await _commit_transaction(session)
            summary_prompt = await summary_prompt_for_work(
                session,
                work.id,
                str(runtime.get("summary_prompt") or ""),
            )
            payload, usage = await provider.summarize(
                source_text,
                asset_ids=local_asset_names(work),
                system_prompt=summary_prompt,
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
            await store_summary(
                session, work, payload, model=usage.model, source_text=source_text
            )
            job.processed_items += 1
        except BudgetExceeded:
            job.deferred_items += 1
        except JobPersistenceError:
            raise
        except Exception as exc:
            await _rollback_transaction(session)
            work = await session.get(Work, current_work_id, populate_existing=True)
            job = await session.get(Job, current_job_id, populate_existing=True)
            if work is None or job is None:
                raise RuntimeError("总结失败后无法重新加载任务状态") from exc
            if reservation:
                if usage is not None:
                    await record_usage(
                        session,
                        model=usage.model,
                        metric=usage.metric,
                        quantity=usage.quantity,
                        unit=usage.unit,
                        estimated_cost_cny=usage.cost_cny,
                        job_id=current_job_id,
                        work_id=current_work_id,
                        metadata={
                            "input_tokens": usage.input_tokens,
                            "output_tokens": usage.output_tokens,
                            "recovered_after_processing_failure": True,
                        },
                        price_version=PRICE_VERSION,
                    )
                    await consume(
                        session,
                        reservation,
                        actual_llm_tokens=usage.input_tokens + usage.output_tokens,
                    )
                else:
                    await release(session, reservation)
            await mark_summary_failed(session, work, safe_error_message(exc))
            job.failed_items += 1
        _set_progress(job, items_completed=index + 1)
        work_id = work.id
        job_id = job.id
        try:
            await _commit_transaction(session)
        except JobPersistenceError as exc:
            work = await session.get(Work, work_id, populate_existing=True)
            job = await session.get(Job, job_id, populate_existing=True)
            if work is None or job is None:
                raise
            if reservation is not None:
                if usage is not None:
                    await record_usage(
                        session,
                        model=usage.model,
                        metric=usage.metric,
                        quantity=usage.quantity,
                        unit=usage.unit,
                        estimated_cost_cny=usage.cost_cny,
                        job_id=job_id,
                        work_id=work_id,
                        metadata={
                            "input_tokens": usage.input_tokens,
                            "output_tokens": usage.output_tokens,
                            "recovered_after_commit_failure": True,
                        },
                        price_version=PRICE_VERSION,
                    )
                    await consume(
                        session,
                        reservation,
                        actual_llm_tokens=usage.input_tokens + usage.output_tokens,
                    )
                else:
                    await release(session, reservation)
            await mark_summary_failed(session, work, safe_error_message(exc))
            job.failed_items += 1
            _set_progress(job, items_completed=index + 1)
            await _commit_transaction(session)
            continue
        reservation = None
    return await _is_cancelling(session, job)


async def run_job(session: AsyncSession, job: Job) -> None:
    job_id = job.id
    claimed = await session.execute(
        update(Job)
        .where(Job.id == job_id, Job.state == "queued")
        .values(state="running", started_at=utcnow())
    )
    await _commit_transaction(session)
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
        await _commit_transaction(session)
    except Exception as exc:
        logger.exception("本地任务失败: {}", job_id)
        await _rollback_transaction(session)
        job = await session.get(Job, job_id, populate_existing=True)
        if job is None:
            raise
        job.state = "failed"
        job.completed_at = utcnow()
        job.message = safe_error_message(exc)
        _set_progress(job, phase="failed", current_work=None)
        await _commit_transaction(session)


class JobCoordinator:
    def __init__(self) -> None:
        self.stop_event = asyncio.Event()
        self.task: asyncio.Task | None = None
        self.last_error: str | None = None

    def health_snapshot(self) -> dict[str, object]:
        alive = bool(self.task and not self.task.done())
        if self.task and self.task.done() and not self.task.cancelled():
            error = self.task.exception()
            if error is not None:
                self.last_error = safe_error_message(error)
        return {
            "name": "processing",
            "alive": alive,
            "workers_alive": 1 if alive else 0,
            "workers_expected": 1,
            "last_error": self.last_error,
        }

    async def start(self) -> None:
        if self.task and not self.task.done():
            return
        self.last_error = None
        async with async_session_factory() as session:
            recovered = await recover_stale_reservations(session)
            await _commit_transaction(session)
        if recovered.works or recovered.media_minutes or recovered.llm_tokens:
            logger.warning(
                "已回收上次异常退出遗留的预算预留：works={} media_minutes={} tokens={}",
                recovered.works,
                recovered.media_minutes,
                recovered.llm_tokens,
            )
        self.stop_event = asyncio.Event()
        self.task = asyncio.create_task(self._loop(), name="local-processing-worker")

    async def stop(self) -> None:
        self.stop_event.set()
        if self.task:
            try:
                await asyncio.wait_for(
                    asyncio.shield(self.task), timeout=WORKER_SHUTDOWN_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                logger.warning("本地处理 worker 未在时限内停止，正在取消")
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
        self.task = None

    async def _loop(self) -> None:
        while not self.stop_event.is_set():
            try:
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
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = safe_error_message(exc)
                logger.exception("本地处理 worker 轮询失败，将在稍后重试")
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass


coordinator = JobCoordinator()
