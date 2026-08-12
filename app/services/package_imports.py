"""Durable, local-only imports for folders and ZIP files produced by other tools."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import re
import shutil
import sqlite3
import uuid
import zipfile
from datetime import timedelta
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit
from weakref import WeakValueDictionary

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import DATA_DIR
from app.database import async_session_factory
from app.models import (
    Collection,
    ImportBatch,
    ImportItem,
    Job,
    PackageImportFile,
    Work,
    WorkSourceAsset,
    utcnow,
)
from app.services.import_integrations import (
    IntegrationImportError,
    refresh_manifest_progress,
)
from app.services.errors import safe_error_message
from app.services.import_queue import batch_view
from app.services.local_assets import (
    LocalAssetError,
    _kind_and_extension,
    _verify_video,
)


MAX_PACKAGE_VIDEOS = 100
MAX_PACKAGE_ENTRIES = 1000
MAX_VIDEO_BYTES = 1024 * 1024 * 1024
MAX_PACKAGE_BYTES = 20 * 1024 * 1024 * 1024
MAX_METADATA_BYTES = 64 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200
PACKAGE_RETENTION_DAYS = 7
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm"}
METADATA_EXTENSIONS = {".json", ".csv", ".db", ".sqlite", ".sqlite3"}
DOUYIN_ID_RE = re.compile(r"(?<!\d)(\d{15,22})(?!\d)")
PACKAGE_LOCK = asyncio.Lock()
PACKAGE_STATE_LOCK = asyncio.Lock()
PACKAGE_SHUTDOWN_TIMEOUT_SECONDS = 30.0
_PACKAGE_FILE_LOCKS: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


def _package_file_lock(file_id: str) -> asyncio.Lock:
    lock = _PACKAGE_FILE_LOCKS.get(file_id)
    if lock is None:
        lock = asyncio.Lock()
        _PACKAGE_FILE_LOCKS[file_id] = lock
    return lock


def _error(
    code: str, message: str, *, status_code: int = 422, field: str | None = None
):
    return IntegrationImportError(code, message, status_code=status_code, field=field)


def _safe_relative_path(value: object) -> str:
    raw = str(value or "").replace("\\", "/").strip()
    if not raw or "\x00" in raw or raw.startswith(("/", "//")):
        raise _error(
            "invalid_relative_path", "文件路径无效", field="files.relative_path"
        )
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise _error(
            "invalid_relative_path",
            "文件路径不能包含绝对路径或 ..",
            field="files.relative_path",
        )
    if len(path.parts) and re.match(r"^[A-Za-z]:$", path.parts[0]):
        raise _error(
            "invalid_relative_path", "文件路径不能包含盘符", field="files.relative_path"
        )
    normalized = path.as_posix()
    if len(normalized) > 1000:
        raise _error(
            "invalid_relative_path", "文件路径过长", field="files.relative_path"
        )
    return normalized


def _path_hash(path: str) -> str:
    return hashlib.sha256(path.casefold().encode("utf-8")).hexdigest()


def _role(path: str) -> str:
    suffix = Path(path).suffix.lower()
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    if suffix == ".zip":
        return "archive"
    if suffix in METADATA_EXTENSIONS:
        return "metadata"
    return "unknown"


def _scope(job: Job | None) -> dict[str, Any]:
    return dict(job.scope or {}) if job and isinstance(job.scope, dict) else {}


async def create_package_batch(
    session: AsyncSession,
    *,
    rights_attested: bool,
    upload_mode: str,
    target_collection_id: int | None,
    files: list[Any],
) -> dict[str, Any]:
    if not rights_attested:
        raise _error(
            "rights_attestation_required",
            "必须确认有权处理这些文件",
            field="rights_attested",
        )
    if target_collection_id is not None and not await session.get(
        Collection, target_collection_id
    ):
        raise _error(
            "collection_not_found", "所选收藏夹不存在", field="target_collection_id"
        )
    rows = [
        item.model_dump() if hasattr(item, "model_dump") else dict(item)
        for item in files
    ]
    if not 1 <= len(rows) <= MAX_PACKAGE_ENTRIES:
        raise _error(
            "invalid_file_count",
            "每个数据包必须包含 1 至 1000 个候选文件",
            field="files",
        )
    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    total_bytes = 0
    for row in rows:
        client_id = str(row.get("client_file_id") or "")
        path = _safe_relative_path(row.get("relative_path"))
        role = _role(path)
        size = int(row.get("size_bytes") or 0)
        if client_id in seen_ids:
            raise _error(
                "duplicate_client_file_id",
                "client_file_id 不能重复",
                field="files.client_file_id",
            )
        hashed = _path_hash(path)
        if hashed in seen_paths:
            raise _error(
                "duplicate_path",
                "数据包内文件路径不能重复（不区分大小写）",
                field="files.relative_path",
            )
        if role == "unknown":
            raise _error(
                "unsupported_file",
                f"不支持的文件类型：{Path(path).suffix or path}",
                field="files.relative_path",
            )
        seen_ids.add(client_id)
        seen_paths.add(hashed)
        total_bytes += size
        normalized.append(
            {
                **row,
                "client_file_id": client_id,
                "relative_path": path,
                "path_hash": hashed,
                "role": role,
            }
        )
    if total_bytes > MAX_PACKAGE_BYTES:
        raise _error(
            "package_too_large",
            "数据包声明总大小超过 20 GB",
            status_code=413,
            field="files",
        )
    video_count = sum(row["role"] == "video" for row in normalized)
    if upload_mode == "zip":
        if len(normalized) != 1 or normalized[0]["role"] != "archive":
            raise _error(
                "invalid_zip_manifest", "ZIP 模式只能提交一个 .zip 文件", field="files"
            )
    elif upload_mode == "folder":
        if not video_count:
            raise _error("missing_video", "文件夹中至少需要一个视频", field="files")
        if video_count > MAX_PACKAGE_VIDEOS:
            raise _error("too_many_videos", "每批最多导入 100 个视频", field="files")
    else:
        raise _error(
            "invalid_upload_mode",
            "upload_mode 必须是 folder 或 zip",
            field="upload_mode",
        )

    now = utcnow()
    batch_id = str(uuid.uuid4())
    job = Job(
        id=str(uuid.uuid4()),
        job_type="package_analyze",
        state="uploading",
        scope={
            "batch_id": batch_id,
            "upload_mode": upload_mode,
            "target_collection_id": target_collection_id,
            "rights_attested": True,
            "attested_at": now.isoformat(),
            "analysis_state": "waiting_upload",
        },
        progress={
            "files_total": len(normalized),
            "files_uploaded": 0,
            "videos_found": 0,
        },
        total_items=len(normalized),
        message="等待上传外部视频数据包",
    )
    session.add(job)
    await session.flush()
    batch = ImportBatch(
        id=batch_id,
        job_id=job.id,
        raw_input=json.dumps(
            {"upload_mode": upload_mode, "file_count": len(normalized)},
            ensure_ascii=False,
        ),
        source_type="package_upload",
        state="uploading",
        total_items=0,
    )
    session.add(batch)
    await session.flush()
    for row in normalized:
        session.add(
            PackageImportFile(
                id=str(uuid.uuid4()),
                batch_id=batch_id,
                client_file_id=row["client_file_id"],
                relative_path=row["relative_path"],
                path_hash=row["path_hash"],
                role=row["role"],
                status="pending",
                declared_size=int(row["size_bytes"]),
                created_at=now,
                updated_at=now,
            )
        )
    await session.commit()
    return await package_batch_view(session, batch_id)


async def upload_package_file(
    session: AsyncSession,
    *,
    batch_id: str,
    file_id: str,
    upload: Any,
    replace: bool = False,
) -> dict[str, Any]:
    try:
        async with _package_file_lock(file_id):
            return await _upload_package_file(
                session,
                batch_id=batch_id,
                file_id=file_id,
                upload=upload,
                replace=replace,
            )
    finally:
        await upload.close()


async def _upload_package_file(
    session: AsyncSession,
    *,
    batch_id: str,
    file_id: str,
    upload: Any,
    replace: bool = False,
) -> dict[str, Any]:
    row = await session.get(PackageImportFile, file_id)
    batch = await session.get(ImportBatch, batch_id)
    if (
        not row
        or row.batch_id != batch_id
        or not batch
        or batch.source_type != "package_upload"
    ):
        raise _error("file_not_found", "数据包文件不存在", status_code=404)
    if batch.state not in {"uploading", "failed"}:
        raise _error(
            "batch_locked", "数据包已经开始检测，不能再替换文件", status_code=409
        )
    root = DATA_DIR / "package-imports" / batch_id
    root.mkdir(parents=True, exist_ok=True)
    temporary = root / f"{uuid.uuid4().hex}.uploading"
    digest = hashlib.sha256()
    size = 0
    limit = (
        MAX_PACKAGE_BYTES
        if row.role == "archive"
        else (MAX_VIDEO_BYTES if row.role == "video" else MAX_METADATA_BYTES)
    )
    final: Path | None = None
    committed = False
    try:
        with temporary.open("wb") as output:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise _error(
                        "file_too_large", "上传文件超过允许大小", status_code=413
                    )
                digest.update(chunk)
                output.write(chunk)
        actual_sha = digest.hexdigest()
        if size != row.declared_size:
            raise _error(
                "size_mismatch", "上传大小与创建批次时声明的大小不一致", status_code=409
            )
        existing_path = Path(row.stored_path) if row.stored_path else None
        if row.sha256 == actual_sha and existing_path and existing_path.is_file():
            temporary.unlink(missing_ok=True)
            return {
                "batch_id": batch_id,
                "file_id": row.id,
                "status": "uploaded",
                "sha256": actual_sha,
                "idempotent": True,
            }
        if row.stored_path and not replace:
            raise _error(
                "file_conflict",
                "该条目已经上传了不同文件；如需替换请显式设置 replace=true",
                status_code=409,
            )
        suffix = Path(row.relative_path).suffix.lower()
        final = root / f"{uuid.uuid4().hex}{suffix}"
        old: Path | None = None
        async with PACKAGE_STATE_LOCK:
            # End the read snapshot opened before the potentially long upload and
            # re-check the durable state immediately before promotion.
            await session.rollback()
            row = await session.get(PackageImportFile, file_id)
            batch = await session.get(ImportBatch, batch_id)
            if (
                not row
                or row.batch_id != batch_id
                or not batch
                or batch.state not in {"uploading", "failed"}
            ):
                raise _error(
                    "batch_locked",
                    "数据包已经开始检测，不能再替换文件",
                    status_code=409,
                )
            if row.stored_path and not replace and row.sha256 != actual_sha:
                raise _error(
                    "file_conflict",
                    "该条目已经上传了不同文件；如需替换请显式设置 replace=true",
                    status_code=409,
                )
            old = Path(row.stored_path) if row.stored_path else None
            temporary.replace(final)
            row.stored_path = str(final)
            row.size_bytes = size
            row.sha256 = actual_sha
            row.status = "uploaded"
            row.error_code = None
            row.error_message = None
            job = await session.get(Job, batch.job_id)
            if job:
                uploaded = await session.scalar(
                    select(PackageImportFile.id)
                    .where(
                        PackageImportFile.batch_id == batch_id,
                        PackageImportFile.status == "uploaded",
                    )
                    .limit(1)
                )
                progress = dict(job.progress or {})
                # The exact count is refreshed in the view; this value is just a durable hint.
                progress["last_uploaded_file_id"] = row.id
                progress["has_uploaded_files"] = bool(uploaded)
                job.progress = progress
            await session.commit()
            committed = True
        if old and old != final:
            try:
                old.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("旧数据包上传文件将在稍后清理 {}: {}", old.name, exc)
        return {
            "batch_id": batch_id,
            "file_id": row.id,
            "status": row.status,
            "sha256": actual_sha,
            "idempotent": False,
        }
    except Exception:
        await session.rollback()
        temporary.unlink(missing_ok=True)
        if final and not committed:
            final.unlink(missing_ok=True)
        raise


async def queue_package_analysis(
    session: AsyncSession, batch_id: str
) -> dict[str, Any]:
    async with PACKAGE_STATE_LOCK:
        return await _queue_package_analysis(session, batch_id)


async def _queue_package_analysis(
    session: AsyncSession, batch_id: str
) -> dict[str, Any]:
    batch = await session.get(ImportBatch, batch_id)
    if not batch or batch.source_type != "package_upload":
        raise _error("batch_not_found", "数据包批次不存在", status_code=404)
    if batch.state in {"queued", "running", "succeeded", "partial"}:
        return await package_batch_view(session, batch_id)
    rows = (
        (
            await session.execute(
                select(PackageImportFile).where(PackageImportFile.batch_id == batch_id)
            )
        )
        .scalars()
        .all()
    )
    missing = [
        row.relative_path
        for row in rows
        if row.status not in {"uploaded", "analyzed"}
        or not row.stored_path
        or not Path(row.stored_path).is_file()
    ]
    if missing:
        raise _error(
            "upload_incomplete",
            f"仍有 {len(missing)} 个文件未上传完成",
            status_code=409,
        )
    batch.state = "queued"
    batch.error_code = None
    batch.error_message = None
    job = await session.get(Job, batch.job_id)
    if job:
        job.state = "queued"
        job.message = "等待检测外部视频数据包"
        scope = _scope(job)
        scope["analysis_state"] = "queued"
        job.scope = scope
    await session.commit()
    await coordinator.enqueue(batch_id)
    return await package_batch_view(session, batch_id)


async def package_batch_view(session: AsyncSession, batch_id: str) -> dict[str, Any]:
    batch = await session.get(ImportBatch, batch_id)
    if not batch or batch.source_type != "package_upload":
        raise LookupError("数据包批次不存在")
    job = await session.get(Job, batch.job_id)
    rows = (
        (
            await session.execute(
                select(PackageImportFile)
                .where(PackageImportFile.batch_id == batch_id)
                .order_by(PackageImportFile.created_at, PackageImportFile.id)
            )
        )
        .scalars()
        .all()
    )
    view = await batch_view(session, batch_id)
    uploaded_count = sum(row.status in {"uploaded", "analyzed"} for row in rows)
    view["package"] = {
        "upload_mode": _scope(job).get("upload_mode") if job else None,
        "analysis_state": (
            _scope(job).get("analysis_state", batch.state) if job else batch.state
        ),
        "uploaded_count": uploaded_count,
        "file_count": len(rows),
        "target_collection_id": (
            _scope(job).get("target_collection_id") if job else None
        ),
    }
    view["package_files"] = [
        {
            "id": row.id,
            "client_file_id": row.client_file_id,
            "relative_path": row.relative_path,
            "role": row.role,
            "status": row.status,
            "declared_size": row.declared_size,
            "size_bytes": row.size_bytes,
            "sha256": row.sha256,
            "error_code": row.error_code,
            "error_message": row.error_message,
            "upload_url": (
                f"/api/package-import-batches/{batch_id}/files/{row.id}"
                if row.status == "pending"
                else None
            ),
        }
        for row in rows
    ]
    return view


def _record(value: dict[str, Any]) -> dict[str, Any]:
    aliases = {
        "aweme_id": "platform_work_id",
        "desc": "description",
        "nickname": "author_name",
        "uid": "author_id",
        "create_time": "published_at",
        "file": "video_file",
        "filename": "video_file",
    }
    result: dict[str, Any] = {}
    for key, item in value.items():
        normalized = aliases.get(str(key).strip().lower(), str(key).strip().lower())
        result[normalized] = item
    if not result.get("title"):
        result["title"] = result.get("description") or ""
    platform_id = str(result.get("platform_work_id") or "").strip()
    result["platform_work_id"] = (
        platform_id if re.fullmatch(r"\d{15,22}", platform_id) else None
    )
    published = result.get("published_at")
    if isinstance(published, (int, float)) or (
        isinstance(published, str) and published.isdigit()
    ):
        try:
            from datetime import datetime, timezone

            result["published_at"] = datetime.fromtimestamp(
                float(published), tz=timezone.utc
            ).isoformat()
        except (OverflowError, OSError, ValueError):
            result["published_at"] = None
    return result


def _read_json(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(value, dict) and isinstance(value.get("items"), list):
        value = value["items"]
    if isinstance(value, dict):
        value = [value]
    return (
        [_record(item) for item in value if isinstance(item, dict)]
        if isinstance(value, list)
        else []
    )


def _read_csv(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        return [_record(dict(row)) for row in csv.DictReader(source)]


def _read_f2_database(path: Path) -> list[dict[str, Any]]:
    connection = sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro&immutable=1", uri=True
    )
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='video_info'"
        ).fetchone()
        if not table:
            return []
        available = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(video_info)")
        }
        allowed = [
            name
            for name in (
                "aweme_id",
                "desc",
                "nickname",
                "uid",
                "sec_user_id",
                "create_time",
                "duration",
            )
            if name in available
        ]
        if "aweme_id" not in allowed:
            return []
        query = (
            "SELECT " + ",".join(f'"{name}"' for name in allowed) + " FROM video_info"
        )
        return [
            _record(dict(zip(allowed, row)))
            for row in connection.execute(query).fetchall()
        ]
    finally:
        connection.close()


def _match_key(value: object) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").casefold())


def _zip_entries(archive: Path, destination: Path) -> list[tuple[str, Path]]:
    extracted: list[tuple[str, Path]] = []
    with zipfile.ZipFile(archive) as source:
        infos = source.infolist()
        if len(infos) > MAX_PACKAGE_ENTRIES:
            raise _error(
                "too_many_archive_entries", "ZIP 条目超过 1000 个", status_code=413
            )
        total = 0
        seen: set[str] = set()
        for info in infos:
            if info.is_dir():
                continue
            relative = _safe_relative_path(info.filename)
            hashed = _path_hash(relative)
            if hashed in seen:
                raise _error("archive_path_collision", "ZIP 内存在重复路径")
            seen.add(hashed)
            suffix = Path(relative).suffix.lower()
            if suffix == ".zip":
                raise _error("nested_archive", "不支持嵌套 ZIP")
            if suffix not in VIDEO_EXTENSIONS | METADATA_EXTENSIONS:
                continue
            if info.flag_bits & 0x1:
                raise _error("encrypted_archive", "不支持加密 ZIP")
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise _error("archive_symlink", "ZIP 不能包含符号链接")
            total += int(info.file_size)
            if total > MAX_PACKAGE_BYTES:
                raise _error(
                    "package_too_large", "ZIP 解压后超过 20 GB", status_code=413
                )
            if info.file_size > MAX_VIDEO_BYTES and suffix in VIDEO_EXTENSIONS:
                raise _error(
                    "file_too_large", f"视频超过 1 GB：{relative}", status_code=413
                )
            if info.file_size > MAX_METADATA_BYTES and suffix in METADATA_EXTENSIONS:
                raise _error(
                    "metadata_too_large", f"元数据文件过大：{relative}", status_code=413
                )
            if (
                info.file_size > 10 * 1024 * 1024
                and info.compress_size > 0
                and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO
            ):
                raise _error("zip_bomb", "ZIP 压缩比异常，已拒绝解压", status_code=413)
            target = destination / f"{uuid.uuid4().hex}{suffix}"
            with source.open(info) as input_file, target.open("wb") as output:
                copied = 0
                while True:
                    chunk = input_file.read(1024 * 1024)
                    if not chunk:
                        break
                    copied += len(chunk)
                    if copied > info.file_size or total > MAX_PACKAGE_BYTES:
                        raise _error("zip_bomb", "ZIP 解压大小异常", status_code=413)
                    output.write(chunk)
            extracted.append((relative, target))
    return extracted


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def _analyze(batch_id: str) -> None:
    async with PACKAGE_LOCK:
        async with async_session_factory() as session:
            batch = await session.get(ImportBatch, batch_id)
            if (
                not batch
                or batch.source_type != "package_upload"
                or batch.state in {"succeeded", "partial"}
            ):
                return
            job = await session.get(Job, batch.job_id)
            rows = (
                (
                    await session.execute(
                        select(PackageImportFile).where(
                            PackageImportFile.batch_id == batch_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            batch.state = "running"
            if job:
                job.state = "running"
                job.started_at = job.started_at or utcnow()
                job.message = "正在识别视频与元数据"
                scope = _scope(job)
                scope["analysis_state"] = "running"
                job.scope = scope
            await session.commit()

            root = DATA_DIR / "package-imports" / batch_id
            expanded = root / "expanded"
            expanded.mkdir(parents=True, exist_ok=True)
            temporary_paths: list[Path] = []
            try:
                entries: list[tuple[str, Path]] = []
                if rows and rows[0].role == "archive":
                    entries = await asyncio.to_thread(
                        _zip_entries, Path(rows[0].stored_path or ""), expanded
                    )
                    temporary_paths.extend(path for _, path in entries)
                else:
                    entries = [
                        (row.relative_path, Path(row.stored_path or "")) for row in rows
                    ]
                videos = [
                    (name, path)
                    for name, path in entries
                    if Path(name).suffix.lower() in VIDEO_EXTENSIONS
                ]
                if not videos:
                    raise _error("missing_video", "数据包中没有可用视频")
                if len(videos) > MAX_PACKAGE_VIDEOS:
                    raise _error("too_many_videos", "每批最多检测 100 个视频")

                explicit: dict[str, dict[str, Any]] = {}
                sidecars: dict[str, list[dict[str, Any]]] = {}
                f2_records: list[dict[str, Any]] = []
                for name, path in entries:
                    suffix = Path(name).suffix.lower()
                    records: list[dict[str, Any]] = []
                    try:
                        if suffix == ".json":
                            records = await asyncio.to_thread(_read_json, path)
                        elif suffix == ".csv":
                            records = await asyncio.to_thread(_read_csv, path)
                        elif suffix in {".db", ".sqlite", ".sqlite3"}:
                            records = await asyncio.to_thread(_read_f2_database, path)
                            f2_records.extend(records)
                    except (
                        OSError,
                        ValueError,
                        json.JSONDecodeError,
                        sqlite3.DatabaseError,
                        csv.Error,
                    ):
                        continue
                    for record in records:
                        video_file = record.get("video_file")
                        if video_file:
                            try:
                                explicit[_safe_relative_path(video_file).casefold()] = (
                                    record
                                )
                            except IntegrationImportError:
                                continue
                        elif suffix in {".json", ".csv"}:
                            sidecars.setdefault(_match_key(Path(name).stem), []).append(
                                record
                            )

                f2_by_id = {
                    str(row.get("platform_work_id")): row
                    for row in f2_records
                    if row.get("platform_work_id")
                }
                f2_by_title: dict[str, list[dict[str, Any]]] = {}
                for row in f2_records:
                    key = _match_key(row.get("description") or row.get("title"))
                    if key:
                        f2_by_title.setdefault(key, []).append(row)

                previous_items = (
                    (
                        await session.execute(
                            select(ImportItem).where(ImportItem.batch_id == batch_id)
                        )
                    )
                    .scalars()
                    .all()
                )
                previous_by_ordinal = {item.ordinal: item for item in previous_items}
                created = sum(item.status != "failed" for item in previous_items)
                failed = sum(item.status == "failed" for item in previous_items)
                seen_identity: set[tuple[str, str]] = {
                    (item.platform, item.platform_work_id)
                    for item in previous_items
                    if item.platform_work_id
                }
                target_collection_id = (
                    _scope(job).get("target_collection_id") if job else None
                )
                attested_at = _scope(job).get("attested_at") if job else None
                for ordinal, (relative, path) in enumerate(videos, 1):
                    if ordinal in previous_by_ordinal:
                        continue
                    asset_dir: Path | None = None
                    destination: Path | None = None
                    try:
                        with path.open("rb") as source:
                            header = source.read(32)
                        kind, extension, mime = _kind_and_extension(header)
                        if kind != "video":
                            raise LocalAssetError("unsupported_media", "文件不是视频")
                        duration = await asyncio.to_thread(_verify_video, path)
                        sha = await asyncio.to_thread(_sha256_file, path)
                        record = explicit.get(relative.casefold()) or explicit.get(
                            Path(relative).name.casefold()
                        )
                        match_source = "manifest" if record else None
                        stem = Path(relative).stem
                        if not record:
                            candidates = sidecars.get(_match_key(stem), [])
                            if len(candidates) == 1:
                                record = candidates[0]
                                match_source = "sidecar"
                        numeric = DOUYIN_ID_RE.search(stem)
                        if not record and numeric and numeric.group(1) in f2_by_id:
                            record = f2_by_id[numeric.group(1)]
                            match_source = "f2_database"
                        if not record:
                            stem_key = _match_key(stem)
                            title_matches = [
                                row
                                for key, values in f2_by_title.items()
                                if key
                                and (
                                    key == stem_key
                                    or (len(key) >= 8 and stem_key.endswith(key))
                                )
                                for row in values
                            ]
                            if len(title_matches) == 1:
                                record = title_matches[0]
                                match_source = "f2_database_title"
                        if not record and numeric:
                            record = {
                                "platform_work_id": numeric.group(1),
                                "title": stem,
                            }
                            match_source = "filename_id"
                        record = record or {}
                        expected_sha = str(record.get("expected_sha256") or "").lower()
                        if expected_sha:
                            if not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
                                raise _error(
                                    "invalid_expected_sha256",
                                    f"元数据中的 SHA-256 无效：{relative}",
                                )
                            if expected_sha != sha:
                                raise _error(
                                    "sha256_mismatch",
                                    f"视频与元数据声明的 SHA-256 不一致：{relative}",
                                )
                        platform_id = str(record.get("platform_work_id") or "")
                        platform = "douyin" if platform_id.isdigit() else "local"
                        if platform == "local":
                            platform_id = f"local-{sha}"
                            match_source = "local_fallback"
                        identity = (platform, platform_id)
                        existing = await session.scalar(
                            select(Work).where(
                                Work.platform == platform,
                                Work.platform_work_id == platform_id,
                            )
                        )
                        if not existing and platform == "local":
                            old_asset = await session.scalar(
                                select(WorkSourceAsset)
                                .where(
                                    WorkSourceAsset.sha256 == sha,
                                    WorkSourceAsset.work_id.is_not(None),
                                )
                                .limit(1)
                            )
                            if old_asset:
                                existing = await session.get(Work, old_asset.work_id)
                        duplicate_input = identity in seen_identity
                        seen_identity.add(identity)
                        title = (
                            str(
                                record.get("title") or record.get("description") or stem
                            ).strip()[:500]
                            or stem[:500]
                        )
                        description = (
                            str(record.get("description") or "")[:20_000] or None
                        )
                        source_url = str(record.get("source_url") or "")[:2000]
                        if platform == "douyin":
                            parsed_source = urlsplit(source_url)
                            hostname = (parsed_source.hostname or "").lower()
                            if parsed_source.scheme != "https" or not (
                                hostname == "douyin.com"
                                or hostname.endswith(".douyin.com")
                            ):
                                source_url = (
                                    f"https://www.douyin.com/video/{platform_id}"
                                )
                        extra_metadata = (
                            dict(record.get("extra_metadata") or {})
                            if isinstance(record.get("extra_metadata"), dict)
                            else {}
                        )
                        if (
                            len(
                                json.dumps(
                                    extra_metadata,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                ).encode("utf-8")
                            )
                            > 64 * 1024
                        ):
                            raise _error(
                                "metadata_too_large",
                                f"单条扩展元数据超过 64 KB：{relative}",
                            )
                        metadata = {
                            "external_import": {
                                "published_at": record.get("published_at"),
                                "extra_metadata": extra_metadata,
                            },
                            "import_provenance": {
                                "source_type": "package_upload",
                                "rights_attested": True,
                                "attested_at": attested_at,
                                "original_relative_path": relative,
                                "sha256": sha,
                                "match_source": match_source,
                            },
                        }
                        if not existing and not duplicate_input:
                            # Copy before opening a write transaction. Large videos
                            # must not hold SQLite's writer lock while shutil runs.
                            asset_dir = (
                                DATA_DIR
                                / "source-assets"
                                / f"package-{batch_id}-{ordinal}-{uuid.uuid4().hex}"
                            )
                            await asyncio.to_thread(
                                asset_dir.mkdir, parents=True, exist_ok=False
                            )
                            destination = asset_dir / f"source{extension}"
                            await asyncio.to_thread(shutil.copy2, path, destination)

                        item = ImportItem(
                            batch_id=batch_id,
                            ordinal=ordinal,
                            platform=platform,
                            client_item_id=f"package-{ordinal}",
                            target_collection_id=target_collection_id,
                            input_url=(
                                source_url
                                if platform == "douyin"
                                else f"package://{batch_id}/{ordinal}"
                            ),
                            normalized_url=(
                                source_url
                                if platform == "douyin"
                                else f"package://{batch_id}/{ordinal}"
                            ),
                            canonical_url=source_url or None,
                            platform_work_id=platform_id,
                            kind="video",
                            title=title,
                            description=description,
                            author_id=str(record.get("author_id") or "")[:120] or None,
                            author_name=str(record.get("author_name") or "")[:200]
                            or None,
                            duration_seconds=float(duration),
                            status=(
                                "duplicate" if existing or duplicate_input else "ready"
                            ),
                            existing_work_id=existing.id if existing else None,
                            error_code=(
                                "already_imported"
                                if existing
                                else ("duplicate_input" if duplicate_input else None)
                            ),
                            error_message=(
                                "该视频已存在" if existing or duplicate_input else None
                            ),
                            raw_metadata=metadata,
                        )
                        session.add(item)
                        await session.flush()
                        if destination is not None:
                            session.add(
                                WorkSourceAsset(
                                    import_item_id=item.id,
                                    kind="video",
                                    path=str(destination),
                                    mime_type=mime,
                                    size_bytes=destination.stat().st_size,
                                    sha256=sha,
                                    position=0,
                                )
                            )
                        await session.commit()
                        if destination is None and path in temporary_paths:
                            path.unlink(missing_ok=True)
                        created += 1
                    except Exception as exc:
                        await session.rollback()
                        if asset_dir and asset_dir.exists():
                            await asyncio.to_thread(
                                shutil.rmtree, asset_dir, ignore_errors=True
                            )
                        failed += 1
                        session.add(
                            ImportItem(
                                batch_id=batch_id,
                                ordinal=ordinal,
                                platform="local",
                                client_item_id=f"package-{ordinal}",
                                input_url=f"package://{batch_id}/{ordinal}",
                                normalized_url=f"package://{batch_id}/{ordinal}",
                                kind="video",
                                title=Path(relative).stem[:500],
                                status="failed",
                                error_code=getattr(exc, "code", "invalid_video"),
                                error_message=str(exc)[:2000],
                            )
                        )
                        await session.commit()
                batch = await session.get(ImportBatch, batch_id)
                if not batch:
                    return
                job = await session.get(Job, batch.job_id)
                rows = (
                    (
                        await session.execute(
                            select(PackageImportFile).where(
                                PackageImportFile.batch_id == batch_id
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                batch.total_items = len(videos)
                batch.state = "partial" if failed else "succeeded"
                batch.completed_at = utcnow()
                for row in rows:
                    row.status = "analyzed"
                if job:
                    job.state = batch.state
                    job.total_items = len(videos)
                    job.processed_items = created
                    job.failed_items = failed
                    job.completed_at = utcnow()
                    job.progress = {
                        "files_total": len(rows),
                        "files_uploaded": len(rows),
                        "videos_found": len(videos),
                        "ready": created,
                        "failed": failed,
                        "completed": len(videos),
                    }
                    job.message = (
                        f"数据包检测完成：{created} 个可确认，{failed} 个失败"
                    )
                    scope = _scope(job)
                    scope["analysis_state"] = (
                        "completed_with_issues" if failed else "completed"
                    )
                    job.scope = scope
                await session.commit()
                await refresh_manifest_progress(session, batch_id)
                if not failed:
                    await asyncio.to_thread(shutil.rmtree, root, ignore_errors=True)
                    for row in rows:
                        row.stored_path = None
                    await session.commit()
            except Exception as exc:
                await session.rollback()
                batch = await session.get(ImportBatch, batch_id)
                if not batch:
                    return
                job = await session.get(Job, batch.job_id)
                batch.state = "failed"
                batch.error_code = getattr(exc, "code", "package_analysis_failed")
                batch.error_message = str(exc)[:2000]
                batch.completed_at = utcnow()
                if job:
                    job.state = "failed"
                    job.failed_items = max(1, job.failed_items)
                    job.message = str(exc)[:2000]
                    job.completed_at = utcnow()
                    scope = _scope(job)
                    scope["analysis_state"] = "failed"
                    job.scope = scope
                await session.commit()


async def cleanup_expired_packages() -> int:
    cutoff = utcnow() - timedelta(days=PACKAGE_RETENTION_DAYS)
    removed = 0
    async with async_session_factory() as session:
        completed_batches = (
            (
                await session.execute(
                    select(ImportBatch).where(
                        ImportBatch.source_type == "package_upload",
                        ImportBatch.state == "succeeded",
                    )
                )
            )
            .scalars()
            .all()
        )
        for batch in completed_batches:
            root = DATA_DIR / "package-imports" / batch.id
            if root.exists():
                await asyncio.to_thread(shutil.rmtree, root, ignore_errors=True)
                removed += 1
            rows = (
                (
                    await session.execute(
                        select(PackageImportFile).where(
                            PackageImportFile.batch_id == batch.id
                        )
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                row.stored_path = None

        batches = (
            (
                await session.execute(
                    select(ImportBatch).where(
                        ImportBatch.source_type == "package_upload",
                        ImportBatch.updated_at < cutoff,
                        ImportBatch.state.in_(
                            {"uploading", "failed", "partial", "cancelled"}
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        for batch in batches:
            root = DATA_DIR / "package-imports" / batch.id
            if root.exists():
                await asyncio.to_thread(shutil.rmtree, root, ignore_errors=True)
                removed += 1
            rows = (
                (
                    await session.execute(
                        select(PackageImportFile).where(
                            PackageImportFile.batch_id == batch.id
                        )
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                row.stored_path = None
                if row.status in {"pending", "uploaded"}:
                    row.status = "expired"
                    row.error_code = "upload_expired"
                    row.error_message = "未完成的数据包已超过 7 天保留期"
        await session.commit()
    return removed


class PackageImportCoordinator:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[str | None] = asyncio.Queue()
        self.task: asyncio.Task | None = None
        self.queued: set[str] = set()
        self.last_error: str | None = None

    def health_snapshot(self) -> dict[str, object]:
        alive = bool(self.task and not self.task.done())
        if self.task and self.task.done() and not self.task.cancelled():
            error = self.task.exception()
            if error is not None:
                self.last_error = safe_error_message(error)
        return {
            "name": "package_import",
            "alive": alive,
            "workers_alive": 1 if alive else 0,
            "workers_expected": 1,
            "last_error": self.last_error,
        }

    async def start(self) -> None:
        if self.task and not self.task.done():
            return
        self.last_error = None
        await cleanup_expired_packages()
        async with async_session_factory() as session:
            batches = (
                (
                    await session.execute(
                        select(ImportBatch).where(
                            ImportBatch.source_type == "package_upload",
                            ImportBatch.state.in_({"queued", "running"}),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for batch in batches:
                if batch.state == "running":
                    batch.state = "queued"
                    job = await session.get(Job, batch.job_id)
                    if job:
                        job.state = "queued"
                await self.enqueue(batch.id)
            await session.commit()
        self.task = asyncio.create_task(self._run(), name="package-import-coordinator")

    async def stop(self) -> None:
        if not self.task:
            return
        task = self.task
        if not task.done():
            await self.queue.put(None)
        try:
            await asyncio.wait_for(
                asyncio.shield(task), timeout=PACKAGE_SHUTDOWN_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            logger.warning("数据包检测任务未在关闭期限内退出，正在取消")
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        finally:
            # A sentinel or stale in-memory ids may remain after either a normal
            # stop or an unexpected worker exit. Durable work is recovered from
            # the database by the next start().
            self.queue = asyncio.Queue()
            self.queued.clear()
            self.task = None

    async def enqueue(self, batch_id: str) -> None:
        if batch_id in self.queued:
            return
        self.queued.add(batch_id)
        await self.queue.put(batch_id)

    async def _run(self) -> None:
        while True:
            batch_id = await self.queue.get()
            if batch_id is None:
                self.queue.task_done()
                return
            try:
                await _analyze(batch_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = safe_error_message(exc)
                logger.exception("数据包检测 worker 处理批次 {} 时异常", batch_id)
            finally:
                self.queued.discard(batch_id)
                self.queue.task_done()


coordinator = PackageImportCoordinator()
