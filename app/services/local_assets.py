"""Safe local-file supplements for F2 results that do not expose media."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any
from weakref import WeakValueDictionary

from PIL import Image
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import DATA_DIR, settings
from app.models import ImportBatch, ImportItem, Work, WorkSourceAsset, utcnow
from app.services.budget import estimate_video_ingest_units
from app.services.keyframes import parse_duration_output
from app.services.providers import SUMMARY_MAX_OUTPUT_TOKENS
from app.services.runtime_settings import get_runtime_settings


IMAGE_LIMIT = 30 * 1024 * 1024
IMAGE_TOTAL_LIMIT = 180 * 1024 * 1024
VIDEO_LIMIT = 1024 * 1024 * 1024
ALLOWED_ITEM_ERROR_CODES = {
    "media_missing",
    "media_expired",
    "f2_cookie_required",
    "f2_response_invalid",
    "f2_contract_changed",
    "page_contract_changed",
    "work_unavailable",
    "unsupported_content_type",
}
_IMPORT_VIDEO_LOCKS: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()
_WORK_SUPPLEMENT_LOCKS: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()


class LocalAssetError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _kind_and_extension(header: bytes) -> tuple[str, str, str]:
    if header.startswith(b"\xff\xd8\xff"):
        return "image", ".jpg", "image/jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", ".png", "image/png"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "image", ".webp", "image/webp"
    if len(header) >= 12 and header[4:8] == b"ftyp":
        if header[8:12] == b"qt  ":
            return "video", ".mov", "video/quicktime"
        return "video", ".mp4", "video/mp4"
    if header.startswith(b"\x1aE\xdf\xa3"):
        return "video", ".mkv", "video/x-matroska"
    raise LocalAssetError("unsupported_media", "文件内容不是受支持的视频或图片")


def _verify_image(path: Path) -> None:
    try:
        with Image.open(path) as image:
            image.verify()
    except Exception as exc:
        raise LocalAssetError(
            "unsupported_media", "图片内容损坏或格式不受支持"
        ) from exc


def _verify_video(path: Path) -> float:
    ffprobe = shutil.which("ffprobe")
    ffmpeg = shutil.which("ffmpeg")
    if not ffprobe or not ffmpeg:
        raise LocalAssetError("ffmpeg_unavailable", "需要安装 ffmpeg 才能验证本地视频")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_type,duration:format=duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=20,
        creationflags=creationflags,
        check=False,
    )
    if result.returncode != 0:
        raise LocalAssetError("unsupported_media", "视频内容损坏或没有可用视频轨道")
    try:
        payload = json.loads(result.stdout)
    except ValueError as exc:
        raise LocalAssetError("unsupported_media", "无法读取视频结构") from exc
    if not any(
        isinstance(stream, dict) and stream.get("codec_type") == "video"
        for stream in payload.get("streams") or []
    ):
        raise LocalAssetError("unsupported_media", "视频内容损坏或没有可用视频轨道")
    duration = parse_duration_output(result.stdout)
    if not duration:
        raise LocalAssetError("unsupported_media", "无法读取视频时长")
    if duration > settings.max_work_duration_seconds:
        raise LocalAssetError("work_too_long", "视频时长超过单作品安全上限")

    # ffprobe can read the header of a partially downloaded MP4 and still
    # report a plausible duration. Scan every audio/video packet with stream
    # copy so truncated or corrupt uploads are rejected before confirmation,
    # without paying the cost of a full decode.
    integrity = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-xerror",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c",
            "copy",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        timeout=120,
        creationflags=creationflags,
        check=False,
    )
    if integrity.returncode != 0:
        raise LocalAssetError("unsupported_media", "视频文件未下载完整或内容已损坏")
    return duration


def _safe_display_filename(value: object) -> str:
    name = Path(str(value or "video")).name
    name = re.sub(r"[<>:\"/\\|?*\x00-\x1f]+", "_", name)
    return re.sub(r"\s+", " ", name).strip(" ._")[:260] or "video"


def _safe_existing_source_path(value: object) -> Path | None:
    root = (DATA_DIR / "source-assets").resolve()
    try:
        path = Path(str(value)).resolve()
    except OSError:
        return None
    return path if root in path.parents else None


async def _save_upload(upload: Any, directory: Path) -> dict:
    identifier = uuid.uuid4().hex
    temporary = directory / f"{identifier}.uploading"
    digest = hashlib.sha256()
    size = 0
    header = b""
    try:
        with temporary.open("wb") as output:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                if len(header) < 32:
                    header += chunk[: 32 - len(header)]
                size += len(chunk)
                if size > VIDEO_LIMIT:
                    raise LocalAssetError("file_too_large", "单个文件超过 1 GB 上限")
                digest.update(chunk)
                output.write(chunk)
        kind, extension, mime_type = _kind_and_extension(header)
        if kind == "image" and size > IMAGE_LIMIT:
            raise LocalAssetError("file_too_large", "单张图片超过 30 MB 上限")
        final = directory / f"{identifier}{extension}"
        temporary.replace(final)
        duration_seconds = 0.0
        if kind == "image":
            await asyncio.to_thread(_verify_image, final)
        else:
            duration_seconds = await asyncio.to_thread(_verify_video, final)
        return {
            "kind": kind,
            "path": final,
            "mime_type": mime_type,
            "size_bytes": size,
            "sha256": digest.hexdigest(),
            "duration_seconds": duration_seconds,
            "original_filename": _safe_display_filename(
                getattr(upload, "filename", "video")
            ),
        }
    except Exception:
        temporary.unlink(missing_ok=True)
        candidate = directory / f"{identifier}.jpg"
        for extension in (".jpg", ".png", ".webp", ".mp4", ".mov", ".mkv", ".webm"):
            candidate.with_suffix(extension).unlink(missing_ok=True)
        raise
    finally:
        await upload.close()


async def store_local_assets(
    session: AsyncSession, item_id: int, uploads: list[Any]
) -> ImportItem:
    item = await session.get(ImportItem, item_id)
    if not item:
        raise LookupError("导入条目不存在")
    if item.status == "confirmed":
        raise LocalAssetError("already_imported", "该条目已经确认入库")
    if item.status not in {"needs_local_file", "failed", "ready"}:
        raise LocalAssetError("local_file_required", "当前条目不接受本地补件")
    if item.status == "failed" and item.error_code not in ALLOWED_ITEM_ERROR_CODES:
        raise LocalAssetError("local_file_required", "该失败原因不能通过本地文件修复")
    if not uploads:
        raise LocalAssetError("invalid_url", "请选择至少一个本地文件")
    if len(uploads) > 12:
        raise LocalAssetError("file_too_large", "图文补件最多上传 12 张图片")

    directory = DATA_DIR / "source-assets" / f"item-{item.id}"
    directory.mkdir(parents=True, exist_ok=True)
    saved: list[dict] = []
    try:
        for upload in uploads:
            saved.append(await _save_upload(upload, directory))
        kinds = {row["kind"] for row in saved}
        if kinds == {"video"} and len(saved) != 1:
            raise LocalAssetError("unsupported_media", "视频补件只能上传一个文件")
        if len(kinds) != 1:
            raise LocalAssetError("unsupported_media", "不能混合上传视频和图片")
        if (
            kinds == {"image"}
            and sum(row["size_bytes"] for row in saved) > IMAGE_TOTAL_LIMIT
        ):
            raise LocalAssetError("file_too_large", "图片总大小超过 180 MB 上限")
    except Exception:
        for row in saved:
            Path(row["path"]).unlink(missing_ok=True)
        raise

    old_assets = (
        (
            await session.execute(
                select(WorkSourceAsset).where(WorkSourceAsset.import_item_id == item.id)
            )
        )
        .scalars()
        .all()
    )
    old_paths = [
        path
        for old in old_assets
        if (path := _safe_existing_source_path(old.path)) is not None
    ]
    try:
        await session.execute(
            delete(WorkSourceAsset).where(WorkSourceAsset.import_item_id == item.id)
        )
        for position, row in enumerate(saved):
            session.add(
                WorkSourceAsset(
                    import_item_id=item.id,
                    work_id=item.existing_work_id,
                    kind=row["kind"],
                    path=str(row["path"]),
                    mime_type=row["mime_type"],
                    size_bytes=row["size_bytes"],
                    sha256=row["sha256"],
                    position=position,
                )
            )
        item.kind = saved[0]["kind"]
        if item.existing_work_id:
            work = await session.get(Work, item.existing_work_id)
            if not work:
                raise LookupError("关联作品不存在")
            work.kind = item.kind
            work.library_state = "issues"
            work.processing_state = "failed"
            work.process_error = None
            work.last_error_code = None
            # Keep the item actionable until the explicit retry call succeeds.
            item.status = "needs_local_file"
        else:
            item.status = "ready"
        item.error_code = None
        item.error_message = None
        if item.kind == "video":
            item.duration_seconds = float(saved[0].get("duration_seconds") or 0)
        await session.commit()
    except Exception:
        await session.rollback()
        for row in saved:
            Path(row["path"]).unlink(missing_ok=True)
        raise

    # The old rows are no longer reachable only after the replacement is durable.
    for old_path in old_paths:
        try:
            old_path.unlink(missing_ok=True)
        except OSError:
            # A transient Windows file lock must not turn a committed replacement
            # into an API failure. The orphan can be removed by later maintenance.
            pass
    return item


async def _close_uploads(uploads: list[Any]) -> None:
    for upload in uploads:
        close = getattr(upload, "close", None)
        if close is not None:
            await close()


async def store_work_supplement(
    session: AsyncSession,
    work_id: int,
    uploads: list[Any],
    *,
    rights_attested: bool,
) -> tuple[Work, dict]:
    """Atomically replace one work's source video or complete image set."""

    lock = _WORK_SUPPLEMENT_LOCKS.setdefault(work_id, asyncio.Lock())
    async with lock:
        work = await session.get(Work, work_id)
        if not work:
            await _close_uploads(uploads)
            raise LookupError("作品不存在")
        if not rights_attested:
            await _close_uploads(uploads)
            raise LocalAssetError(
                "rights_attestation_required", "请先确认有权处理这些文件"
            )
        if work.supplement_state == "none":
            await _close_uploads(uploads)
            raise LocalAssetError("local_file_required", "该作品当前不需要本地补件")
        if not uploads:
            raise LocalAssetError("local_file_required", "请选择需要补件的本地文件")

        expected_kind = "image" if work.kind == "image" else "video"
        if work.kind not in {"image", "video"}:
            await _close_uploads(uploads)
            raise LocalAssetError("unsupported_media", "当前作品类型不支持本地补件")
        if expected_kind == "video" and len(uploads) != 1:
            await _close_uploads(uploads)
            raise LocalAssetError("unsupported_media", "视频补件只能上传一个完整视频")
        if expected_kind == "image" and len(uploads) > 12:
            await _close_uploads(uploads)
            raise LocalAssetError("file_too_large", "图文补件最多上传 12 张图片")

        directory = DATA_DIR / "source-assets" / f"work-{work.id}"
        directory.mkdir(parents=True, exist_ok=True)
        saved: list[dict] = []
        try:
            for upload in uploads:
                saved.append(await _save_upload(upload, directory))
            if any(row["kind"] != expected_kind for row in saved):
                raise LocalAssetError(
                    "unsupported_media",
                    "视频作品请上传视频，图文作品请上传完整图片组",
                )
            if (
                expected_kind == "image"
                and sum(int(row["size_bytes"]) for row in saved) > IMAGE_TOTAL_LIMIT
            ):
                raise LocalAssetError("file_too_large", "图片总大小超过 180 MB 上限")
        except Exception:
            for row in saved:
                Path(row["path"]).unlink(missing_ok=True)
            # _save_upload closes files it starts reading. Close any later files
            # that were not reached after an earlier validation failure.
            await _close_uploads(uploads[len(saved) :])
            raise

        old_assets = list(
            (
                await session.execute(
                    select(WorkSourceAsset)
                    .where(WorkSourceAsset.work_id == work.id)
                    .order_by(WorkSourceAsset.position, WorkSourceAsset.id)
                )
            ).scalars()
        )
        incoming_identity = [(str(row["kind"]), str(row["sha256"])) for row in saved]
        existing_identity = [(row.kind, row.sha256) for row in old_assets]
        idempotent = bool(old_assets and existing_identity == incoming_identity)
        old_paths = [
            path
            for row in old_assets
            if (path := _safe_existing_source_path(row.path)) is not None
        ]

        try:
            if idempotent:
                for row in saved:
                    Path(row["path"]).unlink(missing_ok=True)
            else:
                await session.execute(
                    delete(WorkSourceAsset).where(WorkSourceAsset.work_id == work.id)
                )
                for position, row in enumerate(saved):
                    session.add(
                        WorkSourceAsset(
                            work_id=work.id,
                            kind=expected_kind,
                            path=str(row["path"]),
                            mime_type=str(row["mime_type"]),
                            size_bytes=int(row["size_bytes"]),
                            sha256=str(row["sha256"]),
                            position=position,
                        )
                    )

            attested_at = utcnow().isoformat()
            raw_metadata = (
                dict(work.raw_metadata or {})
                if isinstance(work.raw_metadata, dict)
                else {}
            )
            raw_metadata["supplement_provenance"] = {
                "source_type": "local_supplement",
                "rights_attested": True,
                "rights_attested_at": attested_at,
                "asset_kind": expected_kind,
                "asset_count": len(saved),
                "sha256": [str(row["sha256"]) for row in saved],
                "original_filenames": [str(row["original_filename"]) for row in saved],
            }
            work.raw_metadata = raw_metadata
            track_report = (
                dict(work.track_report or {})
                if isinstance(work.track_report, dict)
                else {}
            )
            upload_report = {
                "uploaded": True,
                "processed": len(saved),
                "sha256": [str(row["sha256"]) for row in saved],
                "rights_attested_at": attested_at,
            }
            if expected_kind == "image":
                upload_report.update({"expected": len(saved), "missing": []})
                track_report["images"] = upload_report
            else:
                upload_report["duration_seconds"] = float(
                    saved[0].get("duration_seconds") or 0
                )
                track_report["video"] = upload_report
                work.duration_seconds = upload_report["duration_seconds"]
            work.track_report = track_report
            work.supplement_state = "uploaded"
            work.evidence_state = "unverified"
            await session.commit()
        except Exception:
            await session.rollback()
            if not idempotent:
                for row in saved:
                    Path(row["path"]).unlink(missing_ok=True)
            raise

        if not idempotent:
            for old_path in old_paths:
                try:
                    old_path.unlink(missing_ok=True)
                except OSError:
                    # The replacement is already durable. A transient Windows
                    # file lock must not make the successful upload look failed.
                    pass
        return work, {
            "asset_count": len(saved),
            "sha256": [str(row["sha256"]) for row in saved],
            "duration_seconds": (
                float(saved[0].get("duration_seconds") or 0)
                if expected_kind == "video"
                else 0.0
            ),
            "idempotent": idempotent,
        }


async def store_import_video(
    session: AsyncSession,
    item_id: int,
    upload: Any,
    *,
    batch_id: str | None = None,
    expected_sha256: str | None = None,
    replace: bool = False,
) -> tuple[ImportItem, dict]:
    lock = _IMPORT_VIDEO_LOCKS.setdefault(item_id, asyncio.Lock())
    async with lock:
        return await _store_import_video(
            session,
            item_id,
            upload,
            batch_id=batch_id,
            expected_sha256=expected_sha256,
            replace=replace,
        )


async def _store_import_video(
    session: AsyncSession,
    item_id: int,
    upload: Any,
    *,
    batch_id: str | None = None,
    expected_sha256: str | None = None,
    replace: bool = False,
) -> tuple[ImportItem, dict]:
    """Store one validated video for local/external imports with safe retries."""

    item = await session.get(ImportItem, item_id)
    if not item or (batch_id is not None and item.batch_id != batch_id):
        raise LookupError("导入条目不存在或不属于该批次")
    batch = await session.get(ImportBatch, item.batch_id)
    if not batch or batch.source_type not in {"local_upload", "external_batch"}:
        raise LocalAssetError("invalid_import_source", "该条目不接受独立视频上传")
    if item.status in {"confirmed", "duplicate"}:
        raise LocalAssetError("already_imported", "该条目已经提交")
    if item.status not in {"needs_local_file", "ready", "failed"}:
        raise LocalAssetError("local_file_required", "当前条目不接受视频上传")

    directory = DATA_DIR / "source-assets" / f"item-{item.id}"
    directory.mkdir(parents=True, exist_ok=True)
    saved = await _save_upload(upload, directory)
    database_write_started = False
    try:
        if saved["kind"] != "video":
            raise LocalAssetError("unsupported_media", "该接口只接受单个视频文件")
        existing_metadata = (
            dict(item.raw_metadata or {})
            if isinstance(item.raw_metadata, dict)
            else {}
        )
        existing_provenance = dict(
            existing_metadata.get("import_provenance") or {}
        )
        declared_size = int(existing_provenance.get("declared_size_bytes") or 0)
        if declared_size and int(saved["size_bytes"]) != declared_size:
            raise LocalAssetError(
                "size_mismatch", "视频大小与创建清单时声明的大小不一致"
            )
        actual_sha = str(saved["sha256"])
        if expected_sha256 and actual_sha.lower() != expected_sha256.lower():
            raise LocalAssetError("sha256_mismatch", "视频 SHA-256 与清单声明不一致")
        runtime = await get_runtime_settings(session)
        budget = estimate_video_ingest_units(
            saved["duration_seconds"],
            runtime,
            summary_max_output_tokens=SUMMARY_MAX_OUTPUT_TOKENS,
        )
        budget_estimate = {
            "media_minutes": round(budget.media_minutes, 4),
            "llm_tokens": budget.llm_tokens,
        }

        old_assets = (
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
        old_paths = [
            path
            for old in old_assets
            if (path := _safe_existing_source_path(old.path)) is not None
        ]
        if old_assets:
            if len(old_assets) == 1 and old_assets[0].sha256 == actual_sha:
                Path(saved["path"]).unlink(missing_ok=True)
                return item, {
                    "sha256": actual_sha,
                    "duration_seconds": item.duration_seconds,
                    "budget_estimate": budget_estimate,
                    "idempotent": True,
                }
            if not replace:
                raise LocalAssetError(
                    "asset_conflict",
                    "该条目已经上传了不同视频；如需替换请显式确认",
                )

        existing_asset = await session.scalar(
            select(WorkSourceAsset)
            .where(
                WorkSourceAsset.sha256 == actual_sha,
                WorkSourceAsset.work_id.is_not(None),
            )
            .limit(1)
        )
        if (
            existing_asset
            and Path(existing_asset.path).is_file()
            and batch.source_type == "local_upload"
        ):
            Path(saved["path"]).unlink(missing_ok=True)
            database_write_started = True
            item.platform = "local"
            item.platform_work_id = f"local-{actual_sha}"
            item.kind = "video"
            item.duration_seconds = float(saved["duration_seconds"])
            item.status = "duplicate"
            item.error_code = "already_imported"
            item.error_message = "相同视频已经在知识库中"
            item.existing_work_id = existing_asset.work_id
            await session.commit()
            return item, {
                "sha256": actual_sha,
                "duration_seconds": float(saved["duration_seconds"]),
                "budget_estimate": budget_estimate,
                "existing_work_id": existing_asset.work_id,
            }

        database_write_started = True
        for old in old_assets:
            await session.delete(old)
        await session.flush()
        session.add(
            WorkSourceAsset(
                import_item_id=item.id,
                work_id=item.existing_work_id,
                kind="video",
                path=str(saved["path"]),
                mime_type=str(saved["mime_type"]),
                size_bytes=int(saved["size_bytes"]),
                sha256=actual_sha,
                position=0,
            )
        )
        item.kind = "video"
        item.duration_seconds = float(saved["duration_seconds"])
        if batch.source_type == "local_upload":
            item.platform = "local"
            item.platform_work_id = f"local-{actual_sha}"
        metadata = existing_metadata
        provenance = dict(metadata.get("import_provenance") or {})
        provenance.update(
            {
                "source_type": batch.source_type,
                "rights_attested": True,
                "original_filename": saved["original_filename"],
                "sha256": actual_sha,
                "budget_estimate": budget_estimate,
            }
        )
        metadata["import_provenance"] = provenance
        item.raw_metadata = metadata
        item.status = "ready"
        item.error_code = None
        item.error_message = None
        await session.commit()
        for old_path in old_paths:
            try:
                old_path.unlink(missing_ok=True)
            except OSError:
                pass
        return item, {
            "sha256": actual_sha,
            "duration_seconds": item.duration_seconds,
            "budget_estimate": budget_estimate,
            "idempotent": False,
        }
    except Exception:
        if database_write_started:
            await session.rollback()
        Path(saved["path"]).unlink(missing_ok=True)
        raise
