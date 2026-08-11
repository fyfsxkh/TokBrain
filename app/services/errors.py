"""Stable, user-safe processing errors for UI grouping and retry decisions."""

from __future__ import annotations

import re
from urllib.parse import urlsplit


_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
_AUTHORIZATION_RE = re.compile(
    r"(?i)(\bauthorization\b[\"']?\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)([\"']?(?:api[_-]?key|access[_-]?key(?:[_-]?(?:id|secret))?|"
    r"token|f2[_-]?cookie|cookie)[\"']?\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;&]+)"
)


def safe_error_message(error: object, *, limit: int = 500) -> str:
    value = str(error or "处理失败")

    def host_only(match: re.Match[str]) -> str:
        raw = match.group(0)
        trimmed = raw.rstrip("'\"),.;")
        suffix = raw[len(trimmed) :]
        parsed = urlsplit(trimmed)
        return f"{parsed.scheme}://{parsed.hostname or '已隐藏地址'}{suffix}"

    value = _URL_RE.sub(host_only, value)
    value = _AUTHORIZATION_RE.sub(r"\1[REDACTED]", value)
    value = _BEARER_RE.sub("Bearer [REDACTED]", value)
    value = _SECRET_ASSIGNMENT_RE.sub(r"\1[REDACTED]", value)
    return value[:limit]


def classify_error(error: object) -> str:
    explicit = str(getattr(error, "code", "") or getattr(error, "kind", "") or "")
    if explicit:
        return explicit
    value = str(error or "").lower()
    if "winerror 32" in value or "另一个程序正在使用" in value:
        return "temporary_file_locked"
    if "媒体地址已失效" in value:
        return "media_expired"
    if "超时" in value or "timeout" in value or "timed out" in value:
        return "network_timeout"
    if (
        "网络设置" in value
        or "网络或 tls" in value
        or "network error" in value
        or "connection error" in value
        or "httpsconnectionpool" in value
        or "sslerror" in value
        or "ssleoferror" in value
        or "proxyerror" in value
    ):
        return "network_error"
    if "ffmpeg" in value:
        return "ffmpeg_unavailable"
    if "api key" in value or "模型" in value and "配置" in value:
        return "model_not_configured"
    if "时长超过" in value:
        return "work_too_long"
    if (
        "额度" in value
        or "处理上限" in value
        or "分钟上限" in value
        or "limit" in value
    ):
        return "budget_deferred"
    if "总结" in value:
        return "summary_generation_failed"
    return "processing_failed"
