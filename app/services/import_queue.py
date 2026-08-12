"""Persistent producer-consumer queue for user-initiated link previews."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from loguru import logger
from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import DATA_DIR
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
from app.services.secrets import SecretUnavailableError, get_secret
from app.services.errors import safe_error_message


ACTIVE_ITEM_STATES = {"queued", "resolving"}
PREVIEW_SUCCESS_STATES = {
    "ready",
    "needs_local_file",
    "duplicate",
    "confirmed",
}
RISK_SETTING_KEY = "f2_access_circuit"
CREATE_IMPORT_LOCK = asyncio.Lock()
PREVIEW_IDENTITY_LOCK = asyncio.Lock()
CONFIRM_IMPORT_LOCK = asyncio.Lock()


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
    session: AsyncSession,
    raw_input: str,
) -> tuple[ImportBatch, list[int], int]:
    # The preflight and commit must be one local critical section. Otherwise,
    # two near-simultaneous submissions can both pass the duplicate check
    # before either one has persisted its preview items.
    async with CREATE_IMPORT_LOCK:
        return await _create_import_batch(session, raw_input)


async def _create_import_batch(
    session: AsyncSession,
    raw_input: str,
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

    prepared: list[
        tuple[
            int,
            str,
            str,
            str | None,
            str,
            str | None,
            str | None,
        ]
    ] = []
    previous_previews = (
        await session.execute(
            select(
                ImportItem.normalized_url,
                ImportItem.platform_work_id,
            ).where(
                ImportItem.status.in_(
                    {"queued", "resolving", "ready", "needs_local_file"}
                )
            )
        )
    ).all()
    previous_urls = {str(row.normalized_url) for row in previous_previews}
    previous_work_ids = {
        str(row.platform_work_id) for row in previous_previews if row.platform_work_id
    }
    seen: set[str] = set()
    duplicate_count = 0
    rejected_count = 0
    for input_url in links:
        status = "queued"
        error_code = None
        error_message = None
        platform_work_id = None
        try:
            normalized = normalize_input_url(input_url)
        except PublicLinkError as exc:
            normalized = (
                "invalid:" f"{hashlib.sha256(input_url.encode()).hexdigest()[:16]}"
            )
            identity = normalized
            status = "failed"
            error_code = exc.code
            error_message = str(exc)
        else:
            platform_work_id = direct_work_id(normalized)
            identity = platform_work_id or normalized
        if (
            identity in seen
            or normalized in previous_urls
            or bool(platform_work_id and platform_work_id in previous_work_ids)
        ):
            duplicate_count += 1
            seen.add(identity)
            continue
        seen.add(identity)
        if len(prepared) >= MAX_BATCH_LINKS:
            rejected_count += 1
            continue
        ordinal = len(prepared) + 1
        prepared.append(
            (
                ordinal,
                input_url,
                normalized,
                platform_work_id,
                status,
                error_code,
                error_message,
            )
        )

    batch_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())
    job = Job(
        id=job_id,
        job_type="link_preview",
        state="queued",
        total_items=len(prepared),
        scope={"batch_id": batch_id},
        progress={
            "rejected_count": rejected_count,
            "deduplicated_count": duplicate_count,
            "duplicates": duplicate_count,
        },
        message=f"等待解析 {len(prepared)} 个链接",
    )
    batch = ImportBatch(
        id=batch_id,
        job_id=job_id,
        raw_input=raw_input[:100_000],
        state="queued",
        total_items=len(prepared),
    )
    # Persist the parent job before the batch.  The models intentionally do not
    # expose an ORM relationship, so SQLAlchemy cannot always infer the insert
    # order even though the physical table has a foreign key to jobs.
    session.add(job)
    await session.flush()
    session.add(batch)
    await session.flush()

    queued_ids: list[int] = []
    for (
        ordinal,
        input_url,
        normalized,
        platform_work_id,
        status,
        error_code,
        error_message,
    ) in prepared:
        item = ImportItem(
            batch_id=batch_id,
            ordinal=ordinal,
            input_url=sanitize_url(input_url) or "",
            normalized_url=normalized,
            platform_work_id=platform_work_id,
            status=status,
            error_code=error_code,
            error_message=error_message,
            raw_metadata={
                "import_provenance": {
                    "source_type": "link",
                    "rights_attested": False,
                    "attested_at": None,
                }
            },
        )
        session.add(item)
        await session.flush()
        if status == "queued":
            queued_ids.append(item.id)
    if not queued_ids:
        await _refresh_batch(session, batch_id)
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
        deduplicated_count = int(progress.get("deduplicated_count", 0) or 0)
        progress.update(
            {
                "completed": completed,
                "ready": counts.get("ready", 0),
                "confirmed": counts.get("confirmed", 0),
                "needs_local_file": counts.get("needs_local_file", 0),
                "duplicates": counts.get("duplicate", 0) + deduplicated_count,
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
    if batch.cancel_requested:
        batch.state = "cancelled"
    elif counts.get("blocked", 0):
        batch.state = "failed"
    elif failed:
        batch.state = "partial"
    else:
        batch.state = "succeeded"
    batch.completed_at = utcnow()
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
    if batch.state not in {"queued", "running", "cancelling", "automating"}:
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
        "source_type": batch.source_type,
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
                "platform": item.platform,
                "client_item_id": item.client_item_id,
                "target_collection_id": item.target_collection_id,
                "input_url": item.input_url,
                "normalized_url": item.normalized_url,
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


async def delete_import_item(session: AsyncSession, item_id: int) -> str:
    """Delete one completed preview result without deleting a confirmed work."""

    item = await session.get(ImportItem, item_id)
    if not item:
        raise LookupError("预检结果不存在")
    if item.status in ACTIVE_ITEM_STATES:
        raise ValueError("正在解析的作品不能删除，请先中断解析")
    batch = await session.get(ImportBatch, item.batch_id)
    if not batch:
        raise LookupError("导入批次不存在")
    job = await session.get(Job, batch.job_id)
    removable_assets = (
        (
            await session.execute(
                select(WorkSourceAsset).where(
                    WorkSourceAsset.import_item_id == item.id,
                    WorkSourceAsset.work_id.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    source_paths = [asset.path for asset in removable_assets]
    for asset in removable_assets:
        await session.delete(asset)
    await session.delete(item)
    batch.total_items = max(0, int(batch.total_items or 0) - 1)
    if job:
        job.total_items = batch.total_items
    await session.flush()
    await _refresh_batch(session, batch.id)
    await session.commit()

    root = (DATA_DIR / "source-assets").resolve()
    for source_path in source_paths:
        try:
            path = Path(source_path).resolve()
        except OSError:
            continue
        if root in path.parents:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                # The database record is already gone; a transient Windows
                # file lock must not turn a successful preview deletion into
                # a misleading API failure.
                continue
    return batch.id


async def confirm_import_items(
    session: AsyncSession,
    batch_id: str,
    item_ids: list[int],
    collection_ids: dict[int, int] | None = None,
) -> dict:
    # Work has a database uniqueness constraint, but serializing this short
    # local transaction also turns simultaneous confirmations into a clean
    # idempotent result instead of exposing an IntegrityError.
    async with CONFIRM_IMPORT_LOCK:
        return await _confirm_import_items(
            session,
            batch_id,
            item_ids,
            collection_ids=collection_ids,
        )


async def _confirm_import_items(
    session: AsyncSession,
    batch_id: str,
    item_ids: list[int],
    collection_ids: dict[int, int] | None = None,
    *,
    commit: bool = True,
) -> dict:
    requested = set(int(item) for item in item_ids)
    requested_collections = {
        int(item_id): int(collection_id)
        for item_id, collection_id in (collection_ids or {}).items()
        if int(item_id) in requested
    }
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
    batch = await session.get(ImportBatch, batch_id)
    if not batch:
        raise LookupError("导入批次不存在")
    stored_collection_ids = {
        int(item.target_collection_id)
        for item in items
        if item.target_collection_id is not None
    }
    all_collection_ids = set(requested_collections.values()) | stored_collection_ids
    collection_map = {
        group.id: group
        for group in (
            (
                await session.execute(
                    select(Collection).where(Collection.id.in_(all_collection_ids))
                )
            )
            .scalars()
            .all()
        )
    }
    missing_collections = all_collection_ids - set(collection_map)
    if missing_collections:
        raise ValueError("所选收藏夹不存在，请刷新收藏夹后重试")
    manual_group = (
        await session.execute(
            select(Collection).where(Collection.key == "manual-import")
        )
    ).scalar_one_or_none()
    if not manual_group:
        manual_group = Collection(key="manual-import", title="手动导入", sort_order=-1)
        session.add(manual_group)
        await session.flush()

    assets_by_item: dict[int, list[WorkSourceAsset]] = {
        int(item.id): [] for item in items
    }
    assets = (
        (
            await session.execute(
                select(WorkSourceAsset)
                .where(WorkSourceAsset.import_item_id.in_(assets_by_item))
                .order_by(WorkSourceAsset.import_item_id, WorkSourceAsset.position)
            )
        )
        .scalars()
        .all()
    )
    for asset in assets:
        if asset.import_item_id is not None:
            assets_by_item.setdefault(int(asset.import_item_id), []).append(asset)

    identity_by_item: dict[int, tuple[str, str]] = {}
    target_collection_by_item: dict[int, int] = {}
    for item in items:
        platform_work_id = item.platform_work_id or (
            "local-" + hashlib.sha256(item.normalized_url.encode()).hexdigest()[:32]
        )
        identity_by_item[int(item.id)] = (item.platform or "douyin", platform_work_id)
        target_collection_by_item[int(item.id)] = requested_collections.get(
            item.id, item.target_collection_id or manual_group.id
        )
    platform_work_ids = {identity[1] for identity in identity_by_item.values()}
    existing_works = (
        (
            await session.execute(
                select(Work).where(Work.platform_work_id.in_(platform_work_ids))
            )
        )
        .scalars()
        .all()
    )
    work_by_identity = {
        (str(work.platform), str(work.platform_work_id)): work for work in existing_works
    }
    existing_work_ids = {int(work.id) for work in existing_works}
    target_collection_ids = set(target_collection_by_item.values()) | {manual_group.id}
    membership_pairs = {
        (int(collection_id), int(work_id))
        for collection_id, work_id in (
            await session.execute(
                select(
                    CollectionMembership.collection_id,
                    CollectionMembership.work_id,
                ).where(
                    CollectionMembership.collection_id.in_(target_collection_ids),
                    CollectionMembership.work_id.in_(existing_work_ids),
                )
            )
        ).all()
    }

    work_ids: list[int] = []
    for item in items:
        assets = assets_by_item[int(item.id)]
        if batch.source_type != "link" and not any(
            asset.kind == "video" and Path(asset.path).is_file() for asset in assets
        ):
            item.status = "needs_local_file"
            item.error_code = "missing_video"
            item.error_message = "已上传视频不存在，请重新上传"
            continue
        if item.status == "needs_local_file" and not assets:
            continue
        platform, platform_work_id = identity_by_item[int(item.id)]
        target_collection_id = target_collection_by_item[int(item.id)]
        target_group = collection_map.get(target_collection_id, manual_group)
        work = work_by_identity.get((platform, platform_work_id))
        if work:
            membership_key = (int(target_group.id), int(work.id))
            if membership_key not in membership_pairs:
                session.add(
                    CollectionMembership(
                        collection_id=target_group.id,
                        work_id=work.id,
                    )
                )
                membership_pairs.add(membership_key)
            for asset in assets:
                asset.work_id = work.id
            if batch.source_type != "link" or work.library_state not in {
                "pending",
                "issues",
            }:
                item.status = "duplicate"
                item.error_code = "already_imported"
                item.error_message = ERROR_MESSAGES["already_imported"]
            else:
                item.status = "confirmed"
                item.error_code = None
                item.error_message = None
                item.confirmed_at = utcnow()
            item.existing_work_id = work.id
            if work.id not in work_ids:
                work_ids.append(work.id)
            continue
        published_at = None
        external_metadata = dict((item.raw_metadata or {}).get("external_import") or {})
        published_value = external_metadata.get("published_at")
        if isinstance(published_value, datetime):
            published_at = published_value
        elif isinstance(published_value, str) and published_value:
            try:
                published_at = datetime.fromisoformat(
                    published_value.replace("Z", "+00:00")
                )
            except ValueError:
                published_at = None
        work = Work(
            platform=platform,
            platform_work_id=platform_work_id,
            kind=item.kind or ("image" if len(assets) > 1 else "video"),
            title=item.title or f"本地补件 {platform_work_id[-8:]}",
            description=item.description or "",
            author_id=item.author_id,
            author_name=item.author_name,
            duration_seconds=item.duration_seconds,
            cover_url=item.cover_url,
            source_url=(
                None
                if item.platform == "local"
                else item.canonical_url or item.input_url
            ),
            media_urls=list(item.media_urls or []),
            image_urls=list(item.image_urls or []),
            raw_metadata=dict(item.raw_metadata or {}),
            import_source=batch.source_type,
            refresh_policy="f2" if batch.source_type == "link" else "never",
            library_state="pending",
            processing_state="discovered",
            published_at=published_at,
        )
        session.add(work)
        await session.flush()
        session.add(
            CollectionMembership(collection_id=target_group.id, work_id=work.id)
        )
        for asset in assets:
            asset.work_id = work.id
        item.status = "confirmed"
        item.confirmed_at = utcnow()
        item.existing_work_id = work.id
        if work.id not in work_ids:
            work_ids.append(work.id)
    if not work_ids:
        raise ValueError("所选作品均已存在或仍缺少本地补件")
    await session.flush()
    if commit:
        await session.commit()
    return {
        "confirmed_count": len(work_ids),
        "work_ids": work_ids,
        "library_state": "pending",
    }


async def _fail_preview_item(
    item_id: int,
    batch_id: str,
    *,
    code: str,
    message: str,
    opens_circuit: bool = False,
) -> None:
    async with async_session_factory() as session:
        item = await session.get(ImportItem, item_id)
        if not item or item.status not in ACTIVE_ITEM_STATES:
            return
        item.status = "failed"
        item.error_code = code
        item.error_message = message
        item.worker_id = None
        if opens_circuit:
            await _mark_batch_blocked(
                session,
                batch_id,
                code=code,
                message=message,
            )
        await _refresh_batch(session, batch_id)
        await session.commit()


class ImportCoordinator:
    def __init__(self, *, client: F2WorkClient | None = None):
        self.worker_count = 3
        self.client = client or F2WorkClient()
        self.queue: asyncio.Queue[int | None] = asyncio.Queue(maxsize=90)
        self.tasks: list[asyncio.Task] = []
        self.last_error: str | None = None

    def health_snapshot(self) -> dict[str, object]:
        alive_tasks = [task for task in self.tasks if not task.done()]
        for task in self.tasks:
            if task.done() and not task.cancelled():
                error = task.exception()
                if error is not None:
                    self.last_error = safe_error_message(error)
        return {
            "name": "link_preview",
            "alive": len(alive_tasks) == self.worker_count,
            "workers_alive": len(alive_tasks),
            "workers_expected": self.worker_count,
            "last_error": self.last_error,
        }

    async def start(self) -> None:
        self.last_error = None
        async with async_session_factory() as session:
            active_batches = (
                (
                    await session.execute(
                        select(ImportBatch).where(
                            ImportBatch.state.in_(
                                {"queued", "running", "cancelling", "automating"}
                            )
                        )
                    )
                )
                .scalars()
                .all()
            )
            for batch in active_batches:
                job = await session.get(Job, batch.job_id)
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
                        worker_id=None,
                        error_code="cancelled_by_user",
                        error_message="应用重启后不会自动恢复作品链接访问",
                    )
                )
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
        tasks = list(self.tasks)
        self.tasks = []
        for task in tasks:
            task.cancel()
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True), timeout=5
                )
            except TimeoutError:
                logger.error("链接预检 worker 未能在 5 秒内停止")
        while True:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                self.queue.task_done()

    async def enqueue(self, item_ids: list[int]) -> None:
        for item_id in item_ids:
            await self.queue.put(item_id)

    async def _worker(self, worker_id: int) -> None:
        while True:
            item_id = await self.queue.get()
            try:
                if item_id is None:
                    return
                try:
                    await self._process_item(item_id, worker_id)
                except Exception as exc:
                    self.last_error = safe_error_message(exc)
                    # A single malformed response or transient database error
                    # must not terminate one of the three persistent workers.
                    logger.exception(
                        "链接预检 worker {} 处理条目 {} 时失败",
                        worker_id,
                        item_id,
                    )
            finally:
                self.queue.task_done()

    async def _process_item(self, item_id: int, worker_id: int) -> None:
        async with async_session_factory() as session:
            item = await session.get(ImportItem, item_id)
            if not item:
                return
            batch_id = item.batch_id
        try:
            await self._process_item_inner(item_id, worker_id)
        except SecretUnavailableError:
            await _fail_preview_item(
                item_id,
                batch_id,
                code="f2_cookie_unreadable",
                message="F2 Cookie 无法解密，请在设置中重新保存",
            )
        except Exception as exc:
            self.last_error = safe_error_message(exc)
            logger.exception("链接预检条目发生未知错误: {}", item_id)
            await _fail_preview_item(
                item_id,
                batch_id,
                code="preview_internal_error",
                message="链接解析发生内部错误，请稍后重试",
            )

    async def _process_item_inner(self, item_id: int, worker_id: int) -> None:
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
            async with PREVIEW_IDENTITY_LOCK:
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
                    previous_preview = await session.scalar(
                        select(ImportItem.id)
                        .where(
                            ImportItem.id != item.id,
                            ImportItem.platform_work_id == result.platform_work_id,
                            ImportItem.status.in_(
                                {
                                    "ready",
                                    "needs_local_file",
                                }
                            ),
                        )
                        .order_by(ImportItem.id)
                        .limit(1)
                    )
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
                    resolved_metadata = dict(result.raw_metadata or {})
                    resolved_metadata.pop("import_provenance", None)
                    previous_metadata = (
                        dict(item.raw_metadata or {})
                        if isinstance(item.raw_metadata, dict)
                        else {}
                    )
                    provenance = previous_metadata.get("import_provenance")
                    if isinstance(provenance, dict):
                        # F2 owns discovery metadata, while provenance is a
                        # controlled local attestation and must never be
                        # overwritten by an upstream response.
                        resolved_metadata["import_provenance"] = dict(provenance)
                    item.raw_metadata = resolved_metadata
                    item.worker_id = None
                    if existing:
                        item.status = "duplicate"
                        item.error_code = "already_imported"
                        item.error_message = ERROR_MESSAGES["already_imported"]
                        item.existing_work_id = existing.id
                    elif previous_preview:
                        item.status = "duplicate"
                        item.error_code = "duplicate_input"
                        item.error_message = ERROR_MESSAGES["duplicate_input"]
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
