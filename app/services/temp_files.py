"""Best-effort cleanup for disposable media files on Windows."""

from __future__ import annotations

import gc
import time
from pathlib import Path

from loguru import logger


def unlink_with_retries(
    path: Path, *, attempts: int = 8, delay_seconds: float = 0.25
) -> bool:
    """Delete a temporary file without turning a Windows sharing lock into job failure."""

    for attempt in range(max(1, attempts)):
        try:
            path.unlink(missing_ok=True)
            return True
        except PermissionError:
            if attempt + 1 >= attempts:
                break
            # Some upload libraries release their file object only during GC.
            gc.collect()
            time.sleep(delay_seconds)
        except OSError as exc:
            if getattr(exc, "winerror", None) != 32:
                logger.warning("临时文件清理失败，将在应用下次启动时重试 {}: {}", path.name, exc)
                return False
            if attempt + 1 >= attempts:
                break
            gc.collect()
            time.sleep(delay_seconds)
    logger.warning("临时文件仍被系统占用，将在应用下次启动时清理: {}", path.name)
    return False


def cleanup_stale_temp_media(root: Path) -> int:
    """Remove files that are always regenerated when an interrupted job resumes."""

    if not root.is_dir():
        return 0
    removed = 0
    for pattern in ("*.asr.wav", "*.asr.opus", "*.mp4"):
        for path in root.glob(pattern):
            if path.is_file() and unlink_with_retries(path):
                removed += 1
    return removed
