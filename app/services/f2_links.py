"""User-initiated Douyin link resolution backed only by F2 post detail."""

from __future__ import annotations

import asyncio
import html
import ipaddress
import math
import random
import re
import socket
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx


MAX_BATCH_LINKS = 10
DAILY_LINK_LIMIT = 150
MIN_LINK_COOLDOWN_SECONDS = 4.0
MAX_LINK_COOLDOWN_SECONDS = 8.0
MAX_REDIRECTS = 5
LINK_TIMEOUT_SECONDS = 20.0
F2_VENDOR_DIR = Path(__file__).resolve().parents[2] / ".vendor"

PAGE_HOST_SUFFIXES = (".douyin.com", ".iesdouyin.com")
MEDIA_HOST_SUFFIXES = (
    ".douyin.com",
    ".douyinvod.com",
    ".douyinpic.com",
    ".byteimg.com",
    ".bytecdn.cn",
    ".bytedance.com",
    ".snssdk.com",
    ".zjcdn.com",
)
URL_RE = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)
WORK_ID_RE = re.compile(r"/(?:video|note)/(?:detail/)?(\d{1,64})(?:/|$)", re.I)
TRAILING_PUNCTUATION = "，。！？；：、）】》〉』」”’]}>.,!?;:'\""
RISK_TOKENS = (
    "captcha",
    "secsdk-captcha",
    "verifycenter",
    "verify-page",
    "验证码",
    "安全验证",
    "访问验证",
    "风控",
)

ERROR_MESSAGES = {
    "invalid_url": "未识别到合法的 HTTPS 链接",
    "unsupported_host": "该链接不是受支持的抖音公开域名",
    "redirect_blocked": "链接重定向到了不允许访问的地址",
    "too_many_redirects": "链接重定向次数过多",
    "daily_limit_exceeded": "已达到今日 150 条链接解析上限",
    "access_forbidden": "抖音拒绝了本次作品解析请求",
    "rate_limited": "抖音请求过于频繁，请等待风控冷却结束",
    "risk_verification": "抖音要求验证码或安全验证",
    "network_timeout": "访问抖音超时",
    "network_error": "无法连接抖音，请检查网络设置",
    "upstream_server_error": "抖音服务暂时异常，重试后仍未恢复",
    "unsupported_content_type": "链接返回了不受支持的内容类型",
    "f2_cookie_required": "无登录状态下未能取得作品；可在设置中填写可选 Cookie 后重试",
    "f2_response_invalid": "链接解析服务未返回有效数据，可能是 Cookie 已失效、平台风控或接口暂不可用",
    "f2_contract_changed": "链接解析服务返回结构已变化，当前版本无法识别作品信息",
    "work_unavailable": "作品可能不存在、已删除、私密或权限不足",
    "media_missing": "已读取作品元数据，但链接解析服务没有返回可处理的媒体",
    "media_expired": "作品媒体地址已经失效",
    "duplicate_input": "该作品已在预检结果中，无需重复添加",
    "already_imported": "该作品已存在于本地知识库",
    "local_file_required": "请上传本地视频或图片后继续",
    "cancelled_by_user": "用户中断前该链接尚未开始",
    "security_cleanup_required": "旧版浏览器敏感数据尚未清理完成",
}


def ensure_f2_runtime() -> None:
    """Prefer the isolated F2 runtime installed by scripts/setup.ps1."""

    if not (F2_VENDOR_DIR / "f2").is_dir():
        return
    vendor_path = str(F2_VENDOR_DIR)
    if vendor_path not in sys.path:
        sys.path.insert(0, vendor_path)


class PublicLinkError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str | None = None,
        *,
        opens_circuit: bool = False,
    ) -> None:
        self.code = code
        self.opens_circuit = opens_circuit
        super().__init__(message or ERROR_MESSAGES.get(code, "链接解析失败"))


@dataclass(slots=True)
class PublicWork:
    platform_work_id: str
    canonical_url: str
    kind: str
    title: str
    description: str = ""
    author_id: str | None = None
    author_name: str | None = None
    duration_seconds: float = 0
    cover_url: str | None = None
    download_permission: str = "unknown"
    processing_mode: str = "subtitle_or_audio"
    audio_urls: list[str] = field(default_factory=list)
    subtitle_urls: list[str] = field(default_factory=list)
    subtitle_texts: list[str] = field(default_factory=list)
    media_urls: list[str] = field(default_factory=list)
    image_urls: list[str] = field(default_factory=list)
    raw_metadata: dict[str, Any] = field(default_factory=dict)


def _allowed_host(host: str | None, *, media: bool = False) -> bool:
    if not host:
        return False
    lowered = f".{host.lower().strip('.')}"
    suffixes = MEDIA_HOST_SUFFIXES if media else PAGE_HOST_SUFFIXES
    return any(lowered.endswith(suffix) for suffix in suffixes)


def _validate_url_shape(url: str, *, media: bool = False) -> str:
    try:
        parsed = urlsplit(str(url).strip())
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise PublicLinkError("invalid_url") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or port not in (None, 443)
    ):
        raise PublicLinkError("invalid_url")
    if not _allowed_host(parsed.hostname, media=media):
        raise PublicLinkError("unsupported_host" if not media else "redirect_blocked")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        address = None
    if address and not address.is_global:
        raise PublicLinkError("redirect_blocked")
    return urlunsplit(
        ("https", parsed.netloc.lower(), parsed.path or "/", parsed.query, "")
    )


def validate_media_url(url: str) -> str:
    return _validate_url_shape(url, media=True)


async def ensure_public_dns(url: str) -> None:
    parsed = urlsplit(url)
    if not parsed.hostname:
        raise PublicLinkError("invalid_url")
    try:
        results = await asyncio.to_thread(
            socket.getaddrinfo, parsed.hostname, 443, type=socket.SOCK_STREAM
        )
    except socket.gaierror as exc:
        raise PublicLinkError("network_error") from exc
    for result in results:
        try:
            address = ipaddress.ip_address(result[4][0])
        except ValueError:
            continue
        if not address.is_global:
            raise PublicLinkError("redirect_blocked")


def extract_links(text: str) -> list[str]:
    links: list[str] = []
    for match in URL_RE.finditer(text or ""):
        value = html.unescape(match.group(0)).rstrip(TRAILING_PUNCTUATION)
        if value:
            links.append(value)
    return links


def normalize_input_url(url: str) -> str:
    value = _validate_url_shape(url)
    parsed = urlsplit(value)
    path = re.sub(r"/+", "/", parsed.path or "/")
    return urlunsplit(("https", parsed.netloc.lower(), path.rstrip("/") or "/", "", ""))


def direct_work_id(url: str) -> str | None:
    match = WORK_ID_RE.search(urlsplit(url).path)
    return match.group(1) if match else None


def sanitize_url(url: str | None) -> str | None:
    if not url:
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _first(value: Any, default: Any = None) -> Any:
    while isinstance(value, (list, tuple)):
        value = value[0] if value else None
    return default if value is None else value


def _strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.startswith("https://") else []
    if isinstance(value, dict):
        result: list[str] = []
        preferred = value.get("url_list")
        if isinstance(preferred, list):
            result.extend(
                str(item) for item in preferred if str(item).startswith("https://")
            )
        for child in value.values():
            result.extend(_strings(child))
        return list(dict.fromkeys(result))
    if isinstance(value, (list, tuple)):
        result: list[str] = []
        for child in value:
            result.extend(_strings(child))
        return list(dict.fromkeys(result))
    return []


def _duration_seconds(row: dict[str, Any]) -> float:
    video = row.get("video")
    for candidate in (
        row.get("video_duration"),
        row.get("duration"),
        video.get("duration") if isinstance(video, dict) else None,
    ):
        candidate = _first(candidate)
        try:
            milliseconds = float(candidate)
        except (TypeError, ValueError):
            continue
        if math.isfinite(milliseconds) and milliseconds > 0:
            return milliseconds / 1000
    return 0.0


def _download_permission(payload: dict[str, Any]) -> tuple[str, str, object]:
    """Return a fail-closed interpretation of the per-work download control."""

    aweme = payload.get("aweme_detail")
    if not isinstance(aweme, dict):
        return "unknown", "missing", None
    control = aweme.get("video_control")
    if not isinstance(control, dict):
        control = {}

    allow_download = control.get("allow_download")
    if isinstance(allow_download, bool):
        return (
            "allowed" if allow_download else "denied",
            "video_control.allow_download",
            allow_download,
        )

    prevent_type = control.get("prevent_download_type")
    if isinstance(prevent_type, int) and not isinstance(prevent_type, bool):
        return (
            "allowed" if prevent_type == 0 else "denied",
            "video_control.prevent_download_type",
            prevent_type,
        )

    prevent_download = aweme.get("prevent_download")
    if isinstance(prevent_download, bool):
        return (
            "denied" if prevent_download else "allowed",
            "aweme_detail.prevent_download",
            prevent_download,
        )

    # `download_setting` is retained for diagnostics by F2, but its enum values
    # are not a documented per-work contract. Do not guess and accidentally
    # enable full-media downloading when only this ambiguous signal is present.
    return "unknown", "missing_or_ambiguous", control.get("download_setting")


def _subtitle_candidates(payload: dict[str, Any]) -> tuple[list[str], list[str]]:
    aweme = payload.get("aweme_detail")
    if not isinstance(aweme, dict):
        return [], []
    video = aweme.get("video")
    video = video if isinstance(video, dict) else {}
    containers = [
        video.get("subtitleInfos"),
        video.get("subtitle_infos"),
        aweme.get("subtitle_infos"),
        aweme.get("video_subtitle"),
    ]
    urls: list[str] = []
    texts: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                lowered = str(key).lower()
                if lowered in {"url", "subtitle_url"}:
                    urls.extend(_strings(child))
                elif lowered in {"text", "content", "subtitle_text"}:
                    if isinstance(child, str) and child.strip():
                        texts.append(child.strip()[:100_000])
                elif isinstance(child, (dict, list, tuple)):
                    visit(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child)

    for container in containers:
        visit(container)
    allowed_urls = [
        url
        for url in dict.fromkeys(urls)
        if _allowed_host(urlsplit(url).hostname, media=True)
    ]
    return allowed_urls, list(dict.fromkeys(texts))


def _add_media_policy(detail: dict[str, Any], payload: dict[str, Any]) -> None:
    permission, permission_source, permission_raw = _download_permission(payload)
    subtitle_urls, subtitle_texts = _subtitle_candidates(payload)
    detail["_download_permission"] = permission
    detail["_download_permission_source"] = permission_source
    detail["_download_permission_raw"] = permission_raw
    detail["_subtitle_urls"] = subtitle_urls
    detail["_subtitle_texts"] = subtitle_texts


def _safe_raw(row: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "aweme_type",
        "create_time",
        "duration",
        "is_ads",
        "is_story",
        "is_top",
        "region",
        "sec_user_id",
        "uid",
        "unique_id",
    }
    return {
        key: value
        for key, value in row.items()
        if key in allowed and isinstance(value, (str, int, float, bool, type(None)))
    }


def _normalize_detail(row: dict[str, Any]) -> PublicWork:
    if not isinstance(row, dict):
        raise PublicLinkError("f2_contract_changed")
    work_id = str(_first(row.get("aweme_id"), "") or "")
    if not re.fullmatch(r"\d{1,64}", work_id):
        raise PublicLinkError("f2_contract_changed")
    try:
        aweme_type = int(_first(row.get("aweme_type"), 0) or 0)
    except (TypeError, ValueError) as exc:
        raise PublicLinkError("f2_contract_changed") from exc
    all_images = [
        url
        for url in _strings(row.get("images"))
        if _allowed_host(urlsplit(url).hostname, media=True)
    ]
    all_media_urls = [
        url
        for url in _strings(row.get("video_play_addr"))
        if _allowed_host(urlsplit(url).hostname, media=True)
    ]
    audio_urls = [
        url
        for url in _strings(row.get("music_play_url"))
        if _allowed_host(urlsplit(url).hostname, media=True)
    ]
    subtitle_urls = [
        url
        for url in _strings(row.get("_subtitle_urls"))
        if _allowed_host(urlsplit(url).hostname, media=True)
    ]
    subtitle_texts = [
        str(value).strip()[:100_000]
        for value in (row.get("_subtitle_texts") or [])
        if isinstance(value, str) and value.strip()
    ]
    permission = str(row.get("_download_permission") or "unknown")
    if permission not in {"allowed", "denied", "unknown"}:
        permission = "unknown"
    is_image_post = bool(all_images or aweme_type in {2, 68, 150})
    # Public image posts are processed independently of the video's download
    # permission flag. That flag is still retained for provenance/diagnostics,
    # but it is not a reliable image-post media policy.
    processing_mode = (
        "full_images"
        if is_image_post
        else ("full_media" if permission == "allowed" else "subtitle_or_audio")
    )
    images = all_images[:12] if is_image_post else []
    media_urls = all_media_urls if permission == "allowed" else []
    covers = [
        url
        for url in _strings(row.get("cover"))
        if _allowed_host(urlsplit(url).hostname, media=True)
    ]
    description = str(
        _first(row.get("desc_raw"), _first(row.get("desc"), "")) or ""
    ).strip()
    author_name = (
        str(
            _first(row.get("nickname_raw"), _first(row.get("nickname"), "")) or ""
        ).strip()
        or None
    )
    author_id = _first(row.get("sec_user_id"), _first(row.get("uid")))
    kind = "image" if is_image_post else "video"
    canonical_url = (
        f"https://www.douyin.com/{'note' if kind == 'image' else 'video'}/{work_id}"
    )
    return PublicWork(
        platform_work_id=work_id,
        canonical_url=canonical_url,
        kind=kind,
        title=(
            description.splitlines()[0][:180] if description else f"抖音作品 {work_id}"
        ),
        description=description,
        author_id=str(author_id) if author_id else None,
        author_name=author_name,
        duration_seconds=_duration_seconds(row),
        cover_url=(covers[0] if covers else (all_images[0] if all_images else None)),
        download_permission=permission,
        processing_mode=processing_mode,
        audio_urls=audio_urls,
        subtitle_urls=subtitle_urls,
        subtitle_texts=subtitle_texts,
        media_urls=media_urls,
        image_urls=images,
        raw_metadata={
            "source": "f2-post-detail",
            "detail": _safe_raw(row),
            "media_policy": {
                "download_permission": permission,
                "permission_source": str(
                    row.get("_download_permission_source") or "missing"
                ),
                "permission_raw": row.get("_download_permission_raw"),
                "processing_mode": processing_mode,
                "audio_urls": audio_urls,
                "subtitle_urls": subtitle_urls,
                "subtitle_texts": subtitle_texts,
                "expected_image_count": len(images),
            },
        },
    )


def _classify_f2_exception(exc: Exception, *, has_cookie: bool) -> PublicLinkError:
    message = str(exc)
    lowered = message.lower()
    names: set[str] = set()
    current: BaseException | None = exc
    while current is not None:
        names.add(current.__class__.__name__.lower())
        current = current.__cause__ or current.__context__
    status_codes = {
        int(status)
        for status in (
            getattr(exc, "status_code", None),
            getattr(getattr(exc, "response", None), "status_code", None),
        )
        if isinstance(status, int)
    }
    if (
        any("timeout" in name for name in names)
        or "timed out" in lowered
        or "超时" in message
    ):
        return PublicLinkError("network_timeout")
    if (
        429 in status_codes
        or "apiratelimiterror" in names
        or "429" in lowered
        or "too many requests" in lowered
    ):
        return PublicLinkError("rate_limited", opens_circuit=True)
    if any(token in lowered for token in ("captcha", "verify", "risk")) or any(
        token in message for token in ("验证码", "安全验证", "风控")
    ):
        return PublicLinkError("risk_verification", opens_circuit=True)
    if 403 in status_codes or "403" in lowered or "forbidden" in lowered:
        return PublicLinkError("access_forbidden", opens_circuit=True)
    if (
        401 in status_codes
        or "apiunauthorizederror" in names
        or any(
            token in lowered for token in ("cookie", "login required", "unauthorized")
        )
    ):
        return PublicLinkError("f2_cookie_required")
    if (
        any(500 <= status <= 599 for status in status_codes)
        or "apiunavailableerror" in names
        or re.search(r"(?:^|\D)5\d\d(?:\D|$)", lowered)
    ):
        return PublicLinkError("upstream_server_error")
    if "apinotfounderror" in names or 404 in status_codes:
        return PublicLinkError("work_unavailable")
    if "apiretryexhaustederror" in names:
        return PublicLinkError(
            "f2_response_invalid" if has_cookie else "f2_cookie_required"
        )
    if "apiresponseerror" in names:
        return PublicLinkError("f2_response_invalid")
    if any(
        token in name
        for name in names
        for token in ("connect", "network", "proxy", "protocol", "dns")
    ):
        return PublicLinkError("network_error")
    if any("jsondecode" in name for name in names):
        return PublicLinkError("f2_response_invalid")
    if any(token in name for name in names for token in ("keyerror", "attributeerror")):
        return PublicLinkError("f2_contract_changed")
    return PublicLinkError("network_error")


def _parse_f2_response(
    response: httpx.Response | None,
    *,
    has_cookie: bool,
) -> dict[str, Any]:
    """Parse one F2 response without logging URLs, cookies, or response bodies."""

    if response is None or not isinstance(response, httpx.Response):
        raise PublicLinkError(
            "f2_response_invalid" if has_cookie else "f2_cookie_required"
        )

    status_code = response.status_code
    if status_code == 401:
        raise PublicLinkError("f2_cookie_required")
    if status_code == 403:
        raise PublicLinkError("access_forbidden", opens_circuit=True)
    if status_code == 429:
        raise PublicLinkError("rate_limited", opens_circuit=True)
    if status_code >= 500:
        raise PublicLinkError("upstream_server_error")
    if status_code == 404:
        raise PublicLinkError("work_unavailable")
    if status_code != 200:
        raise PublicLinkError("f2_response_invalid")

    body_hint = response.text[:200_000].lower()
    if any(token.lower() in body_hint for token in RISK_TOKENS):
        raise PublicLinkError("risk_verification", opens_circuit=True)
    if any(
        token in body_hint
        for token in ("login required", "passport", "unauthorized", "cookie expired")
    ):
        raise PublicLinkError("f2_cookie_required")
    try:
        payload = response.json()
    except (ValueError, UnicodeDecodeError) as exc:
        raise PublicLinkError(
            "f2_response_invalid" if has_cookie else "f2_cookie_required"
        ) from exc
    if not isinstance(payload, dict):
        raise PublicLinkError("f2_contract_changed")
    return payload


DetailFetcher = Callable[[str, str], Awaitable[dict[str, Any]]]


class F2WorkClient:
    """Resolve one submitted link; never calls F2 account or collection APIs."""

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        uniform: Callable[[float, float], float] = random.uniform,
        dns_check: Callable[[str], Awaitable[None]] = ensure_public_dns,
        detail_fetcher: DetailFetcher | None = None,
    ) -> None:
        self.transport = transport
        self.sleep = sleep
        self.uniform = uniform
        self.dns_check = dns_check
        self.detail_fetcher = detail_fetcher

    async def resolve(self, url: str, *, cookie: str = "") -> PublicWork:
        normalized = normalize_input_url(url)
        work_id = direct_work_id(normalized)
        if not work_id:
            work_id = await self._resolve_short_link(normalized)
        last_error: PublicLinkError | None = None
        for attempt in range(2):
            try:
                payload = await self._fetch_detail(work_id, cookie)
                if not isinstance(payload, dict):
                    raise PublicLinkError("f2_contract_changed")
                if not payload:
                    raise PublicLinkError(
                        "f2_response_invalid" if cookie else "f2_cookie_required"
                    )
                status_code = payload.get("status_code")
                status_message = str(
                    payload.get("status_msg")
                    or payload.get("message")
                    or payload.get("prompts")
                    or ""
                )
                if status_code not in (None, 0, "0"):
                    if not cookie and any(
                        token in status_message.lower()
                        for token in ("cookie", "login", "unauthorized")
                    ):
                        raise PublicLinkError("f2_cookie_required")
                    raise PublicLinkError("work_unavailable")
                try:
                    ensure_f2_runtime()
                    from f2.apps.douyin.filter import PostDetailFilter

                    detail = PostDetailFilter(payload)._to_dict()
                except ImportError as exc:
                    raise PublicLinkError(
                        "f2_contract_changed",
                        "未安装兼容版本 f2==0.0.1.7",
                    ) from exc
                except Exception as exc:
                    raise PublicLinkError("f2_contract_changed") from exc
                if not isinstance(detail, dict) or not _first(detail.get("aweme_id")):
                    raise PublicLinkError(
                        "f2_cookie_required" if not cookie else "work_unavailable"
                    )
                _add_media_policy(detail, payload)
                return _normalize_detail(detail)
            except PublicLinkError as exc:
                last_error = exc
                if (
                    exc.code
                    not in {
                        "network_timeout",
                        "network_error",
                        "upstream_server_error",
                    }
                    or attempt
                ):
                    raise
                await self.sleep(self.uniform(15.0, 30.0))
            except Exception as exc:
                classified = _classify_f2_exception(exc, has_cookie=bool(cookie))
                last_error = classified
                if (
                    classified.code
                    not in {
                        "network_timeout",
                        "network_error",
                        "upstream_server_error",
                    }
                    or attempt
                ):
                    raise classified from exc
                await self.sleep(self.uniform(15.0, 30.0))
        raise last_error or PublicLinkError("network_error")

    async def _fetch_detail(self, work_id: str, cookie: str) -> dict[str, Any]:
        if self.detail_fetcher:
            return await self.detail_fetcher(work_id, cookie)
        try:
            ensure_f2_runtime()
            from f2.apps.douyin.crawler import DouyinCrawler
            from f2.apps.douyin.model import PostDetail
        except ImportError as exc:
            raise PublicLinkError(
                "f2_contract_changed",
                "未安装兼容版本 f2==0.0.1.7",
            ) from exc
        kwargs = {
            "headers": {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/130.0.0.0 Safari/537.36"
                ),
                "Referer": "https://www.douyin.com/",
            },
            "proxies": {"http://": None, "https://": None},
            "timeout": LINK_TIMEOUT_SECONDS,
            # F2 0.0.1.7 interprets this as the total number of attempts.
            # Zero skips the request entirely and produces an empty JSON result.
            "max_retries": 1,
            "cookie": cookie.strip(),
        }
        async with DouyinCrawler(kwargs) as crawler:
            crawler.parse_json = lambda response: _parse_f2_response(
                response,
                has_cookie=bool(cookie.strip()),
            )
            return await crawler.fetch_post_detail(PostDetail(aweme_id=work_id))

    async def _resolve_short_link(self, url: str) -> str:
        headers = {
            "User-Agent": "TokBrain/0.3 (local user-initiated F2 link resolver)",
            "Accept": "text/html,application/xhtml+xml",
        }
        try:
            async with httpx.AsyncClient(
                timeout=LINK_TIMEOUT_SECONDS,
                headers=headers,
                follow_redirects=False,
                transport=self.transport,
                trust_env=False,
            ) as client:
                current = url
                for redirect_index in range(MAX_REDIRECTS + 1):
                    current = _validate_url_shape(current)
                    await self.dns_check(current)
                    async with client.stream("GET", current) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            location = response.headers.get("location")
                            if not location:
                                raise PublicLinkError("redirect_blocked")
                            if redirect_index >= MAX_REDIRECTS:
                                raise PublicLinkError("too_many_redirects")
                            try:
                                current = _validate_url_shape(
                                    urljoin(current, location)
                                )
                            except PublicLinkError as exc:
                                raise PublicLinkError("redirect_blocked") from exc
                            work_id = direct_work_id(current)
                            if work_id:
                                return work_id
                            continue
                        if response.status_code == 403:
                            raise PublicLinkError(
                                "access_forbidden", opens_circuit=True
                            )
                        if response.status_code == 429:
                            raise PublicLinkError("rate_limited", opens_circuit=True)
                        if response.status_code >= 500:
                            raise PublicLinkError("upstream_server_error")
                        if response.status_code >= 400:
                            raise PublicLinkError("work_unavailable")
                        work_id = direct_work_id(str(response.url)) or direct_work_id(
                            current
                        )
                        if work_id:
                            return work_id
                        raise PublicLinkError("work_unavailable")
        except PublicLinkError:
            raise
        except httpx.TimeoutException as exc:
            raise PublicLinkError("network_timeout") from exc
        except httpx.HTTPError as exc:
            raise PublicLinkError("network_error") from exc
        raise PublicLinkError("too_many_redirects")


T = TypeVar("T")


class F2AccessGate:
    """Serialize complete F2/media network phases and hold through cooldown."""

    def __init__(
        self,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        uniform: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self._lock = asyncio.Lock()
        self._sleep = sleep
        self._uniform = uniform
        self.active = 0
        self.max_active = 0

    async def run(self, operation: Callable[[], Awaitable[T]]) -> T:
        async with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                return await operation()
            finally:
                self.active -= 1
                await self._sleep(
                    self._uniform(
                        MIN_LINK_COOLDOWN_SECONDS,
                        MAX_LINK_COOLDOWN_SECONDS,
                    )
                )


f2_access_gate = F2AccessGate()
