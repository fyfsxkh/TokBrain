"""Durable, local-only custom themes and wallpaper media."""

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

from PIL import Image, ImageSequence
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import DATA_DIR
from app.models import CustomTheme
from app.services.temp_files import unlink_with_retries


THEME_ASSET_ROOT = DATA_DIR / "theme-assets"
THEME_TEMP_ROOT = DATA_DIR / "tmp"
STATIC_LIMIT = 30 * 1024 * 1024
DYNAMIC_LIMIT = 256 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_VIDEO_SECONDS = 300.0
MAX_ANIMATED_IMAGE_SECONDS = 60.0
MAX_VIDEO_PIXELS = 3840 * 2160

BUILTIN_THEME_IDS = frozenset(
    {
        "classic-night",
        "dragonbone-biopunk",
        "aether-sky-city",
        "abyssal-runepunk",
        "paper-organizer",
        "ocean-dawn",
        "sunset-cloudsea",
        "aurora-snowfield",
        "eastern-mist",
        "moss-forest",
        "desert-observatory",
        "sakura-valley",
        "volcanic-forge",
    }
)

PALETTE_KEYS = (
    "page",
    "sidebar",
    "surface",
    "surfaceAlt",
    "border",
    "text",
    "muted",
    "primary",
    "secondary",
    "success",
    "warning",
    "danger",
)
_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


class ThemeAssetError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 422):
        self.code = code
        self.status_code = status_code
        super().__init__(message)

    def detail(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "retryable": False,
        }


def normalize_palette(value: object, *, require_all: bool = True) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ThemeAssetError("invalid_palette", "色板必须是一个颜色对象")
    if require_all and set(value) != set(PALETTE_KEYS):
        raise ThemeAssetError("invalid_palette", "色板必须完整包含 12 个语义颜色")
    if not set(value) <= set(PALETTE_KEYS):
        raise ThemeAssetError("invalid_palette", "色板包含不受支持的颜色字段")
    palette: dict[str, str] = {}
    for key, raw in value.items():
        color = str(raw).strip()
        if not _HEX_COLOR.fullmatch(color):
            raise ThemeAssetError(
                "invalid_palette", f"{key} 必须使用 #RRGGBB 颜色格式"
            )
        palette[key] = color.upper()
    return palette


def parse_palette_json(value: str) -> dict[str, str]:
    try:
        payload = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ThemeAssetError("invalid_palette", "色板 JSON 无法解析") from exc
    return normalize_palette(payload)


def validate_source_theme_id(value: str) -> str:
    source_theme_id = value.strip()
    if source_theme_id not in BUILTIN_THEME_IDS:
        raise ThemeAssetError("invalid_source_theme", "来源主题不存在")
    return source_theme_id


def _kind_and_extension(header: bytes) -> tuple[str, str, str]:
    if header.startswith(b"\xff\xd8\xff"):
        return "image", ".jpg", "image/jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", ".png", "image/png"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image", ".gif", "image/gif"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "image", ".webp", "image/webp"
    if len(header) >= 12 and header[4:8] == b"ftyp":
        return "video", ".mp4", "video/mp4"
    if header.startswith(b"\x1aE\xdf\xa3"):
        return "video", ".webm", "video/webm"
    raise ThemeAssetError(
        "unsupported_media",
        "只支持 JPEG、PNG、WebP、GIF、H.264 MP4 或 WebM 壁纸",
    )


def _verify_image(path: Path, mime_type: str, poster: Path) -> dict[str, Any]:
    try:
        with Image.open(path) as image:
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                raise ThemeAssetError("image_too_large", "图片像素超过 4000 万上限")
            frames = int(getattr(image, "n_frames", 1) or 1)
            is_dynamic = bool(getattr(image, "is_animated", False) and frames > 1)
            duration_ms = 0
            for frame in ImageSequence.Iterator(image):
                frame.load()
                duration_ms += max(0, int(frame.info.get("duration", 0) or 0))
                if duration_ms > int(MAX_ANIMATED_IMAGE_SECONDS * 1000):
                    raise ThemeAssetError(
                        "animation_too_long", "动画图片时长不能超过 60 秒"
                    )
            if is_dynamic:
                image.seek(0)
                first_frame = image.convert("RGB")
                first_frame.save(poster, format="WEBP", quality=88, method=6)
    except ThemeAssetError:
        raise
    except Exception as exc:
        raise ThemeAssetError("unsupported_media", "图片损坏或格式不受支持") from exc
    return {
        "media_kind": "dynamic" if is_dynamic else "static",
        "mime_type": mime_type,
        "width": width,
        "height": height,
        "duration_seconds": duration_ms / 1000 if is_dynamic else 0.0,
        "poster_path": poster if is_dynamic else path,
    }


def _run(command: list[str], *, timeout: int) -> subprocess.CompletedProcess:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(
        command,
        capture_output=True,
        text=False,
        timeout=timeout,
        creationflags=creationflags,
        check=False,
    )


def _verify_video(path: Path, expected_mime: str, poster: Path) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe")
    ffmpeg = shutil.which("ffmpeg")
    if not ffprobe or not ffmpeg:
        raise ThemeAssetError(
            "ffmpeg_unavailable",
            "需要安装 ffmpeg 才能验证动态壁纸",
            status_code=503,
        )
    probe = _run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,codec_type,width,height,duration:format=format_name,duration",
            "-of",
            "json",
            str(path),
        ],
        timeout=30,
    )
    if probe.returncode != 0:
        raise ThemeAssetError("unsupported_media", "视频损坏或没有可播放的视频轨道")
    try:
        payload = json.loads(probe.stdout.decode("utf-8", errors="replace"))
        stream = next(
            item
            for item in payload.get("streams") or []
            if item.get("codec_type") == "video"
        )
        codec = str(stream.get("codec_name") or "").lower()
        format_name = str((payload.get("format") or {}).get("format_name") or "")
        width = int(stream.get("width") or 0)
        height = int(stream.get("height") or 0)
        duration = float(
            stream.get("duration")
            or (payload.get("format") or {}).get("duration")
            or 0
        )
    except (StopIteration, TypeError, ValueError) as exc:
        raise ThemeAssetError("unsupported_media", "无法读取视频结构") from exc
    if expected_mime == "video/mp4" and (codec != "h264" or "mp4" not in format_name):
        raise ThemeAssetError("unsupported_codec", "MP4 动态壁纸必须使用 H.264 编码")
    if expected_mime == "video/webm" and (
        codec not in {"vp8", "vp9", "av1"} or "webm" not in format_name
    ):
        raise ThemeAssetError("unsupported_codec", "WebM 必须使用 VP8、VP9 或 AV1 编码")
    if width <= 0 or height <= 0 or width * height > MAX_VIDEO_PIXELS:
        raise ThemeAssetError("video_too_large", "动态壁纸分辨率不能超过 4K")
    if duration <= 0 or duration > MAX_VIDEO_SECONDS:
        raise ThemeAssetError("video_too_long", "动态壁纸时长必须在 5 分钟以内")

    integrity = _run(
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
            "-an",
            "-f",
            "null",
            "-",
        ],
        timeout=180,
    )
    if integrity.returncode != 0:
        raise ThemeAssetError("unsupported_media", "视频文件不完整或已损坏")
    poster_result = _run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-vf",
            "scale='min(1920,iw)':-2",
            "-y",
            str(poster),
        ],
        timeout=60,
    )
    if poster_result.returncode != 0 or not poster.is_file():
        raise ThemeAssetError("poster_failed", "无法生成动态壁纸封面")
    return {
        "media_kind": "dynamic",
        "mime_type": expected_mime,
        "width": width,
        "height": height,
        "duration_seconds": duration,
        "poster_path": poster,
    }


async def _save_theme_upload(upload: Any, identifier: str) -> dict[str, Any]:
    THEME_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    THEME_ASSET_ROOT.mkdir(parents=True, exist_ok=True)
    temporary = THEME_TEMP_ROOT / f"{identifier}.theme-uploading"
    digest = hashlib.sha256()
    size = 0
    header = b""
    final: Path | None = None
    poster: Path | None = None
    try:
        with temporary.open("wb") as output:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                if len(header) < 32:
                    header += chunk[: 32 - len(header)]
                size += len(chunk)
                if size > DYNAMIC_LIMIT:
                    raise ThemeAssetError("file_too_large", "动态壁纸不能超过 256 MB")
                digest.update(chunk)
                output.write(chunk)
        if not size:
            raise ThemeAssetError("empty_file", "上传文件为空")
        media_family, extension, mime_type = _kind_and_extension(header)
        if media_family == "image" and size > STATIC_LIMIT:
            raise ThemeAssetError("file_too_large", "图片或动画图片不能超过 30 MB")
        final = THEME_ASSET_ROOT / f"{identifier}{extension}"
        poster = THEME_ASSET_ROOT / f"{identifier}.poster.webp"
        temporary.replace(final)
        if media_family == "image":
            details = await asyncio.to_thread(_verify_image, final, mime_type, poster)
        else:
            details = await asyncio.to_thread(_verify_video, final, mime_type, poster)
        return {
            **details,
            "stored_path": final,
            "size_bytes": size,
            "sha256": digest.hexdigest(),
        }
    except Exception:
        temporary.unlink(missing_ok=True)
        if final is not None:
            final.unlink(missing_ok=True)
        if poster is not None and poster != final:
            poster.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()


def theme_view(theme: CustomTheme) -> dict[str, Any]:
    has_media = bool(theme.stored_path)
    return {
        "id": theme.id,
        "name": theme.name,
        "source_theme_id": theme.source_theme_id,
        "media_kind": theme.media_kind,
        "mime_type": theme.mime_type,
        "size_bytes": theme.size_bytes,
        "sha256": theme.sha256,
        "width": theme.width,
        "height": theme.height,
        "duration_seconds": theme.duration_seconds,
        "palette": dict(theme.palette or {}),
        "content_url": f"/api/themes/{theme.id}/content" if has_media else None,
        "poster_url": f"/api/themes/{theme.id}/poster" if has_media else None,
        "created_at": theme.created_at,
        "updated_at": theme.updated_at,
    }


async def list_custom_themes(session: AsyncSession) -> list[CustomTheme]:
    return list(
        (
            await session.execute(
                select(CustomTheme).order_by(CustomTheme.updated_at.desc(), CustomTheme.id)
            )
        ).scalars()
    )


async def create_custom_theme(
    session: AsyncSession,
    *,
    name: str,
    source_theme_id: str,
    palette: dict[str, str],
    upload: Any | None,
) -> CustomTheme:
    cleaned_name = name.strip()
    if not cleaned_name or len(cleaned_name) > 80:
        raise ThemeAssetError("invalid_name", "主题名称长度必须为 1 到 80 个字符")
    source = validate_source_theme_id(source_theme_id)
    colors = normalize_palette(palette)
    identifier = str(uuid.uuid4())
    asset: dict[str, Any] | None = None
    if upload is not None and getattr(upload, "filename", ""):
        asset = await _save_theme_upload(upload, identifier)
    theme = CustomTheme(
        id=identifier,
        name=cleaned_name,
        source_theme_id=source,
        media_kind=str(asset["media_kind"]) if asset else "inherited",
        mime_type=str(asset["mime_type"]) if asset else None,
        stored_path=str(asset["stored_path"]) if asset else None,
        poster_path=str(asset["poster_path"]) if asset else None,
        size_bytes=int(asset["size_bytes"]) if asset else 0,
        sha256=str(asset["sha256"]) if asset else None,
        width=int(asset["width"]) if asset else 0,
        height=int(asset["height"]) if asset else 0,
        duration_seconds=float(asset["duration_seconds"]) if asset else 0,
        palette=colors,
    )
    session.add(theme)
    try:
        await session.commit()
        await session.refresh(theme)
    except Exception:
        await session.rollback()
        if asset:
            unlink_with_retries(Path(asset["stored_path"]))
            poster_path = Path(asset["poster_path"])
            if poster_path != Path(asset["stored_path"]):
                unlink_with_retries(poster_path)
        raise
    return theme


async def update_custom_theme(
    session: AsyncSession,
    theme_id: str,
    *,
    name: str | None,
    palette: dict[str, str] | None,
) -> CustomTheme:
    theme = await session.get(CustomTheme, theme_id)
    if not theme:
        raise ThemeAssetError("theme_not_found", "自定义主题不存在", status_code=404)
    if name is not None:
        cleaned_name = name.strip()
        if not cleaned_name or len(cleaned_name) > 80:
            raise ThemeAssetError("invalid_name", "主题名称长度必须为 1 到 80 个字符")
        theme.name = cleaned_name
    if palette is not None:
        theme.palette = normalize_palette(palette)
    await session.commit()
    await session.refresh(theme)
    return theme


async def delete_custom_theme(session: AsyncSession, theme_id: str) -> bool:
    theme = await session.get(CustomTheme, theme_id)
    if not theme:
        raise ThemeAssetError("theme_not_found", "自定义主题不存在", status_code=404)
    paths = [
        path
        for value in (theme.stored_path, theme.poster_path)
        if (path := resolve_theme_path(value)) is not None
    ]
    await session.delete(theme)
    await session.commit()
    removed = True
    for path in dict.fromkeys(paths):
        removed = unlink_with_retries(path) and removed
    return removed


def resolve_theme_path(value: str | None) -> Path | None:
    if not value:
        return None
    root = THEME_ASSET_ROOT.resolve()
    try:
        path = Path(value).resolve()
    except OSError:
        return None
    return path if path.is_file() and root in path.parents else None
