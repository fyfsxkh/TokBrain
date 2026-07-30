"""Construct and run the loopback-only TokBrain HTTP application."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from loguru import logger

from app.config import DATA_DIR, ensure_directories, settings
from app.database import init_db
from app.routers_v2 import chat, health, imports, jobs, library
from app.routers_v2 import settings as settings_routes
from app.services.import_queue import coordinator as import_coordinator
from app.services.jobs import coordinator as job_coordinator
from app.services.temp_files import cleanup_stale_temp_media


APP_VERSION = "0.4.0"
API_CONTRACT_VERSION = 4
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_LOCAL_HOSTS = ["127.0.0.1", "localhost", "testserver"]
_ROUTERS = (
    imports.router,
    jobs.router,
    health.router,
    library.router,
    chat.router,
    settings_routes.router,
)


async def _remove_abandoned_media() -> None:
    removed = await asyncio.to_thread(cleanup_stale_temp_media, DATA_DIR / "tmp")
    if removed:
        logger.info("已清理 {} 个上次遗留的临时媒体文件", removed)


@asynccontextmanager
async def _application_lifetime(_application: FastAPI) -> AsyncIterator[None]:
    ensure_directories()
    await _remove_abandoned_media()
    await init_db()
    await import_coordinator.start()
    await job_coordinator.start()
    logger.info("TokBrain 已启动；平台访问只会由用户明确操作触发")
    try:
        yield
    finally:
        await job_coordinator.stop()
        await import_coordinator.stop()


async def _enforce_local_origin(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    origin = request.headers.get("origin")
    if (
        request.method not in _SAFE_METHODS
        and origin is not None
        and origin not in settings.allowed_origins
    ):
        return JSONResponse(
            content={"detail": "拒绝非本机页面发起的修改请求"},
            status_code=403,
        )
    return await call_next(request)


async def _service_index() -> dict[str, str]:
    return {"name": settings.app_name, "version": APP_VERSION, "docs": "/docs"}


async def _process_status() -> dict[str, str | int]:
    return {
        "status": "healthy",
        "scope": "process_only",
        "api_contract": API_CONTRACT_VERSION,
    }


def create_application() -> FastAPI:
    application = FastAPI(
        title=settings.app_name,
        description="把用户主动提交的公开作品链接转换为带来源的本地多模态知识库。",
        version=APP_VERSION,
        lifespan=_application_lifetime,
    )
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=_LOCAL_HOSTS)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Content-Type", "X-Requested-With"],
    )
    application.middleware("http")(_enforce_local_origin)
    for router in _ROUTERS:
        application.include_router(router)
    application.add_api_route("/", _service_index, methods=["GET"])
    application.add_api_route("/health", _process_status, methods=["GET"])
    return application


app = create_application()


def run_local_server() -> None:
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=settings.debug,
    )


if __name__ == "__main__":
    run_local_server()
