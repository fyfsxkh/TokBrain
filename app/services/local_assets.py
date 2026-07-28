"""Safe local-file supplements for F2 results that do not expose media."""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

from PIL import Image
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import DATA_DIR
from app.models import ImportItem, Work, WorkSourceAsset


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
        raise LocalAssetError("unsupported_media", "图片内容损坏或格式不受支持") from exc


def _verify_video(path: Path) -> None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
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
            "stream=codec_type",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=20,
        creationflags=creationflags,
        check=False,
    )
    if result.returncode != 0 or "video" not in result.stdout.lower():
        raise LocalAssetError("unsupported_media", "视频内容损坏或没有可用视频轨道")


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
        if kind == "image":
            await asyncio.to_thread(_verify_image, final)
        else:
            await asyncio.to_thread(_verify_video, final)
        return {
            "kind": kind,
            "path": final,
            "mime_type": mime_type,
            "size_bytes": size,
            "sha256": digest.hexdigest(),
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
        if kinds == {"image"} and sum(row["size_bytes"] for row in saved) > IMAGE_TOTAL_LIMIT:
            raise LocalAssetError("file_too_large", "图片总大小超过 180 MB 上限")
    except Exception:
        for row in saved:
            Path(row["path"]).unlink(missing_ok=True)
        raise

    old_assets = (
        await session.execute(
            select(WorkSourceAsset).where(WorkSourceAsset.import_item_id == item.id)
        )
    ).scalars().all()
    await session.execute(
        delete(WorkSourceAsset).where(WorkSourceAsset.import_item_id == item.id)
    )
    for old in old_assets:
        Path(old.path).unlink(missing_ok=True)
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
    await session.commit()
    return item
