"""Bounded downloader for media addresses returned by user-triggered F2 parsing."""

from __future__ import annotations

import asyncio
import random
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urljoin

import httpx

from app.services.f2_links import (
    RISK_TOKENS,
    PublicLinkError,
    ensure_public_dns,
    f2_access_gate,
    validate_media_url,
)


class DownloadError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


async def _ensure_f2_access_allowed() -> None:
    from app.database import async_session_factory
    from app.services.import_queue import circuit_state

    async with async_session_factory() as session:
        state = await circuit_state(session)
    if state["active"]:
        raise PublicLinkError(
            str(state["error_code"] or "risk_verification"),
            str(state["message"] or "链接解析访问暂时熔断"),
            opens_circuit=True,
        )


async def _persist_f2_circuit(error: PublicLinkError) -> None:
    from app.database import async_session_factory
    from app.services.import_queue import open_f2_circuit

    async with async_session_factory() as session:
        await open_f2_circuit(session, error_code=error.code, message=str(error))
        await session.commit()


async def download_media(
    url: str,
    target: Path,
    *,
    max_bytes: int,
    transport: httpx.AsyncBaseTransport | None = None,
    dns_check: Callable[[str], Awaitable[None]] = ensure_public_dns,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    uniform: Callable[[float, float], float] = random.uniform,
    _accept: str = "video/*,image/*,audio/*,application/octet-stream",
    _allowed_content_prefixes: tuple[str, ...] = (
        "video/",
        "image/",
        "audio/",
        "application/octet-stream",
    ),
) -> Path:
    async def fetch_once() -> Path:
        current = validate_media_url(url)
        headers = {
            "User-Agent": "TokBrain/0.3 (local user-initiated media downloader)",
            "Accept": _accept,
        }
        target.parent.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(
            timeout=120,
            headers=headers,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        ) as client:
            for redirect_index in range(6):
                current = validate_media_url(current)
                await dns_check(current)
                async with client.stream("GET", current) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise DownloadError(
                                "redirect_blocked", "媒体重定向缺少目标地址"
                            )
                        if redirect_index >= 5:
                            raise DownloadError(
                                "too_many_redirects", "媒体重定向次数过多"
                            )
                        current = validate_media_url(urljoin(current, location))
                        continue
                    if response.status_code == 403:
                        raise PublicLinkError("access_forbidden", opens_circuit=True)
                    if response.status_code == 429:
                        raise PublicLinkError("rate_limited", opens_circuit=True)
                    if response.status_code in {404, 410}:
                        raise DownloadError("media_expired", "公开媒体地址已失效")
                    if response.status_code >= 500:
                        raise DownloadError(
                            "upstream_server_error",
                            "平台媒体服务暂时异常",
                        )
                    try:
                        response.raise_for_status()
                    except httpx.HTTPStatusError as exc:
                        raise DownloadError(
                            "network_error",
                            f"公开媒体下载失败（HTTP {response.status_code}）",
                        ) from exc
                    content_type = response.headers.get("content-type", "").lower()
                    if content_type.startswith("text/html"):
                        preview = bytearray()
                        async for chunk in response.aiter_bytes(32 * 1024):
                            preview.extend(chunk)
                            if len(preview) >= 200_000:
                                break
                        text = preview.decode("utf-8", errors="ignore").lower()
                        if any(token in text for token in RISK_TOKENS):
                            raise PublicLinkError(
                                "risk_verification", opens_circuit=True
                            )
                        raise DownloadError(
                            "unsupported_content_type",
                            "公开媒体返回了网页而不是媒体文件",
                        )
                    if not any(
                        content_type.startswith(prefix)
                        for prefix in _allowed_content_prefixes
                    ):
                        raise DownloadError(
                            "unsupported_content_type",
                            "公开媒体返回了不受支持的内容类型",
                        )
                    declared = int(response.headers.get("content-length") or 0)
                    if declared and declared > max_bytes:
                        raise DownloadError(
                            "response_too_large", "媒体文件超过大小上限"
                        )
                    total = 0
                    with target.open("wb") as output:
                        async for chunk in response.aiter_bytes(1024 * 1024):
                            total += len(chunk)
                            if total > max_bytes:
                                output.close()
                                target.unlink(missing_ok=True)
                                raise DownloadError(
                                    "response_too_large",
                                    "媒体下载过程中超过大小上限",
                                )
                            output.write(chunk)
                    return target
        raise DownloadError("too_many_redirects", "媒体重定向次数过多")

    async def operation() -> Path:
        await _ensure_f2_access_allowed()
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                return await fetch_once()
            except PublicLinkError as exc:
                if exc.opens_circuit:
                    await _persist_f2_circuit(exc)
                raise
            except DownloadError as exc:
                last_error = exc
                if exc.code != "upstream_server_error" or attempt:
                    raise
            except httpx.TimeoutException as exc:
                last_error = DownloadError("network_timeout", "公开媒体下载超时")
                if attempt:
                    raise last_error from exc
            except httpx.RequestError as exc:
                last_error = DownloadError("network_error", "无法连接公开媒体地址")
                if attempt:
                    raise last_error from exc
            target.unlink(missing_ok=True)
            await sleep(uniform(15.0, 30.0))
        if last_error:
            raise last_error
        raise DownloadError("network_error", "无法下载公开媒体")

    return await f2_access_gate.run(operation)


async def download_subtitle(
    url: str,
    target: Path,
    *,
    max_bytes: int = 2 * 1024 * 1024,
) -> Path:
    return await download_media(
        url,
        target,
        max_bytes=max_bytes,
        _accept=(
            "text/vtt,text/plain,application/x-subrip,application/json,"
            "application/octet-stream"
        ),
        _allowed_content_prefixes=(
            "text/",
            "application/x-subrip",
            "application/json",
            "application/octet-stream",
        ),
    )
