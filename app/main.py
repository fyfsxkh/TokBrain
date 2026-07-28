"""FastAPI entry point for the local-only TokBrain knowledge base."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from loguru import logger

from app.config import DATA_DIR, ensure_directories, settings
from app.database import init_db
from app.routers_v2 import (
    chat,
    health,
    imports,
    jobs,
    library,
    settings as settings_router,
)
from app.services.import_queue import coordinator as import_coordinator
from app.services.jobs import coordinator as job_coordinator
from app.services.temp_files import cleanup_stale_temp_media


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_directories()
    removed_temp_files = await asyncio.to_thread(
        cleanup_stale_temp_media, DATA_DIR / "tmp"
    )
    if removed_temp_files:
        logger.info("已清理 {} 个上次遗留的临时媒体文件", removed_temp_files)
    await init_db()
    await import_coordinator.start()
    await job_coordinator.start()
    logger.info("TokBrain 已启动；单作品访问仅由用户提交或确认入库触发")
    yield
    await job_coordinator.stop()
    await import_coordinator.stop()


API_CONTRACT_VERSION = 4


app = FastAPI(
    title=settings.app_name,
    description="把用户主动提交的公开作品链接转换为带来源的本地多模态知识库。",
    version="0.2.0",
    lifespan=lifespan,
)
app.add_middleware(
    TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"]
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type", "X-Requested-With"],
)


@app.middleware("http")
async def reject_foreign_origin(request: Request, call_next):
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        if origin and origin not in settings.allowed_origins:
            return JSONResponse(
                status_code=403, content={"detail": "拒绝非本机页面发起的修改请求"}
            )
    return await call_next(request)


for router in (
    imports.router,
    jobs.router,
    health.router,
    library.router,
    chat.router,
    settings_router.router,
):
    app.include_router(router)


@app.get("/")
async def root():
    return {"name": settings.app_name, "version": "0.2.0", "docs": "/docs"}


@app.get("/health")
async def process_health():
    return {
        "status": "healthy",
        "scope": "process_only",
        "api_contract": API_CONTRACT_VERSION,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=settings.debug,
    )
