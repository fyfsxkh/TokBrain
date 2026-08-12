"""Local-only runtime checks with no external network access."""

from __future__ import annotations

import shutil

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppSetting, utcnow
from app.services.import_queue import coordinator as import_coordinator
from app.services.jobs import coordinator as job_coordinator
from app.services.package_imports import coordinator as package_import_coordinator

SYSTEM_PROBES = ("database", "media_runtime", "coordinators", "security_cleanup")


def coordinator_snapshots() -> list[dict[str, object]]:
    return [
        import_coordinator.health_snapshot(),
        package_import_coordinator.health_snapshot(),
        job_coordinator.health_snapshot(),
    ]

async def run_system_probe(session: AsyncSession, probe: str) -> dict:
    """Run one local-only probe so the UI can report genuine incremental progress."""
    if probe == "database":
        try:
            await session.execute(text("SELECT 1"))
            return {
                "probe": probe,
                "status": "healthy",
                "message": "本地知识库数据库正常",
                "details": {},
            }
        except Exception as exc:
            return {
                "probe": probe,
                "status": "down",
                "message": f"本地数据库不可用：{type(exc).__name__}",
                "details": {},
            }

    if probe == "media_runtime":
        ffmpeg = shutil.which("ffmpeg")
        return {
            "probe": probe,
            "status": "healthy" if ffmpeg else "degraded",
            "message": "音视频处理工具正常" if ffmpeg else "未检测到 ffmpeg，视频暂时无法入库",
            "details": {"available": bool(ffmpeg)},
        }

    if probe == "coordinators":
        try:
            snapshots = coordinator_snapshots()
        except Exception as exc:
            return {
                "probe": probe,
                "status": "down",
                "message": "无法读取后台协调器状态",
                "details": {"coordinators": [], "last_error": type(exc).__name__},
            }
        alive = all(bool(item.get("alive")) for item in snapshots)
        has_error = any(bool(item.get("last_error")) for item in snapshots)
        status = "down" if not alive else ("degraded" if has_error else "healthy")
        return {
            "probe": probe,
            "status": status,
            "message": (
                "后台协调器正常"
                if status == "healthy"
                else (
                    "后台协调器仍在运行，但最近发生过错误"
                    if status == "degraded"
                    else "一个或多个后台协调器已停止"
                )
            ),
            "details": {"coordinators": snapshots},
        }

    if probe == "security_cleanup":
        try:
            cleanup = await session.get(AppSetting, "security_cleanup")
            cleanup_value = (
                cleanup.value if cleanup and isinstance(cleanup.value, dict) else {}
            )
            cleanup_required = bool(cleanup_value.get("required"))
            return {
                "probe": probe,
                "status": "down" if cleanup_required else "healthy",
                "message": str(
                    cleanup_value.get("message")
                    or "旧敏感数据目录已清理"
                ),
                "details": {"required": cleanup_required},
            }
        except Exception as exc:
            return {
                "probe": probe,
                "status": "down",
                "message": f"无法确认敏感数据清理状态：{type(exc).__name__}",
                "details": {"required": True},
            }

    raise KeyError(probe)


def summarize_system_probes(probes: list[dict]) -> dict:
    if any(item["status"] == "down" for item in probes):
        overall = "down"
    elif any(item["status"] == "degraded" for item in probes):
        overall = "degraded"
    else:
        overall = "healthy"
    return {
        "overall": overall,
        "summary": (
            "本地运行环境正常"
            if overall == "healthy"
            else "部分本地处理能力需要处理"
        ),
        "checked_at": utcnow(),
        "probes": probes,
    }


async def run_system_checks(session: AsyncSession) -> dict:
    probes = [await run_system_probe(session, probe) for probe in SYSTEM_PROBES]
    return summarize_system_probes(probes)
