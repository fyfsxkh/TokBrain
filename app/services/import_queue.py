"""Persistent producer-consumer queue for user-initiated link previews."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_factory
from app.models import (
    AppSetting,
    Collection,
    CollectionMembership,
    DailyLinkQuota,
    ImportBatch,
    ImportItem,
    Job,
    Work,
    WorkSourceAsset,
    utcnow,
)
from app.services.f2_links import (
    DAILY_LINK_LIMIT,
    ERROR_MESSAGES,
    F2WorkClient,
    MAX_BATCH_LINKS,
    PublicLinkError,
    direct_work_id,
    extract_links,
    f2_access_gate,
    normalize_input_url,
    sanitize_url,
)
from app.services.secrets import get_secret


ACTIVE_ITEM_STATES = {"queued", "resolving"}
PREVIEW_SUCCESS_STATES = {"ready", "needs_local_file", "duplicate"}
RISK_SETTING_KEY = "f2_access_circuit"


def _media_policy(metadata: object) -> dict:
    if not isinstance(metadata, dict):
        return {}
    value = metadata.get("media_policy")
    return dict(value) if isinstance(value, dict) else {}


def shanghai_day():
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


async def _security_cleanup_required(session: AsyncSession) -> bool:
    record = await session.get(AppSetting, "security_cleanup")
    value = record.value if record and isinstance(record.value, dict) else {}
    return bool(value.get("required"))


def _parse_time(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


async def circuit_state(session: AsyncSession) -> dict:
    record = await session.get(AppSetting, RISK_SETTING_KEY)
    value = (
        dict(record.value or {}) if record and isinstance(record.value, dict) else {}
    )
    expires_at = _parse_time(value.get("expires_at"))
    active = bool(expires_at and expires_at > utcnow())
    return {
        "active": active,
        "expires_at": expires_at,
        "error_code": value.get("error_code") if active else None,
        "message": value.get("message") if active else None,
    }


async def _open_circuit(
    session: AsyncSession, *, error_code: str, message: str
) -> dict:
    expires_at = utcnow() + timedelta(minutes=30)
    value = {
        "expires_at": expires_at.isoformat(),
        "error_code": error_code,
        "message": message,
    }
    statement = sqlite_insert(AppSetting).values(
        key=RISK_SETTING_KEY, value=value, updated_at=utcnow()
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[AppSetting.key],
            set_={"value": value, "updated_at": utcnow()},
        )
    )
    return {**value, "active": True}


async def open_f2_circuit(
    session: AsyncSession, *, error_code: str, message: str
) -> dict:
    return await _open_circuit(session, error_code=error_code, message=message)


async def _consume_link_quota(session: AsyncSession) -> None:
    day = shanghai_day()
    await session.execute(
        sqlite_insert(DailyLinkQuota)
        .values(day=day, attempted=0, updated_at=utcnow())
        .on_conflict_do_nothing(index_elements=[DailyLinkQuota.day])
    )
    result = await session.execute(
        update(DailyLinkQuota)
        .where(
            DailyLinkQuota.day == day,
            DailyLinkQuota.attempted < DAILY_LINK_LIMIT,
        )
        .values(
            attempted=DailyLinkQuota.attempted + 1,
            updated_at=utcnow(),
        )
    )
    if int(result.rowcount or 0) != 1:
        raise PublicLinkError("daily_limit_exceeded")


async def remaining_daily_quota(session: AsyncSession) -> int:
    row = await session.get(DailyLinkQuota, shanghai_day())
    return max(0, DAILY_LINK_LIMIT - int(row.attempted if row else 0))


async def resolve_submitted_link(client: F2WorkClient, url: str):
    async def operation():
        async with async_session_factory() as gate_session:
            state = await circuit_state(gate_session)
            if state["active"]:
                raise PublicLinkError(
                    str(state["error_code"] or "risk_verification"),
                    str(state["message"] or ERROR_MESSAGES["risk_verification"]),
                    opens_circuit=True,
                )
            await _consume_link_quota(gate_session)
            cookie = await get_secret(gate_session, "f2_cookie") or ""
            await gate_session.commit()
        try:
            return await client.resolve(url, cookie=cookie)
        except PublicLinkError as exc:
            # Persist the breaker before releasing the global network lock.
            # Otherwise another waiting worker could start in the small window
            # between this exception and the caller's item-status write.
            if exc.opens_circuit:
                async with async_session_factory() as gate_session:
                    await _open_circuit(
                        gate_session, error_code=exc.code, message=str(exc)
                    )
                    await gate_session.commit()
            raise

    return await f2_access_gate.run(operation)


async def create_import_batch(
    session: AsyncSession, raw_input: str
) -> tuple[ImportBatch, list[int], int]:
    if await _security_cleanup_required(session):
        raise PublicLinkError("security_cleanup_required")
    state = await circuit_state(session)
    if state["active"]:
        raise PublicLinkError(
            str(state["error_code"] or "risk_verification"),
            f"{state['message']}；请在熔断结束后再试",
        )
    links = extract_links(raw_input)
    if not links:
        raise PublicLinkError("invalid_url")

    accepted = links[:MAX_BATCH_LINKS]
    rejected_count = max(0, len(links) - len(accepted))
    batch_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())
    job = Job(
        id=job_id,
        job_type="link_preview",
        state="queued",
        total_items=len(accepted),
        scope={"batch_id": batch_id},
        progress={"rejected_count": rejected_count},
        message=f"等待解析 {len(accepted)} 个链接",
    )
    batch = ImportBatch(
        id=batch_id,
        job_id=job_id,
        raw_input=raw_input[:100_000],
        state="queued",
        total_items=len(accepted),
    )
    # Persist the parent job before the batch.  The models intentionally do not
    # expose an ORM relationship, so SQLAlchemy cannot always infer the insert
    # order even though the physical table has a foreign key to jobs.
    session.add(job)
    await session.flush()
    session.add(batch)
    await session.flush()

    seen: set[str] = set()
    queued_ids: list[int] = []
    for ordinal, input_url in enumerate(accepted, 1):
        status = "queued"
        error_code = None
        error_message = None
        try:
            normalized = normalize_input_url(input_url)
        except PublicLinkError as exc:
            normalized = f"invalid:{ordinal}:{hashlib.sha256(input_url.encode()).hexdigest()[:16]}"
            status = "failed"
            error_code = exc.code
            error_message = str(exc)
        else:
            identity = direct_work_id(normalized) or normalized
            if identity in seen:
                status = "duplicate"
                error_code = "duplicate_input"
                error_message = ERROR_MESSAGES[error_code]
            seen.add(identity)
        item = ImportItem(
            batch_id=batch_id,
            ordinal=ordinal,
            input_url=sanitize_url(input_url) or "",
            normalized_url=normalized,
            status=status,
            error_code=error_code,
            error_message=error_message,
        )
        session.add(item)
        await session.flush()
        if status == "queued":
            queued_ids.append(item.id)
    await session.commit()
    return batch, queued_ids, rejected_count


async def _mark_batch_blocked(
    session: AsyncSession, batch_id: str, *, code: str, message: str
) -> None:
    await _open_circuit(session, error_code=code, message=message)
    await session.execute(
        update(ImportItem)
        .where(
            ImportItem.batch_id == batch_id,
            ImportItem.status == "queued",
        )
        .values(
            status="blocked",
            error_code=code,
            error_message=f"{message}；后续链接已停止",
        )
    )
    batch = await session.get(ImportBatch, batch_id)
    if batch:
        batch.error_code = code
        batch.error_message = message


async def _refresh_batch(session: AsyncSession, batch_id: str) -> None:
    batch = await session.get(ImportBatch, batch_id)
    if not batch:
        return
    job = await session.get(Job, batch.job_id)
    rows = (
        await session.execute(
            select(ImportItem.status, func.count(ImportItem.id))
            .where(ImportItem.batch_id == batch_id)
            .group_by(ImportItem.status)
        )
    ).all()
    counts = {str(status): int(count) for status, count in rows}
    active = sum(counts.get(state, 0) for state in ACTIVE_ITEM_STATES)
    succeeded = sum(counts.get(state, 0) for state in PREVIEW_SUCCESS_STATES)
    failed = counts.get("failed", 0) + counts.get("blocked", 0)
    cancelled = counts.get("cancelled", 0)
    completed = max(0, batch.total_items - active)
    if job:
        progress = dict(job.progress or {})
        progress.update(
            {
                "completed": completed,
                "ready": counts.get("ready", 0),
                "needs_local_file": counts.get("needs_local_file", 0),
                "duplicates": counts.get("duplicate", 0),
                "failed": failed,
                "cancelled": cancelled,
                "remaining": active,
            }
        )
        job.progress = progress
        job.processed_items = succeeded
        job.failed_items = failed
        job.cancelled_items = cancelled
        job.message = f"已完成 {completed}/{batch.total_items} 个链接"
    if active:
        batch.state = "cancelling" if batch.cancel_requested else "running"
        if job:
            job.state = batch.state
        return
    batch.completed_at = utcnow()
    if batch.cancel_requested:
        batch.state = "cancelled"
    elif counts.get("blocked", 0):
        batch.state = "failed"
    elif failed:
        batch.state = "partial"
    else:
        batch.state = "succeeded"
    if job:
        job.state = batch.state
        job.completed_at = utcnow()
        job.message = (
            f"预检完成：可确认 {counts.get('ready', 0)}，"
            f"需补件 {counts.get('needs_local_file', 0)}，"
            f"重复 {counts.get('duplicate', 0)}，失败 {failed}，取消 {cancelled}"
        )


async def cancel_import_batch(session: AsyncSession, batch_id: str) -> ImportBatch:
    batch = await session.get(ImportBatch, batch_id)
    if not batch:
        raise LookupError("导入批次不存在")
    if batch.state not in {"queued", "running", "cancelling"}:
        return batch
    batch.cancel_requested = True
    batch.state = "cancelling"
    job = await session.get(Job, batch.job_id)
    if job:
        job.cancel_requested = True
        job.state = "cancelling"
        job.message = "正在安全停止链接解析"
    await session.execute(
        update(ImportItem)
        .where(ImportItem.batch_id == batch_id, ImportItem.status == "queued")
        .values(
            status="cancelled",
            error_code="cancelled_by_user",
            error_message=ERROR_MESSAGES["cancelled_by_user"],
        )
    )
    await _refresh_batch(session, batch_id)
    await session.commit()
    return batch


async def batch_view(session: AsyncSession, batch_id: str) -> dict:
    batch = await session.get(ImportBatch, batch_id)
    if not batch:
        raise LookupError("导入批次不存在")
    job = await session.get(Job, batch.job_id)
    items = (
        (
            await session.execute(
                select(ImportItem)
                .where(ImportItem.batch_id == batch_id)
                .order_by(ImportItem.ordinal)
            )
        )
        .scalars()
        .all()
    )
    asset_counts = (
        {
            int(item_id): int(count)
            for item_id, count in (
                await session.execute(
                    select(
                        WorkSourceAsset.import_item_id, func.count(WorkSourceAsset.id)
                    )
                    .where(
                        WorkSourceAsset.import_item_id.in_([item.id for item in items])
                    )
                    .group_by(WorkSourceAsset.import_item_id)
                )
            ).all()
            if item_id is not None
        }
        if items
        else {}
    )
    circuit = await circuit_state(session)
    resolving = [
        {
            "worker_id": item.worker_id,
            "item_id": item.id,
            "title": item.title or item.input_url,
        }
        for item in items
        if item.status == "resolving"
    ]
    return {
        "id": batch.id,
        "job_id": batch.job_id,
        "state": batch.state,
        "total_items": batch.total_items,
        "cancel_requested": batch.cancel_requested,
        "error_code": batch.error_code,
        "error_message": batch.error_message,
        "created_at": batch.created_at,
        "completed_at": batch.completed_at,
        "progress": dict(job.progress or {}) if job else {},
        "workers": resolving,
        "remaining_daily": await remaining_daily_quota(session),
        "daily_limit": DAILY_LINK_LIMIT,
        "circuit": circuit,
        "items": [
            {
                "id": item.id,
                "ordinal": item.ordinal,
                "input_url": item.input_url,
                "canonical_url": item.canonical_url,
                "platform_work_id": item.platform_work_id,
                "kind": item.kind,
                "title": item.title,
                "author_name": item.author_name,
                "duration_seconds": item.duration_seconds,
                "cover_url": item.cover_url,
                "status": item.status,
                "error_code": item.error_code,
                "error_message": item.error_message,
                "existing_work_id": item.existing_work_id,
                "worker_id": item.worker_id,
                "local_asset_count": asset_counts.get(item.id, 0),
                "has_public_media": bool(item.media_urls or item.image_urls),
                "download_permission": str(
                    _media_policy(item.raw_metadata).get("download_permission")
                    or "unknown"
                ),
                "processing_mode": str(
                    _media_policy(item.raw_metadata).get("processing_mode")
                    or "subtitle_or_audio"
                ),
                "has_audio_or_subtitle": bool(
                    _media_policy(item.raw_metadata).get("audio_urls")
                    or _media_policy(item.raw_metadata).get("subtitle_urls")
                    or _media_policy(item.raw_metadata).get("subtitle_texts")
                ),
            }
            for item in items
        ],
    }


async def confirm_import_items(
    session: AsyncSession, batch_id: str, item_ids: list[int]
) -> Job:
    requested = set(int(item) for item in item_ids)
    items = (
        (
            await session.execute(
                select(ImportItem).where(
                    ImportItem.batch_id == batch_id,
                    ImportItem.id.in_(requested),
                    ImportItem.status.in_({"ready", "needs_local_file"}),
                )
            )
        )
        .scalars()
        .all()
    )
    if not items:
        raise ValueError("请选择至少一个已解析或已补件的作品")
    manual_group = (
        await session.execute(
            select(Collection).where(Collection.key == "manual-import")
        )
    ).scalar_one_or_none()
    if not manual_group:
        manual_group = Collection(key="manual-import", title="手动导入", sort_order=-1)
        session.add(manual_group)
        await session.flush()

    work_ids: list[int] = []
    for item in items:
        assets = (
            (
                await session.execute(
                    select(WorkSourceAsset).where(
                        WorkSourceAsset.import_item_id == item.id
                    )
                )
            )
            .scalars()
            .all()
        )
        if item.status == "needs_local_file" and not assets:
            continue
        platform_work_id = item.platform_work_id or (
            "local-" + hashlib.sha256(item.normalized_url.encode()).hexdigest()[:32]
        )
        work = (
            await session.execute(
                select(Work).where(
                    Work.platform == "douyin",
                    Work.platform_work_id == platform_work_id,
                )
            )
        ).scalar_one_or_none()
        if work:
            item.status = "duplicate"
            item.error_code = "already_imported"
            item.error_message = ERROR_MESSAGES["already_imported"]
            item.existing_work_id = work.id
            continue
        work = Work(
            platform="douyin",
            platform_work_id=platform_work_id,
            kind=item.kind or ("image" if len(assets) > 1 else "video"),
            title=item.title or f"本地补件 {platform_work_id[-8:]}",
            description=item.description or "",
            author_id=item.author_id,
            author_name=item.author_name,
            duration_seconds=item.duration_seconds,
            cover_url=item.cover_url,
            source_url=item.canonical_url or item.input_url,
            media_urls=list(item.media_urls or []),
            image_urls=list(item.image_urls or []),
            raw_metadata=dict(item.raw_metadata or {}),
            library_state="pending",
            processing_state="discovered",
        )
        session.add(work)
        await session.flush()
        session.add(
            CollectionMembership(collection_id=manual_group.id, work_id=work.id)
        )
        for asset in assets:
            asset.work_id = work.id
        item.status = "confirmed"
        item.confirmed_at = utcnow()
        item.existing_work_id = work.id
        work_ids.append(work.id)
    if not work_ids:
        raise ValueError("所选作品均已存在或仍缺少本地补件")
    await session.flush()
    from app.services.jobs import enqueue_ingest_job

    job = await enqueue_ingest_job(session, work_ids)
    await session.commit()
    return job


class ImportCoordinator:
    def __init__(self, *, client: F2WorkClient | None = None):
        self.worker_count = 3
        self.client = client or F2WorkClient()
        self.queue: asyncio.Queue[int | None] = asyncio.Queue(maxsize=90)
        self.tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        async with async_session_factory() as session:
            active_batches = (
                (
                    await session.execute(
                        select(ImportBatch).where(
                            ImportBatch.state.in_({"queued", "running", "cancelling"})
                        )
                    )
                )
                .scalars()
                .all()
            )
            for batch in active_batches:
                batch.cancel_requested = True
                batch.state = "cancelled"
                batch.completed_at = utcnow()
                await session.execute(
                    update(ImportItem)
                    .where(
                        ImportItem.batch_id == batch.id,
                        ImportItem.status.in_(ACTIVE_ITEM_STATES),
                    )
                    .values(
                        status="cancelled",
                        error_code="cancelled_by_user",
                        error_message="应用重启后不会自动恢复作品链接访问",
                    )
                )
                job = await session.get(Job, batch.job_id)
                if job:
                    job.state = "cancelled"
                    job.completed_at = utcnow()
                    job.message = "应用重启后已安全结束链接解析；成功结果仍保留"
            await session.execute(
                update(Job)
                .where(
                    Job.job_type.in_({"ingest", "summarize"}),
                    Job.state.in_({"queued", "running", "cancelling"}),
                )
                .values(
                    state="cancelled",
                    completed_at=utcnow(),
                    message="应用重启后已结束遗留任务，请手动重新发起",
                )
            )
            await session.commit()
        self.tasks = [
            asyncio.create_task(
                self._worker(index + 1), name=f"link-preview-{index + 1}"
            )
            for index in range(self.worker_count)
        ]

    async def stop(self) -> None:
        for _ in self.tasks:
            await self.queue.put(None)
        if self.tasks:
            await asyncio.gather(*self.tasks)
        self.tasks = []

    async def enqueue(self, item_ids: list[int]) -> None:
        for item_id in item_ids:
            await self.queue.put(item_id)

    async def _worker(self, worker_id: int) -> None:
        while True:
            item_id = await self.queue.get()
            try:
                if item_id is None:
                    return
                await self._process_item(item_id, worker_id)
            finally:
                self.queue.task_done()

    async def _process_item(self, item_id: int, worker_id: int) -> None:
        async with async_session_factory() as session:
            item = await session.get(ImportItem, item_id)
            if not item:
                return
            batch = await session.get(ImportBatch, item.batch_id)
            if not batch or batch.cancel_requested or item.status != "queued":
                if item.status == "queued":
                    item.status = "cancelled"
                    item.error_code = "cancelled_by_user"
                    item.error_message = ERROR_MESSAGES["cancelled_by_user"]
                    await _refresh_batch(session, item.batch_id)
                    await session.commit()
                return
            item.status = "resolving"
            item.worker_id = worker_id
            batch.state = "running"
            job = await session.get(Job, batch.job_id)
            if job:
                job.state = "running"
                job.started_at = job.started_at or utcnow()
            await session.commit()
            batch_id = item.batch_id
            input_url = item.normalized_url

        try:
            known_id = direct_work_id(input_url)
            if known_id:
                async with async_session_factory() as session:
                    existing = (
                        await session.execute(
                            select(Work).where(
                                Work.platform == "douyin",
                                Work.platform_work_id == known_id,
                            )
                        )
                    ).scalar_one_or_none()
                    if existing:
                        item = await session.get(ImportItem, item_id)
                        if item:
                            item.status = "duplicate"
                            item.error_code = "already_imported"
                            item.error_message = ERROR_MESSAGES["already_imported"]
                            item.platform_work_id = known_id
                            item.existing_work_id = existing.id
                            item.worker_id = None
                            await _refresh_batch(session, batch_id)
                            await session.commit()
                        return
            result = await resolve_submitted_link(self.client, input_url)
            async with async_session_factory() as session:
                item = await session.get(ImportItem, item_id)
                if not item:
                    return
                existing = (
                    await session.execute(
                        select(Work).where(
                            Work.platform == "douyin",
                            Work.platform_work_id == result.platform_work_id,
                        )
                    )
                ).scalar_one_or_none()
                item.platform_work_id = result.platform_work_id
                item.canonical_url = result.canonical_url
                item.kind = result.kind
                item.title = result.title
                item.description = result.description
                item.author_id = result.author_id
                item.author_name = result.author_name
                item.duration_seconds = result.duration_seconds
                item.cover_url = result.cover_url
                item.media_urls = result.media_urls
                item.image_urls = result.image_urls
                item.raw_metadata = result.raw_metadata
                item.worker_id = None
                if existing:
                    item.status = "duplicate"
                    item.error_code = "already_imported"
                    item.error_message = ERROR_MESSAGES["already_imported"]
                    item.existing_work_id = existing.id
                elif (
                    result.download_permission != "allowed"
                    or result.media_urls
                    or result.image_urls
                ):
                    item.status = "ready"
                    item.error_code = None
                    item.error_message = None
                else:
                    item.status = "needs_local_file"
                    item.error_code = "media_missing"
                    item.error_message = ERROR_MESSAGES["media_missing"]
                await _refresh_batch(session, batch_id)
                await session.commit()
        except PublicLinkError as exc:
            async with async_session_factory() as session:
                item = await session.get(ImportItem, item_id)
                if not item:
                    return
                item.status = "failed"
                item.error_code = exc.code
                item.error_message = str(exc)
                item.worker_id = None
                if exc.opens_circuit:
                    await _mark_batch_blocked(
                        session, batch_id, code=exc.code, message=str(exc)
                    )
                await _refresh_batch(session, batch_id)
                await session.commit()


coordinator = ImportCoordinator()
