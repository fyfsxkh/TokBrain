"""Local-video and authenticated external import orchestration."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AppSetting,
    Collection,
    CollectionMembership,
    ImportBatch,
    ImportItem,
    Job,
    Work,
    WorkSourceAsset,
)
from app.services.f2_links import sanitize_url
from app.services.import_queue import batch_view, confirm_import_items
from app.services.jobs import enqueue_ingest_job


INTEGRATION_TOKEN_SETTING = "external_import_token"
MAX_EXTERNAL_METADATA_BYTES = 64 * 1024
MAX_EXTERNAL_REQUEST_BYTES = 2 * 1024 * 1024
_IMPORT_LOCK = asyncio.Lock()


class IntegrationImportError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        retryable: bool = False,
        field: str | None = None,
    ):
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        self.field = field
        super().__init__(message)

    def detail(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "field": self.field,
        }


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    if len(encoded) > MAX_EXTERNAL_REQUEST_BYTES:
        raise IntegrationImportError(
            "request_too_large",
            "JSON 清单超过 2 MB 上限",
            status_code=413,
            field="body",
        )
    return hashlib.sha256(encoded).hexdigest()


def _safe_filename(value: object) -> str:
    name = Path(str(value or "video")).name
    for char in '<>:"/\\|?*':
        name = name.replace(char, "_")
    return " ".join(name.split()).strip(" ._")[:260] or "video"


def _item_payload(item: Any) -> dict[str, Any]:
    if hasattr(item, "model_dump"):
        return item.model_dump(mode="json", exclude_none=True)
    return dict(item)


async def _collection_ids(
    session: AsyncSession, identifiers: Iterable[int | None]
) -> set[int]:
    requested = {int(value) for value in identifiers if value is not None}
    if not requested:
        return set()
    found = set(
        (
            await session.execute(
                select(Collection.id).where(Collection.id.in_(requested))
            )
        ).scalars()
    )
    missing = requested - found
    if missing:
        raise IntegrationImportError(
            "collection_not_found",
            f"收藏夹不存在：{min(missing)}",
            status_code=422,
            field="target_collection_id",
        )
    return found


async def integration_token_status(session: AsyncSession) -> dict[str, object]:
    row = await session.get(AppSetting, INTEGRATION_TOKEN_SETTING)
    value = dict(row.value or {}) if row and isinstance(row.value, dict) else {}
    return {
        "configured": bool(value.get("sha256")),
        "prefix": value.get("prefix"),
        "created_at": value.get("created_at"),
    }


async def rotate_integration_token(session: AsyncSession) -> dict[str, object]:
    plaintext = f"tb_{secrets.token_urlsafe(32)}"
    created_at = _utcnow().isoformat(timespec="seconds") + "Z"
    value = {
        "sha256": _sha256(plaintext),
        "prefix": plaintext[:11],
        "created_at": created_at,
    }
    row = await session.get(AppSetting, INTEGRATION_TOKEN_SETTING)
    if row:
        row.value = value
    else:
        session.add(AppSetting(key=INTEGRATION_TOKEN_SETTING, value=value))
    await session.commit()
    return {
        "configured": True,
        "prefix": value["prefix"],
        "created_at": value["created_at"],
        "token": plaintext,
    }


async def revoke_integration_token(session: AsyncSession) -> dict[str, object]:
    await session.execute(
        delete(AppSetting).where(AppSetting.key == INTEGRATION_TOKEN_SETTING)
    )
    await session.commit()
    return {"configured": False, "prefix": None, "created_at": None}


async def verify_integration_token(
    session: AsyncSession, authorization: str | None
) -> None:
    row = await session.get(AppSetting, INTEGRATION_TOKEN_SETTING)
    value = dict(row.value or {}) if row and isinstance(row.value, dict) else {}
    expected = str(value.get("sha256") or "")
    if not expected:
        raise IntegrationImportError(
            "token_not_configured",
            "请先在设置页生成外部导入令牌",
            status_code=503,
        )
    scheme, separator, token = (authorization or "").partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not token.strip():
        raise IntegrationImportError(
            "authentication_required",
            "需要有效的 Bearer 导入令牌",
            status_code=401,
        )
    if not hmac.compare_digest(_sha256(token.strip()), expected):
        raise IntegrationImportError(
            "invalid_token",
            "外部导入令牌无效或已撤销",
            status_code=401,
        )


async def _new_terminal_batch(
    session: AsyncSession,
    *,
    source_type: str,
    total_items: int,
    raw_input: dict[str, object],
    idempotency_key_hash: str | None = None,
    request_digest: str | None = None,
) -> ImportBatch:
    now = _utcnow()
    job = Job(
        id=str(uuid.uuid4()),
        job_type="local_import" if source_type == "local_upload" else "external_import",
        state="succeeded",
        progress={
            "processed": 0,
            "completed": 0,
            "total": total_items,
            "ready": 0,
            "needs_local_file": total_items,
            "duplicates": 0,
            "failed": 0,
            "cancelled": 0,
        },
        total_items=total_items,
        processed_items=0,
        message="导入清单已创建，等待上传视频",
        completed_at=now,
    )
    session.add(job)
    await session.flush()
    batch = ImportBatch(
        id=str(uuid.uuid4()),
        job_id=job.id,
        source_type=source_type,
        idempotency_key_hash=idempotency_key_hash,
        request_digest=request_digest,
        raw_input=json.dumps(raw_input, ensure_ascii=False, separators=(",", ":")),
        state="succeeded",
        total_items=total_items,
        completed_at=now,
    )
    session.add(batch)
    await session.flush()
    return batch


async def create_local_import_batch(
    session: AsyncSession, *, rights_attested: bool, items: list[Any]
) -> dict:
    if not rights_attested:
        raise IntegrationImportError(
            "rights_attestation_required",
            "必须确认有权处理这些视频文件",
            status_code=422,
            field="rights_attested",
        )
    if not 1 <= len(items) <= 10:
        raise IntegrationImportError(
            "invalid_item_count",
            "一次请选择 1 至 10 个视频",
            status_code=422,
            field="items",
        )
    rows = [_item_payload(item) for item in items]
    client_ids = [str(row["client_item_id"]) for row in rows]
    if len(set(client_ids)) != len(client_ids):
        raise IntegrationImportError(
            "duplicate_client_item_id",
            "同一批次的 client_item_id 不能重复",
            status_code=422,
            field="items.client_item_id",
        )
    await _collection_ids(session, (row.get("target_collection_id") for row in rows))
    attested_at = _utcnow().isoformat(timespec="seconds") + "Z"
    async with _IMPORT_LOCK:
        batch = await _new_terminal_batch(
            session,
            source_type="local_upload",
            total_items=len(rows),
            raw_input={
                "rights_attested": True,
                "attested_at": attested_at,
                "files": [_safe_filename(row.get("filename")) for row in rows],
            },
        )
        for ordinal, row in enumerate(rows, 1):
            filename = _safe_filename(row.get("filename"))
            title = str(row.get("title") or Path(filename).stem or "本地视频")[:500]
            client_item_id = str(row["client_item_id"])
            session.add(
                ImportItem(
                    batch_id=batch.id,
                    ordinal=ordinal,
                    platform="local",
                    client_item_id=client_item_id,
                    target_collection_id=row.get("target_collection_id"),
                    input_url=f"local://{client_item_id}",
                    normalized_url=f"local://{client_item_id}",
                    platform_work_id=None,
                    kind="video",
                    title=title,
                    description=str(row.get("description") or "")[:20_000] or None,
                    status="needs_local_file",
                    raw_metadata={
                        "import_provenance": {
                            "source_type": "local_upload",
                            "rights_attested": True,
                            "attested_at": attested_at,
                            "original_filename": filename,
                            "declared_size_bytes": int(row["size_bytes"]),
                        }
                    },
                )
            )
        await session.commit()
    return await batch_view(session, batch.id)


def _external_source_url(value: object, platform_work_id: str) -> str:
    if not value:
        return f"https://www.douyin.com/video/{platform_work_id}"
    url = sanitize_url(str(value))
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (
        hostname == "douyin.com" or hostname.endswith(".douyin.com")
    ):
        raise IntegrationImportError(
            "invalid_source_url",
            "source_url 仅接受 HTTPS 抖音作品链接",
            status_code=422,
            field="items.source_url",
        )
    return url


def _validate_extra_metadata(value: object) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise IntegrationImportError(
            "invalid_metadata",
            "extra_metadata 必须是 JSON 对象",
            status_code=422,
            field="items.extra_metadata",
        )
    size = len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    if size > MAX_EXTERNAL_METADATA_BYTES:
        raise IntegrationImportError(
            "metadata_too_large",
            "单条 extra_metadata 超过 64 KB 上限",
            status_code=422,
            field="items.extra_metadata",
        )
    return dict(value)


async def create_external_import_batch(
    session: AsyncSession,
    *,
    idempotency_key: str,
    rights_attested: bool,
    items: list[Any],
) -> dict[str, object]:
    if not 8 <= len(idempotency_key) <= 200:
        raise IntegrationImportError(
            "invalid_idempotency_key",
            "Idempotency-Key 长度必须为 8 至 200 个字符",
            status_code=422,
            field="Idempotency-Key",
        )
    if not rights_attested:
        raise IntegrationImportError(
            "rights_attestation_required",
            "必须确认有权处理这些视频文件",
            status_code=422,
            field="rights_attested",
        )
    if not 1 <= len(items) <= 100:
        raise IntegrationImportError(
            "invalid_item_count",
            "每批清单必须包含 1 至 100 条作品",
            status_code=422,
            field="items",
        )
    rows = [_item_payload(item) for item in items]
    client_ids = [str(row["client_item_id"]) for row in rows]
    if len(set(client_ids)) != len(client_ids):
        raise IntegrationImportError(
            "duplicate_client_item_id",
            "同一批次的 client_item_id 不能重复",
            status_code=422,
            field="items.client_item_id",
        )
    for row in rows:
        row["title"] = str(row.get("title") or "").strip()
        if not row["title"]:
            raise IntegrationImportError(
                "invalid_title",
                "title 不能为空",
                status_code=422,
                field="items.title",
            )
        row["extra_metadata"] = _validate_extra_metadata(row.get("extra_metadata"))
        row["source_url"] = _external_source_url(
            row.get("source_url"), str(row["platform_work_id"])
        )
    await _collection_ids(session, (row.get("target_collection_id") for row in rows))
    digest = _canonical_digest(
        {"rights_attested": True, "items": rows, "contract_version": 1}
    )
    key_hash = _sha256(idempotency_key)

    async with _IMPORT_LOCK:
        existing = await session.scalar(
            select(ImportBatch).where(ImportBatch.idempotency_key_hash == key_hash)
        )
        if existing:
            if existing.request_digest != digest:
                raise IntegrationImportError(
                    "idempotency_conflict",
                    "相同 Idempotency-Key 已用于不同清单",
                    status_code=409,
                    field="Idempotency-Key",
                )
            view = await external_batch_view(session, existing.id)
            return {**view, "replayed": True}

        work_ids = {str(row["platform_work_id"]) for row in rows}
        existing_works = {
            work.platform_work_id: work
            for work in (
                (
                    await session.execute(
                        select(Work).where(
                            Work.platform == "douyin",
                            Work.platform_work_id.in_(work_ids),
                        )
                    )
                )
                .scalars()
                .all()
            )
        }
        attested_at = _utcnow().isoformat(timespec="seconds") + "Z"
        batch = await _new_terminal_batch(
            session,
            source_type="external_batch",
            total_items=len(rows),
            raw_input={
                "rights_attested": True,
                "attested_at": attested_at,
                "item_count": len(rows),
            },
            idempotency_key_hash=key_hash,
            request_digest=digest,
        )
        seen_platform_ids: set[str] = set()
        for ordinal, row in enumerate(rows, 1):
            platform_work_id = str(row["platform_work_id"])
            work = existing_works.get(platform_work_id)
            duplicate_input = platform_work_id in seen_platform_ids
            seen_platform_ids.add(platform_work_id)
            metadata = {
                "external_import": {
                    "author_id": row.get("author_id"),
                    "author_name": row.get("author_name"),
                    "published_at": row.get("published_at"),
                    "expected_sha256": row.get("expected_sha256"),
                    "extra_metadata": row.get("extra_metadata") or {},
                },
                "import_provenance": {
                    "source_type": "external_batch",
                    "rights_attested": True,
                    "attested_at": attested_at,
                },
            }
            session.add(
                ImportItem(
                    batch_id=batch.id,
                    ordinal=ordinal,
                    platform="douyin",
                    client_item_id=str(row["client_item_id"]),
                    target_collection_id=row.get("target_collection_id"),
                    input_url=str(row["source_url"]),
                    normalized_url=str(row["source_url"]),
                    platform_work_id=platform_work_id,
                    kind="video",
                    title=str(row["title"])[:500],
                    description=str(row.get("description") or "")[:20_000] or None,
                    author_id=str(row.get("author_id") or "")[:120] or None,
                    author_name=str(row.get("author_name") or "")[:200] or None,
                    duration_seconds=float(row.get("duration_seconds") or 0),
                    existing_work_id=work.id if work else None,
                    status=(
                        "duplicate" if work or duplicate_input else "needs_local_file"
                    ),
                    error_code=(
                        "already_imported"
                        if work
                        else "duplicate_platform_work_id" if duplicate_input else None
                    ),
                    error_message=(
                        "该抖音作品已经导入"
                        if work
                        else "本批清单中作品 ID 重复" if duplicate_input else None
                    ),
                    raw_metadata=metadata,
                )
            )
        await session.commit()
    view = await external_batch_view(session, batch.id)
    return {**view, "replayed": False}


async def refresh_manifest_progress(session: AsyncSession, batch_id: str) -> None:
    batch = await session.get(ImportBatch, batch_id)
    if not batch or batch.source_type == "link":
        return
    items = (
        (
            await session.execute(
                select(ImportItem).where(ImportItem.batch_id == batch_id)
            )
        )
        .scalars()
        .all()
    )
    counts: dict[str, int] = {}
    for item in items:
        counts[item.status] = counts.get(item.status, 0) + 1
    processed_items = sum(
        counts.get(state, 0) for state in ("ready", "duplicate", "invalid", "confirmed")
    )
    job = await session.get(Job, batch.job_id)
    if job:
        job.processed_items = processed_items
        job.failed_items = counts.get("invalid", 0) + counts.get("failed", 0)
        job.progress = {
            "total": len(items),
            "completed": processed_items + counts.get("failed", 0),
            "duplicates": counts.get("duplicate", 0),
            **counts,
        }
    await session.commit()


async def update_import_item(
    session: AsyncSession,
    *,
    item_id: int,
    title: str | None = None,
    description: str | None = None,
    description_provided: bool = False,
    target_collection_id: int | None = None,
    target_collection_provided: bool = False,
) -> dict[str, object]:
    item = await session.get(ImportItem, item_id)
    if not item:
        raise IntegrationImportError(
            "item_not_found", "导入条目不存在", status_code=404
        )
    batch = await session.get(ImportBatch, item.batch_id)
    if not batch or batch.source_type not in {
        "local_upload",
        "external_batch",
        "package_upload",
    }:
        raise IntegrationImportError(
            "item_not_editable", "该条目不支持此编辑接口", status_code=409
        )
    if item.status in {"confirmed", "duplicate"}:
        raise IntegrationImportError(
            "item_not_editable", "已提交或重复条目不能再编辑", status_code=409
        )
    if target_collection_provided and target_collection_id is not None:
        await _collection_ids(session, [target_collection_id])
    if title is not None:
        clean_title = title.strip()
        if not clean_title:
            raise IntegrationImportError(
                "invalid_title",
                "标题不能为空",
                status_code=422,
                field="title",
            )
        item.title = clean_title
    if description_provided:
        item.description = description.strip() if description else None
    if target_collection_provided:
        item.target_collection_id = target_collection_id
    await session.commit()
    return {
        "item_id": item.id,
        "title": item.title,
        "description": item.description,
        "target_collection_id": item.target_collection_id,
    }


def _external_item_view(item: ImportItem) -> dict[str, object]:
    metadata = dict(item.raw_metadata or {})
    external = dict(metadata.get("external_import") or {})
    error = None
    if item.error_code:
        error = {
            "code": item.error_code,
            "message": item.error_message or item.error_code,
            "retryable": False,
            "field": None,
        }
    upload_url = None
    if item.status in {"needs_local_file", "ready"}:
        upload_url = (
            f"/api/integrations/v1/import-batches/{item.batch_id}/items/"
            f"{item.client_item_id}/asset"
        )
    return {
        "client_item_id": item.client_item_id,
        "item_id": item.id,
        "platform_work_id": item.platform_work_id,
        "status": item.status,
        "existing_work_id": item.existing_work_id,
        "expected_sha256": external.get("expected_sha256"),
        "upload_url": upload_url,
        "error": error,
    }


async def external_batch_view(
    session: AsyncSession, batch_id: str
) -> dict[str, object]:
    batch = await session.get(ImportBatch, batch_id)
    if not batch or batch.source_type != "external_batch":
        raise IntegrationImportError(
            "batch_not_found", "外部导入批次不存在", status_code=404
        )
    items = (
        (
            await session.execute(
                select(ImportItem)
                .where(ImportItem.batch_id == batch_id)
                .order_by(ImportItem.id.asc())
            )
        )
        .scalars()
        .all()
    )
    return {
        "batch_id": batch.id,
        "state": batch.state,
        "source_type": batch.source_type,
        "items": [_external_item_view(item) for item in items],
    }


async def external_item_by_client_id(
    session: AsyncSession, *, batch_id: str, client_item_id: str
) -> ImportItem:
    item = await session.scalar(
        select(ImportItem).where(
            ImportItem.batch_id == batch_id,
            ImportItem.client_item_id == client_item_id,
        )
    )
    if not item:
        raise IntegrationImportError(
            "item_not_found", "批次条目不存在", status_code=404
        )
    batch = await session.get(ImportBatch, batch_id)
    if not batch or batch.source_type != "external_batch":
        raise IntegrationImportError(
            "batch_not_found", "外部导入批次不存在", status_code=404
        )
    return item


async def commit_external_batch(
    session: AsyncSession, *, batch_id: str, start_processing: bool
) -> dict[str, object]:
    async with _IMPORT_LOCK:
        return await _commit_external_batch(
            session,
            batch_id=batch_id,
            start_processing=start_processing,
        )


async def _commit_external_batch(
    session: AsyncSession, *, batch_id: str, start_processing: bool
) -> dict[str, object]:
    batch = await session.get(ImportBatch, batch_id)
    if not batch or batch.source_type != "external_batch":
        raise IntegrationImportError(
            "batch_not_found", "外部导入批次不存在", status_code=404
        )
    items = (
        (
            await session.execute(
                select(ImportItem)
                .where(ImportItem.batch_id == batch_id)
                .order_by(ImportItem.id.asc())
            )
        )
        .scalars()
        .all()
    )
    ready_item_ids = {item.id for item in items if item.status == "ready"}
    if ready_item_ids:
        uploaded_assets = (
            (
                await session.execute(
                    select(WorkSourceAsset).where(
                        WorkSourceAsset.import_item_id.in_(ready_item_ids),
                        WorkSourceAsset.kind == "video",
                    )
                )
            )
            .scalars()
            .all()
        )
        valid_ready_ids = {
            int(asset.import_item_id)
            for asset in uploaded_assets
            if asset.import_item_id is not None and Path(asset.path).is_file()
        }
        for item in items:
            if item.status == "ready" and item.id not in valid_ready_ids:
                item.status = "needs_local_file"
                item.error_code = "missing_video"
                item.error_message = "已上传视频不存在，请重新上传"
        await session.commit()
    ready_ids = [item.id for item in items if item.status == "ready"]
    if ready_ids:
        await confirm_import_items(session, batch_id, ready_ids)
    unresolved_duplicates = [
        item
        for item in items
        if item.status == "duplicate"
        and not item.existing_work_id
        and item.platform_work_id
    ]
    if unresolved_duplicates:
        works_by_platform_id = {
            work.platform_work_id: work.id
            for work in (
                (
                    await session.execute(
                        select(Work).where(
                            Work.platform == "douyin",
                            Work.platform_work_id.in_(
                                {
                                    item.platform_work_id
                                    for item in unresolved_duplicates
                                }
                            ),
                        )
                    )
                )
                .scalars()
                .all()
            )
        }
        for item in unresolved_duplicates:
            item.existing_work_id = works_by_platform_id.get(item.platform_work_id)
        await session.commit()
    # A duplicate may still request a collection membership. These inserts are idempotent.
    duplicate_items = [
        item
        for item in items
        if item.status == "duplicate"
        and item.existing_work_id
        and item.target_collection_id
    ]
    if duplicate_items:
        pairs = {
            (row.collection_id, row.work_id)
            for row in (
                await session.execute(
                    select(CollectionMembership).where(
                        CollectionMembership.work_id.in_(
                            {item.existing_work_id for item in duplicate_items}
                        )
                    )
                )
            ).scalars()
        }
        for item in duplicate_items:
            pair = (int(item.target_collection_id), int(item.existing_work_id))
            if pair not in pairs:
                session.add(
                    CollectionMembership(collection_id=pair[0], work_id=pair[1])
                )
                pairs.add(pair)
        await session.commit()

    items = (
        (
            await session.execute(
                select(ImportItem)
                .where(ImportItem.batch_id == batch_id)
                .order_by(ImportItem.id.asc())
            )
        )
        .scalars()
        .all()
    )
    work_ids = sorted(
        {
            int(item.existing_work_id)
            for item in items
            if item.existing_work_id and item.status in {"confirmed", "duplicate"}
        }
    )
    job_payload = None
    if start_processing and work_ids:
        try:
            job = await enqueue_ingest_job(session, work_ids)
            job_payload = {"job_id": job.id, "state": job.state}
        except ValueError:
            job_payload = None

    results: list[dict[str, object]] = []
    for item in items:
        error = None
        if item.status == "needs_local_file":
            result_status = "missing_video"
            error = {
                "code": "missing_video",
                "message": "尚未上传视频",
                "retryable": True,
                "field": "asset",
            }
        elif item.status == "duplicate":
            result_status = "duplicate" if item.existing_work_id else "invalid"
            if not item.existing_work_id:
                error = {
                    "code": item.error_code or "invalid",
                    "message": item.error_message or "条目无效",
                    "retryable": False,
                    "field": "platform_work_id",
                }
        elif item.status == "confirmed":
            result_status = "imported"
        elif item.status in {"invalid", "failed"}:
            result_status = "invalid"
            error = {
                "code": item.error_code or "invalid",
                "message": item.error_message or "条目无效",
                "retryable": False,
                "field": None,
            }
        else:
            result_status = "invalid"
            error = {
                "code": "invalid_state",
                "message": f"条目状态无法提交：{item.status}",
                "retryable": False,
                "field": None,
            }
        results.append(
            {
                "client_item_id": item.client_item_id,
                "status": result_status,
                "work_id": item.existing_work_id,
                "error": error,
            }
        )
    await refresh_manifest_progress(session, batch_id)
    return {
        "batch_id": batch_id,
        "results": results,
        "work_ids": work_ids,
        "job": job_payload,
    }
