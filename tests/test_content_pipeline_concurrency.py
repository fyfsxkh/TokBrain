from pathlib import Path
from types import SimpleNamespace

import pytest

import app.services.content_pipeline as pipeline
from app.services.providers import ProviderUsage, asr_audio_command


def test_asr_audio_is_downsampled_and_compressed_for_upload():
    command = asr_audio_command("ffmpeg", Path("input.mp4"), Path("output.asr.opus"))

    assert command[command.index("-ac") + 1] == "1"
    assert command[command.index("-ar") + 1] == "16000"
    assert command[command.index("-c:a") + 1] == "libopus"
    assert command[command.index("-b:a") + 1] == "32k"
    assert command[-1].endswith(".asr.opus")


async def test_remote_pipeline_finishes_before_database_writes(monkeypatch):
    remote_done = False

    class FakeProvider:
        async def summarize(
            self, _content, *, asset_ids, system_prompt=None, model=None
        ):
            assert asset_ids == []
            assert system_prompt == "自定义总结规则"
            assert model == "qwen3.6-flash"
            return {
                "one_sentence": "概括",
                "sections": [{"kind": "content", "title": "讲了什么", "body": "内容"}],
                "tags": [],
                "asset_ids": [],
            }, ProviderUsage("summary", 1, 1, 0, quantity=2)

        async def embed(self, texts):
            nonlocal remote_done
            remote_done = True
            return [[0.0] for _ in texts], ProviderUsage(
                "embedding", 1, 0, 0, quantity=1
            )

    class FakeSession:
        def add(self, _row):
            assert (
                remote_done
            ), "database write started while remote AI work was still running"

        async def execute(self, _query):
            class Result:
                def scalars(self):
                    return self

                def all(self):
                    return []

            return Result()

    async def fake_secret(_session, _key):
        return "api-key"

    async def fake_runtime(_session):
        return {
            "summary_prompt": "自定义总结规则",
            "processing_model": "qwen3.6-flash",
        }

    async def fake_store(*_args, **_kwargs):
        assert remote_done

    async def fake_replace(*_args, **_kwargs):
        assert remote_done

    provider = FakeProvider()
    monkeypatch.setattr(pipeline, "get_secret", fake_secret)
    monkeypatch.setattr(pipeline, "get_runtime_settings", fake_runtime)
    monkeypatch.setattr(pipeline, "DashScopeProvider", lambda _api_key: provider)
    monkeypatch.setattr(pipeline, "store_summary", fake_store)
    monkeypatch.setattr(pipeline, "_replace_chunks", fake_replace)
    work = SimpleNamespace(
        id=1,
        platform_work_id="contract-work",
        title="标题",
        description="内容",
        author_name="作者",
        kind="video",
        media_urls=[],
        image_urls=[],
        raw_metadata={
            "media_policy": {
                "download_permission": "denied",
                "processing_mode": "subtitle_or_audio",
                "subtitle_texts": [
                    "这是一段来自原始字幕的有效内容，用于验证所有远程模型调用结束之后才开始写入数据库，"
                    "并确认字幕、总结与向量处理都严格遵守既定顺序。"
                ],
            }
        },
        duration_seconds=0,
        processing_state="discovered",
        process_error=None,
        process_attempts=0,
        content_text=None,
    )

    result = await pipeline.process_work(FakeSession(), work, "job-1")  # type: ignore[arg-type]

    assert result == "processed"
    assert remote_done
    assert work.evidence_state == "sufficient"
    assert work.supplement_state == "required"


async def test_restricted_pipeline_prefers_inline_subtitles_without_media_fetch(
    monkeypatch,
):
    async def forbidden(*_args, **_kwargs):
        raise AssertionError(
            "inline subtitles must not fetch video, audio, or subtitle URLs"
        )

    class Provider:
        transcribe = forbidden

    monkeypatch.setattr(pipeline, "download_subtitle", forbidden)
    monkeypatch.setattr(pipeline, "_download_first", forbidden)
    work = SimpleNamespace(
        platform_work_id="restricted-subtitle",
        duration_seconds=20,
    )

    kind, text, usage = await pipeline._restricted_text_or_audio(
        Provider(),  # type: ignore[arg-type]
        work,  # type: ignore[arg-type]
        {
            "subtitle_texts": ["第一句", "第二句"],
            "audio_urls": ["https://v3-web.douyinvod.com/audio.m4a"],
        },
    )

    assert kind == "subtitle"
    assert text == "第一句\n第二句"
    assert usage is None


async def test_restricted_pipeline_downloads_audio_only_for_asr(tmp_path, monkeypatch):
    seen: list[Path] = []

    async def fake_download(urls, target, *, max_bytes):
        assert urls == ["https://v3-web.douyinvod.com/audio.m4a"]
        assert max_bytes > 0
        assert not target.name.endswith(".mp4")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"audio")
        seen.append(target)
        return target

    class Provider:
        async def transcribe(self, media_path, duration_seconds):
            assert media_path == seen[0]
            assert duration_seconds == 30
            return "音频转写", ProviderUsage(
                "asr", 0, 0, 0, metric="audio_seconds", quantity=30
            )

    monkeypatch.setattr(pipeline, "DATA_DIR", tmp_path)
    monkeypatch.setattr(pipeline.shutil, "which", lambda _name: "ffmpeg")
    monkeypatch.setattr(pipeline, "_download_first", fake_download)
    work = SimpleNamespace(
        platform_work_id="restricted-audio",
        duration_seconds=30,
    )

    kind, text, usage = await pipeline._restricted_text_or_audio(
        Provider(),  # type: ignore[arg-type]
        work,  # type: ignore[arg-type]
        {"audio_urls": ["https://v3-web.douyinvod.com/audio.m4a"]},
    )

    assert kind == "transcript"
    assert text == "音频转写"
    assert usage is not None
    assert seen and not seen[0].exists()


async def test_restricted_asr_failure_is_not_misclassified_as_missing_evidence(
    tmp_path, monkeypatch
):
    async def fake_download(_urls, target, **_kwargs):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"audio")
        return target

    class Provider:
        async def transcribe(self, *_args, **_kwargs):
            raise RuntimeError("ASR provider unavailable")

    monkeypatch.setattr(pipeline, "DATA_DIR", tmp_path)
    monkeypatch.setattr(pipeline.shutil, "which", lambda _name: "ffmpeg")
    monkeypatch.setattr(pipeline, "_download_first", fake_download)

    with pytest.raises(RuntimeError, match="ASR provider unavailable"):
        await pipeline._restricted_text_or_audio(
            Provider(),  # type: ignore[arg-type]
            SimpleNamespace(
                platform_work_id="restricted-asr-error",
                duration_seconds=20,
            ),
            {"audio_urls": ["https://v3-web.douyinvod.com/audio.m4a"]},
        )


async def test_denied_policy_ignores_stale_full_video_url(monkeypatch):
    async def forbidden(*_args, **_kwargs):
        raise AssertionError(
            "denied policy must not download or inspect the full video"
        )

    class Provider:
        transcribe = forbidden

        async def summarize(
            self, _content, *, asset_ids, system_prompt=None, model=None
        ):
            return {
                "one_sentence": "仅基础信息",
                "sections": [
                    {"kind": "content", "title": "内容", "body": "仅基础信息"}
                ],
                "tags": [],
                "asset_ids": [],
            }, ProviderUsage("summary", 1, 1, 0, quantity=2)

        async def embed(self, texts):
            return [[0.0] for _ in texts], ProviderUsage(
                "embedding", 1, 0, 0, quantity=1
            )

    class FakeSession:
        def add(self, _row):
            return None

        async def execute(self, _query):
            class Result:
                def scalars(self):
                    return self

                def all(self):
                    return []

            return Result()

    async def fake_secret(_session, _key):
        return "api-key"

    async def fake_runtime(_session):
        return {
            "summary_prompt": "",
            "processing_model": "summary",
        }

    async def no_write(*_args, **_kwargs):
        return None

    monkeypatch.setattr(pipeline, "get_secret", fake_secret)
    monkeypatch.setattr(pipeline, "get_runtime_settings", fake_runtime)
    monkeypatch.setattr(pipeline, "DashScopeProvider", lambda _api_key: Provider())
    monkeypatch.setattr(pipeline, "_download_first", forbidden)
    monkeypatch.setattr(pipeline, "_record", no_write)
    monkeypatch.setattr(pipeline, "store_summary", no_write)
    monkeypatch.setattr(pipeline, "_replace_chunks", no_write)
    work = SimpleNamespace(
        id=2,
        platform_work_id="denied-stale-video",
        title="标题",
        description="简介",
        author_name="作者",
        kind="video",
        media_urls=["https://v3-web.douyinvod.com/stale-video.mp4"],
        image_urls=[],
        raw_metadata={
            "media_policy": {
                "download_permission": "denied",
                "processing_mode": "subtitle_or_audio",
            }
        },
        duration_seconds=10,
        processing_state="discovered",
        process_error=None,
        process_attempts=0,
        content_text=None,
    )

    result = await pipeline.process_work(
        FakeSession(), work, "job-denied"  # type: ignore[arg-type]
    )

    assert result == "evidence_insufficient"
    assert work.content_text == ""
    assert work.evidence_state == "insufficient"
    assert work.supplement_state == "required"
