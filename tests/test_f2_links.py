import asyncio

import httpx
import pytest

import app.services.f2_links as f2_links
from app.services.f2_links import (
    ERROR_MESSAGES,
    F2AccessGate,
    F2WorkClient,
    PublicLinkError,
    _classify_f2_exception,
    _parse_f2_response,
    direct_work_id,
    extract_links,
    normalize_input_url,
    sanitize_url,
    validate_media_url,
)


VIDEO_ID = "7351234567890123456"
VIDEO_URL = f"https://www.douyin.com/video/{VIDEO_ID}"
MEDIA_URL = "https://v3-web.douyinvod.com/video.mp4"
AUDIO_URL = "https://v3-web.douyinvod.com/audio.m4a"


def video_payload(work_id: str = VIDEO_ID, *, allow_download: bool = True) -> dict:
    return {
        "status_code": 0,
        "aweme_detail": {
            "aweme_id": work_id,
            "aweme_type": 0,
            "desc": "离线 F2 测试视频",
            "duration": 12340,
            "author": {
                "uid": "author-1",
                "sec_uid": "sec-author-1",
                "nickname": "测试作者",
            },
            "video_control": {"allow_download": allow_download},
            "music": {"play_url": {"url_list": [AUDIO_URL]}},
            "video": {
                "origin_cover": {"url_list": ["https://p3.douyinpic.com/cover.jpg"]},
                "bit_rate": [{"play_addr": {"url_list": [MEDIA_URL]}}],
            },
        },
    }


async def no_dns(_url: str) -> None:
    return None


def test_extracts_links_and_removes_tracking_data():
    text = "视频一 https://v.douyin.com/AbCd/，\n" f"视频二 {VIDEO_URL}?from=share。"
    assert extract_links(text) == [
        "https://v.douyin.com/AbCd/",
        f"{VIDEO_URL}?from=share",
    ]
    assert normalize_input_url(extract_links(text)[1]) == VIDEO_URL
    assert direct_work_id(extract_links(text)[1]) == VIDEO_ID
    assert sanitize_url(extract_links(text)[1]) == VIDEO_URL


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("http://www.douyin.com/video/1", "invalid_url"),
        ("https://example.com/video/1", "unsupported_host"),
        ("https://user@www.douyin.com/video/1", "invalid_url"),
        ("https://www.douyin.com:444/video/1", "invalid_url"),
    ],
)
def test_rejects_unsafe_page_urls(url, code):
    with pytest.raises(PublicLinkError) as captured:
        normalize_input_url(url)
    assert captured.value.code == code


async def test_public_dns_rejects_private_resolution(monkeypatch):
    monkeypatch.setattr(
        f2_links.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (
                f2_links.socket.AF_INET,
                f2_links.socket.SOCK_STREAM,
                6,
                "",
                ("127.0.0.1", 443),
            )
        ],
    )

    with pytest.raises(PublicLinkError) as captured:
        await f2_links.ensure_public_dns(VIDEO_URL)

    assert captured.value.code == "redirect_blocked"


def test_media_allowlist_rejects_suffix_confusion_and_plain_http():
    assert validate_media_url("https://v3-web.douyinvod.com/file.mp4")
    assert validate_media_url("https://p3.douyinpic.com/image.jpg")
    with pytest.raises(PublicLinkError):
        validate_media_url("https://douyinvod.com.evil.example/file.mp4")
    with pytest.raises(PublicLinkError):
        validate_media_url("http://v3-web.douyinvod.com/file.mp4")


async def test_direct_video_uses_only_f2_detail_and_passes_empty_cookie():
    calls: list[tuple[str, str]] = []

    async def detail(work_id: str, cookie: str) -> dict:
        calls.append((work_id, cookie))
        return video_payload(work_id)

    work = await F2WorkClient(detail_fetcher=detail).resolve(VIDEO_URL)
    assert calls == [(VIDEO_ID, "")]
    assert work.platform_work_id == VIDEO_ID
    assert work.kind == "video"
    assert work.title == "离线 F2 测试视频"
    assert work.author_name == "测试作者"
    assert work.duration_seconds == pytest.approx(12.34)
    assert work.download_permission == "allowed"
    assert work.processing_mode == "full_media"
    assert work.media_urls == [MEDIA_URL]
    assert work.audio_urls == [AUDIO_URL]
    assert work.raw_metadata["source"] == "f2-post-detail"


async def test_denied_download_keeps_only_audio_and_metadata():
    async def detail(work_id: str, _cookie: str) -> dict:
        payload = video_payload(work_id, allow_download=False)
        payload["aweme_detail"]["video"]["subtitleInfos"] = [
            {
                "Url": "https://v3-web.douyinvod.com/subtitles.json",
                "LanguageCodeName": "chi",
            }
        ]
        return payload

    work = await F2WorkClient(detail_fetcher=detail).resolve(VIDEO_URL)

    assert work.download_permission == "denied"
    assert work.processing_mode == "subtitle_or_audio"
    assert work.media_urls == []
    assert work.image_urls == []
    assert work.audio_urls == [AUDIO_URL]
    assert work.subtitle_urls == ["https://v3-web.douyinvod.com/subtitles.json"]
    assert work.raw_metadata["media_policy"]["download_permission"] == "denied"


async def test_missing_download_signal_fails_closed_without_full_media():
    payload = video_payload()
    del payload["aweme_detail"]["video_control"]

    async def detail(_work_id: str, _cookie: str) -> dict:
        return payload

    work = await F2WorkClient(detail_fetcher=detail).resolve(VIDEO_URL)

    assert work.download_permission == "unknown"
    assert work.processing_mode == "subtitle_or_audio"
    assert work.media_urls == []


async def test_optional_cookie_is_forwarded_without_account_verification():
    seen = ""

    async def detail(work_id: str, cookie: str) -> dict:
        nonlocal seen
        seen = cookie
        return video_payload(work_id)

    await F2WorkClient(detail_fetcher=detail).resolve(
        VIDEO_URL, cookie="sessionid=optional"
    )
    assert seen == "sessionid=optional"


async def test_f2_adapter_performs_one_internal_attempt(monkeypatch):
    from f2.apps.douyin import crawler as crawler_module

    seen: dict = {}

    class FakeCrawler:
        def __init__(self, kwargs):
            seen["kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def fetch_post_detail(self, params):
            seen["params"] = params.model_dump()
            return video_payload()

    monkeypatch.setattr(crawler_module, "DouyinCrawler", FakeCrawler)

    payload = await F2WorkClient()._fetch_detail(VIDEO_ID, "sessionid=optional")

    assert payload["status_code"] == 0
    assert seen["params"]["aweme_id"] == VIDEO_ID
    assert seen["kwargs"]["max_retries"] == 1


def test_non_json_f2_response_is_not_reported_as_unavailable_work():
    response = httpx.Response(
        200,
        content=b"<html><body>temporary upstream response</body></html>",
    )

    with pytest.raises(PublicLinkError) as captured:
        _parse_f2_response(response, has_cookie=True)

    assert captured.value.code == "f2_response_invalid"


def test_empty_f2_response_without_cookie_requests_cookie():
    class APIRetryExhaustedError(Exception):
        pass

    error = _classify_f2_exception(
        APIRetryExhaustedError("empty response"),
        has_cookie=False,
    )

    assert error.code == "f2_cookie_required"


def test_f2_risk_page_opens_circuit_without_exposing_body():
    response = httpx.Response(
        200,
        content=b"<html><body>secsdk-captcha secret-token</body></html>",
    )

    with pytest.raises(PublicLinkError) as captured:
        _parse_f2_response(response, has_cookie=True)

    assert captured.value.code == "risk_verification"
    assert captured.value.opens_circuit is True
    assert "secret-token" not in str(captured.value)


@pytest.mark.parametrize("has_cookie", [False, True])
def test_f2_403_response_always_opens_circuit(has_cookie):
    with pytest.raises(PublicLinkError) as captured:
        _parse_f2_response(httpx.Response(403), has_cookie=has_cookie)

    assert captured.value.code == "access_forbidden"
    assert captured.value.opens_circuit is True


@pytest.mark.parametrize("has_cookie", [False, True])
def test_f2_403_exception_always_opens_circuit(has_cookie):
    class APIForbiddenError(Exception):
        status_code = 403

    error = _classify_f2_exception(APIForbiddenError("forbidden"), has_cookie=has_cookie)

    assert error.code == "access_forbidden"
    assert error.opens_circuit is True


async def test_note_payload_is_normalized_as_image_post():
    note_id = "7359999999999999999"

    async def detail(_work_id: str, _cookie: str) -> dict:
        return {
            "status_code": 0,
            "aweme_detail": {
                "aweme_id": note_id,
                "aweme_type": 68,
                "video_control": {"allow_download": True},
                "desc": "图文作品",
                "author": {"nickname": "作者"},
                "images": [
                    {"url_list": ["https://p1.douyinpic.com/1.webp"]},
                    {"url_list": ["https://p1.douyinpic.com/2.webp"]},
                ],
            },
        }

    work = await F2WorkClient(detail_fetcher=detail).resolve(
        f"https://www.douyin.com/note/{note_id}"
    )
    assert work.kind == "image"
    assert len(work.image_urls) == 2


async def test_short_link_checks_redirect_before_f2_detail():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"location": VIDEO_URL},
            request=request,
        )

    async def detail(work_id: str, _cookie: str) -> dict:
        return video_payload(work_id)

    work = await F2WorkClient(
        transport=httpx.MockTransport(handler),
        dns_check=no_dns,
        detail_fetcher=detail,
    ).resolve("https://v.douyin.com/short")
    assert work.platform_work_id == VIDEO_ID


async def test_short_link_blocks_foreign_redirect():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"location": "https://example.com/private"},
            request=request,
        )

    client = F2WorkClient(
        transport=httpx.MockTransport(handler),
        dns_check=no_dns,
        detail_fetcher=lambda _work_id, _cookie: video_payload(),
    )
    with pytest.raises(PublicLinkError) as captured:
        await client.resolve("https://v.douyin.com/short")
    assert captured.value.code == "redirect_blocked"


async def test_f2_network_error_retries_once_with_injected_delay():
    calls = 0
    waits: list[float] = []

    async def detail(work_id: str, _cookie: str) -> dict:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("offline")
        return video_payload(work_id)

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    result = await F2WorkClient(
        detail_fetcher=detail,
        sleep=sleep,
        uniform=lambda _low, _high: 21.0,
    ).resolve(VIDEO_URL)
    assert result.platform_work_id == VIDEO_ID
    assert calls == 2
    assert waits == [21.0]


async def test_empty_cookie_has_independent_failure_code():
    async def detail(_work_id: str, _cookie: str) -> dict:
        return {"status_code": 2190008, "status_msg": "login required"}

    with pytest.raises(PublicLinkError) as captured:
        await F2WorkClient(detail_fetcher=detail).resolve(VIDEO_URL)
    assert captured.value.code == "f2_cookie_required"


async def test_empty_f2_payload_has_independent_invalid_response_code():
    async def detail(_work_id: str, _cookie: str) -> dict:
        return {}

    with pytest.raises(PublicLinkError) as captured:
        await F2WorkClient(detail_fetcher=detail).resolve(
            VIDEO_URL, cookie="sessionid=optional"
        )

    assert captured.value.code == "f2_response_invalid"


async def test_empty_f2_payload_without_cookie_requests_cookie():
    async def detail(_work_id: str, _cookie: str) -> dict:
        return {}

    with pytest.raises(PublicLinkError) as captured:
        await F2WorkClient(detail_fetcher=detail).resolve(VIDEO_URL)

    assert captured.value.code == "f2_cookie_required"


async def test_f2_contract_change_fails_closed():
    async def detail(_work_id: str, _cookie: str) -> dict:
        return {"status_code": 0, "aweme_detail": {"aweme_id": "invalid"}}

    with pytest.raises(PublicLinkError) as captured:
        await F2WorkClient(detail_fetcher=detail).resolve(
            VIDEO_URL, cookie="optional=1"
        )
    assert captured.value.code == "f2_contract_changed"


async def test_global_gate_is_serial_and_waits_four_to_eight_seconds():
    active = 0
    peak = 0
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        await asyncio.sleep(0)

    gate = F2AccessGate(sleep=sleep, uniform=lambda _low, _high: 6.0)

    async def operation(index: int) -> int:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        return index

    results = await asyncio.gather(
        *(gate.run(lambda i=i: operation(i)) for i in range(6))
    )
    assert results == list(range(6))
    assert peak == 1
    assert gate.max_active == 1
    assert waits == [6.0] * 6


def test_every_required_error_has_an_independent_chinese_message():
    required = {
        "invalid_url",
        "unsupported_host",
        "redirect_blocked",
        "too_many_redirects",
        "daily_limit_exceeded",
        "access_forbidden",
        "rate_limited",
        "risk_verification",
        "network_timeout",
        "network_error",
        "upstream_server_error",
        "unsupported_content_type",
        "f2_cookie_required",
        "f2_response_invalid",
        "f2_contract_changed",
        "work_unavailable",
        "media_missing",
        "media_expired",
        "duplicate_input",
        "already_imported",
        "local_file_required",
        "cancelled_by_user",
        "security_cleanup_required",
    }
    assert required <= ERROR_MESSAGES.keys()
    assert all(ERROR_MESSAGES[code].strip() for code in required)
