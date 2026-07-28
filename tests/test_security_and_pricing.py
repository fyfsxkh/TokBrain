from pathlib import Path

import httpx
import pytest

import app.services.downloader as downloader
import app.services.providers as providers
from app.main import app
from app.services.downloader import DownloadError, download_media, is_allowed_media_url
from app.services.errors import (
    classify_error,
    normalized_error_code,
    safe_error_message,
    user_error_message,
)
from app.services.pricing import asr_cost, token_cost
from app.services.f2_links import F2AccessGate, PublicLinkError


async def no_sleep(_seconds: float) -> None:
    return None


async def no_dns(_url: str) -> None:
    return None


async def access_allowed() -> None:
    return None


async def ignore_circuit(_error) -> None:
    return None


def test_media_allowlist_rejects_suffix_confusion_and_plain_http():
    assert is_allowed_media_url("https://v3-web.douyinvod.com/file.mp4")
    assert is_allowed_media_url("https://p3.douyinpic.com/image.jpg")
    assert is_allowed_media_url("https://v5-dy-ov-experiment.zjcdn.com/file.mp4")
    assert not is_allowed_media_url("https://douyinvod.com.evil.example/file.mp4")
    assert not is_allowed_media_url("http://v3-web.douyinvod.com/file.mp4")


def test_asr_result_url_requires_public_aliyun_https(monkeypatch):
    monkeypatch.setattr(
        providers.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (providers.socket.AF_INET, providers.socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
        ],
    )
    allowed = "https://dashscope-result.oss-cn-beijing.aliyuncs.com/result.json"
    assert providers._validate_asr_result_url(allowed) == allowed

    with pytest.raises(RuntimeError):
        providers._validate_asr_result_url(
            "https://aliyuncs.com.evil.example/result.json"
        )
    with pytest.raises(RuntimeError):
        providers._validate_asr_result_url(
            "http://dashscope-result.oss-cn-beijing.aliyuncs.com/result.json"
        )

    monkeypatch.setattr(
        providers.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (providers.socket.AF_INET, providers.socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
        ],
    )
    with pytest.raises(RuntimeError, match="公网"):
        providers._validate_asr_result_url(allowed)


def test_asr_result_download_bounds_redirects_and_size():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if len(calls) == 1:
            return httpx.Response(
                302,
                headers={"location": "/final.json?signature=synthetic"},
            )
        return httpx.Response(200, json={"transcripts": []})

    url = "https://dashscope-result.oss-cn-beijing.aliyuncs.com/start.json"
    payload = providers._download_asr_result(
        url,
        transport=httpx.MockTransport(handler),
        validate_url=lambda value: value,
    )
    assert payload == {"transcripts": []}
    assert len(calls) == 2

    oversized = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            headers={
                "content-length": str(providers.ASR_RESULT_MAX_BYTES + 1),
            },
            content=b"{}",
        )
    )
    with pytest.raises(RuntimeError, match="10 MB"):
        providers._download_asr_result(
            url,
            transport=oversized,
            validate_url=lambda value: value,
        )


def test_official_list_price_estimate_units():
    assert token_cost("qwen3.5-ocr", 1_000_000, 1_000_000) == 2.5
    assert token_cost("text-embedding-v4", 1_000) == 0.0005
    assert asr_cost("paraformer-v2", 60) == pytest.approx(0.0048)


def test_error_messages_strip_query_and_never_invent_private_or_deleted_reason():
    message = safe_error_message(
        "403 for https://v26-web.douyinvod.com/video/file?token=secret&expires=1"
    )
    assert message == "403 for https://v26-web.douyinvod.com"
    assert "secret" not in message
    assert "上传本地文件" in user_error_message("media_expired", message)
    unavailable = user_error_message("work_unavailable", "unknown")
    assert "可能" in unavailable
    assert classify_error(PublicLinkError("network_error")) == "network_error"
    assert normalized_error_code(None, None) is None


async def test_media_downloader_retries_five_x_once_and_validates_mime(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        downloader,
        "f2_access_gate",
        F2AccessGate(sleep=no_sleep, uniform=lambda _low, _high: 4.0),
    )
    monkeypatch.setattr(downloader, "_ensure_f2_access_allowed", access_allowed)
    monkeypatch.setattr(downloader, "_persist_f2_circuit", ignore_circuit)
    calls = 0
    waits: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(502, request=request)
        return httpx.Response(
            200,
            headers={"content-type": "video/mp4"},
            content=b"\x00\x00\x00\x18ftypisomvideo",
            request=request,
        )

    async def retry_sleep(seconds: float) -> None:
        waits.append(seconds)

    target = tmp_path / "video.mp4"
    result = await download_media(
        "https://v3-web.douyinvod.com/video.mp4",
        target,
        max_bytes=1024,
        transport=httpx.MockTransport(handler),
        dns_check=no_dns,
        sleep=retry_sleep,
        uniform=lambda _low, _high: 18.0,
    )
    assert result == target
    assert target.read_bytes().startswith(b"\x00\x00\x00\x18ftyp")
    assert calls == 2
    assert waits == [18.0]


async def test_media_downloader_rejects_html_and_oversized_response(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        downloader,
        "f2_access_gate",
        F2AccessGate(sleep=no_sleep, uniform=lambda _low, _high: 4.0),
    )
    monkeypatch.setattr(downloader, "_ensure_f2_access_allowed", access_allowed)
    monkeypatch.setattr(downloader, "_persist_f2_circuit", ignore_circuit)

    def html(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html>not media</html>",
            request=request,
        )

    with pytest.raises(DownloadError) as captured:
        await download_media(
            "https://v3-web.douyinvod.com/video.mp4",
            tmp_path / "bad.mp4",
            max_bytes=1024,
            transport=httpx.MockTransport(html),
            dns_check=no_dns,
        )
    assert captured.value.code == "unsupported_content_type"

    def large(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "video/mp4", "content-length": "2048"},
            request=request,
        )

    with pytest.raises(DownloadError) as captured:
        await download_media(
            "https://v3-web.douyinvod.com/video.mp4",
            tmp_path / "large.mp4",
            max_bytes=1024,
            transport=httpx.MockTransport(large),
            dns_check=no_dns,
        )
    assert captured.value.code == "response_too_large"


def test_v4_routes_have_no_legacy_collection_or_auth_entry_points():
    paths = set(app.openapi()["paths"])
    assert "/api/import-batches" in paths
    assert "/api/jobs" in paths
    removed = {
        "/api/auth/status",
        "/api/adapter-health",
        "/api/sync/jobs",
        "/api/library/collections/refresh",
    }
    assert paths.isdisjoint(removed)


def test_runtime_dependencies_keep_pinned_f2_without_browser_stack():
    root = Path(__file__).resolve().parents[1]
    requirements = (root / "requirements.txt").read_text(encoding="utf-8").lower()
    f2_requirements = (root / "requirements-f2.txt").read_text(
        encoding="utf-8"
    ).lower()
    setup = (root / "scripts" / "setup.ps1").read_text(encoding="utf-8").lower()
    assert "f2==0.0.1.7" not in requirements
    assert "f2==0.0.1.7" in f2_requirements
    assert "requirements-f2.txt" in setup
    assert "--no-deps" in setup
    assert "python-multipart==" in requirements
