"""Stable, user-safe processing errors for UI grouping and retry decisions."""

from __future__ import annotations

import re
from urllib.parse import urlsplit


_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)


def safe_error_message(error: object, *, limit: int = 500) -> str:
    value = str(error or "处理失败")

    def host_only(match: re.Match[str]) -> str:
        raw = match.group(0)
        trimmed = raw.rstrip("'\"),.;")
        suffix = raw[len(trimmed) :]
        parsed = urlsplit(trimmed)
        return f"{parsed.scheme}://{parsed.hostname or '已隐藏地址'}{suffix}"

    value = _URL_RE.sub(host_only, value)
    return value[:limit]


def user_error_message(error_code: str | None, error: object) -> str:
    """Turn old and new processing failures into an actionable UI message."""

    messages = {
        "media_expired": (
            "作品的公开媒体地址已过期。请重新粘贴公开链接或上传本地文件。"
        ),
        "media_missing": "链接解析服务没有返回可处理媒体，请上传本地文件后继续。",
        "page_contract_changed": "旧版公开页面解析失败，请重新提交链接或上传本地文件。",
        "f2_contract_changed": "链接解析服务返回结构已经变化，请升级 TokBrain 或上传本地文件。",
        "f2_cookie_required": "无登录解析未取得作品，可在设置中填写可选 Cookie 后重试。",
        "f2_response_invalid": (
            "链接解析服务未返回有效数据，可能是 Cookie 已失效、平台风控或接口暂不可用。"
            "请刷新 Cookie 后重试，也可以上传本地文件继续。"
        ),
        "work_unavailable": "作品可能不存在、已删除、私密或权限不足。",
        "access_forbidden": "抖音拒绝访问，系统已停止后续平台访问并进入熔断。",
        "rate_limited": "平台请求过于频繁，系统已停止后续访问并进入熔断。",
        "risk_verification": "抖音要求验证码或安全验证，系统不会尝试绕过。",
        "network_error": ("读取作品时网络连接失败，请检查网络设置后重试。"),
        "network_timeout": "读取作品时等待超时，请稍后重试。",
        "redirect_blocked": "公开链接重定向到了不安全或不受支持的地址。",
        "ffmpeg_unavailable": "缺少音视频处理组件，请安装 ffmpeg 后重试。",
        "model_not_configured": "尚未配置模型服务，请先在设置中填写模型 API Key。",
        "work_too_long": "作品时长超过当前处理上限。",
        "budget_deferred": "今日处理额度已用完，作品已保留，可在额度恢复后重试。",
        "summary_generation_failed": "作品内容已保留，但精华总结生成失败，可以单独重试总结。",
        "temporary_file_locked": (
            "Windows 暂时占用了转写音频。重启应用后重新加入待入库即可，"
            "不会影响原始抖音作品。"
        ),
    }
    return messages.get(error_code or "") or safe_error_message(error)


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
    if "网络设置" in value or "network error" in value or "connection error" in value:
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


def normalized_error_code(error_code: str | None, error: object) -> str | None:
    """Correct legacy broad codes when their stored message is more specific."""

    if not error_code and not str(error or "").strip():
        return None
    inferred = classify_error(error)
    if error_code in {"detail_refresh_failed", "processing_failed"} and inferred in {
        "auth_expired",
        "risk_control",
        "timeout",
        "network_error",
        "contract_changed",
        "temporary_file_locked",
        "media_expired",
        "untrusted_media_host",
        "ffmpeg_unavailable",
        "model_not_configured",
        "work_too_long",
        "budget_deferred",
        "summary_generation_failed",
    }:
        return inferred
    return error_code or inferred
