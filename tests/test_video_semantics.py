from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.content_pipeline as pipeline
from app.services.content_pipeline import (
    _select_keyframe_candidates,
    evidence_text_threshold,
    normalize_evidence_rows,
    normalize_evidence_text,
)
from app.services.keyframes import (
    KeyframeCandidate,
    SceneCandidate,
    parse_audio_stream_output,
)
from app.models import Base, Keyframe, KnowledgeChunk, Work, WorkSummary
from app.services.providers import (
    ProviderUsage,
    TranscriptResult,
    TranscriptSegment,
    normalize_keyframe_selection,
    normalize_visual_requirements,
    parse_transcription_payload,
)


def test_evidence_text_threshold_uses_lowered_contract():
    assert evidence_text_threshold(0) == 40
    assert evidence_text_threshold(None) == 40
    assert evidence_text_threshold(30) == 20
    assert evidence_text_threshold(300) == 60
    assert evidence_text_threshold(1000) == 120


def test_evidence_text_cleanup_removes_timestamps_metadata_and_cross_segment_duplicates():
    assert (
        normalize_evidence_text(
            "WEBVTT\n00:00:01.000 --> 00:00:02.000\n[00:01] 标题\n有效内容\n有效内容",
            metadata_values=("标题", "#标签"),
        )
        == "有效内容"
    )
    assert normalize_evidence_rows(
        [
            ("transcript", "第一句\n重复句", 1.0),
            ("transcript", "重复句\n第二句", 3.0),
        ]
    ) == [
        ("transcript", "第一句\n重复句", 1.0),
        ("transcript", "第二句", 3.0),
    ]


def test_audio_stream_probe_parses_silent_and_spoken_containers():
    assert parse_audio_stream_output('{"streams":[{"codec_type":"video"}]}') is False
    assert (
        parse_audio_stream_output(
            '{"streams":[{"codec_type":"video"},{"codec_type":"audio"}]}'
        )
        is True
    )
    assert parse_audio_stream_output("not-json") is None


def test_transcription_payload_keeps_sentence_timestamps_when_full_text_exists():
    result = parse_transcription_payload(
        {
            "transcripts": [
                {
                    "text": "先打开设置，再开启自动保存。",
                    "sentences": [
                        {"begin_time": 1200, "end_time": 3100, "text": "先打开设置"},
                        {
                            "begin_time": 3200,
                            "end_time": 5800,
                            "text": "再开启自动保存",
                        },
                    ],
                }
            ]
        }
    )

    assert result.text == "先打开设置，再开启自动保存。"
    assert [(item.start_seconds, item.end_seconds) for item in result.segments] == [
        (1.2, 3.1),
        (3.2, 5.8),
    ]


def test_visual_requirement_normalization_clamps_time_and_priority():
    requirements = normalize_visual_requirements(
        {
            "requirements": [
                {
                    "id": "step",
                    "start_seconds": -2,
                    "end_seconds": 15,
                    "need": "看到自动保存开关已开启",
                    "keywords": ["自动保存"],
                    "priority": 9,
                }
            ]
        },
        10,
    )

    assert requirements == [
        {
            "id": "step",
            "start_seconds": 0.0,
            "end_seconds": 10,
            "need": "看到自动保存开关已开启",
            "keywords": ["自动保存"],
            "priority": 3,
        }
    ]


def test_keyframe_selection_rejects_hallucinated_and_duplicate_ids():
    selected = normalize_keyframe_selection(
        {
            "selected": [
                {"id": "frame-2", "score": 1.5, "reason": "命中设置页"},
                {"id": "missing", "score": 1},
                {"id": "frame-2", "score": 0.2},
                {"id": "frame-1", "score": -1},
            ]
        },
        {"frame-1", "frame-2"},
        4,
    )

    assert [item["id"] for item in selected] == ["frame-2", "frame-1"]
    assert [item["score"] for item in selected] == [1.0, 0.0]


async def test_semantic_selection_keeps_ai_reason_and_reserves_timeline_coverage():
    candidates = [
        KeyframeCandidate(
            path=Path(f"candidate-{index}.jpg"),
            candidate=SceneCandidate(float(index * 3 + 1), 0.2, "uniform"),
            perceptual_hash=str(index),
            quality_score=0.7,
        )
        for index in range(6)
    ]

    class Provider:
        async def select_keyframes(self, requirements, payload, *, max_frames, model):
            assert requirements[0]["id"] == "step"
            assert max_frames == 3
            assert model == "planner"
            assert len(payload) == 6
            return [
                {
                    "id": "candidate-002",
                    "score": 0.95,
                    "reason": "出现自动保存开关",
                    "requirement_id": "step",
                }
            ], ProviderUsage("planner", 10, 5, 0, quantity=15)

    selected, usage = await _select_keyframe_candidates(
        Provider(),  # type: ignore[arg-type]
        candidates,
        [{"id": "step", "need": "看到自动保存开关"}],
        max_frames=4,
        min_gap_seconds=2,
        model="planner",
    )

    semantic = next(item for item in selected if item.candidate.timestamp == 7)
    assert semantic.selection_reason == "出现自动保存开关"
    assert semantic.selection_score == 0.95
    assert len(selected) == 4
    assert usage and usage.model == "planner"


async def test_full_video_pipeline_uses_timed_audio_to_select_explainable_frames(
    tmp_path, monkeypatch
):
    captured_material = ""
    added: list[object] = []

    class Session:
        async def execute(self, _query):
            class Result:
                def scalars(self):
                    return self

                def all(self):
                    return []

            return Result()

        def add(self, row):
            added.append(row)

    class Provider:
        async def transcribe_detailed(self, _path, duration):
            assert duration == 10
            return TranscriptResult(
                "打开设置并开启自动保存",
                (TranscriptSegment(2, 5, "打开设置并开启自动保存"),),
            ), ProviderUsage("asr", 0, 0, 0, metric="audio_seconds", quantity=10)

        async def analyze_keyframe(self, path):
            index = path.stem.rsplit("-", 1)[-1]
            return {
                "ocr_text": "自动保存 已开启" if index == "001" else "首页",
                "visual_description": (
                    "设置页中的自动保存开关" if index == "001" else "应用首页"
                ),
            }, ProviderUsage("vision", 10, 5, 0, quantity=15)

        async def plan_visual_requirements(
            self, transcript, *, duration_seconds, model
        ):
            assert transcript.segments[0].start_seconds == 2
            return [
                {"id": "step", "need": "看到自动保存开关", "priority": 3}
            ], ProviderUsage(model, 10, 5, 0, quantity=15)

        async def select_keyframes(
            self, requirements, candidates, *, max_frames, model
        ):
            assert requirements[0]["id"] == "step"
            assert candidates[1]["ocr_text"] == "自动保存 已开启"
            return [
                {
                    "id": "candidate-001",
                    "score": 0.98,
                    "reason": "画面显示自动保存已开启",
                    "requirement_id": "step",
                }
            ], ProviderUsage(model, 10, 5, 0, quantity=15)

        async def summarize(
            self, content, *, asset_ids, system_prompt=None, model=None
        ):
            nonlocal captured_material
            captured_material = content
            return {
                "one_sentence": "开启自动保存",
                "sections": [
                    {"kind": "content", "title": "步骤", "body": "开启自动保存"}
                ],
                "tags": [],
                "asset_ids": asset_ids,
            }, ProviderUsage("summary", 10, 5, 0, quantity=15)

        async def embed(self, texts):
            return [[0.0] for _ in texts], ProviderUsage(
                "embedding", 5, 0, 0, quantity=5
            )

    async def fake_download(_urls, target, *, max_bytes):
        assert max_bytes > 0
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"video")
        return target

    async def fake_extract(_video, output_dir, **_kwargs):
        output_dir.mkdir(parents=True, exist_ok=True)
        result = []
        for index, timestamp in enumerate((1.0, 4.0, 8.0)):
            path = output_dir / f"candidate-{index:03d}.jpg"
            path.write_bytes(b"frame")
            result.append(
                KeyframeCandidate(
                    path,
                    SceneCandidate(timestamp, 0.2, "uniform"),
                    f"hash-{index}",
                    0.8,
                )
            )
        return result, 10.0

    async def fake_finalize(_video, output_dir, selected, *, max_frames):
        promoted = []
        for index, item in enumerate(selected[:max_frames], 1):
            final = output_dir / f"frame-{index:02d}.jpg"
            item.path.replace(final)
            item.path = final
            promoted.append(item)
        return promoted

    async def fake_runtime(_session):
        return {
            "scene_threshold": 0.4,
            "max_scene_candidates": 24,
            "max_keyframes": 2,
            "min_keyframe_gap_seconds": 2,
            "summary_prompt": "",
            "processing_model": "planner",
        }

    async def fake_secret(_session, _key):
        return "key"

    async def no_write(*_args, **_kwargs):
        return None

    monkeypatch.setattr(pipeline, "DATA_DIR", tmp_path)
    monkeypatch.setattr(pipeline, "DashScopeProvider", lambda _key: Provider())
    monkeypatch.setattr(pipeline, "get_secret", fake_secret)
    monkeypatch.setattr(pipeline, "get_runtime_settings", fake_runtime)
    monkeypatch.setattr(pipeline, "_download_first", fake_download)
    monkeypatch.setattr(
        pipeline, "probe_video_duration", lambda *_args: _async_value(10.0)
    )
    monkeypatch.setattr(pipeline, "extract_keyframe_candidates", fake_extract)
    monkeypatch.setattr(pipeline, "finalize_keyframes", fake_finalize)
    monkeypatch.setattr(
        pipeline, "summary_prompt_for_work", lambda *_args: _async_value("")
    )
    monkeypatch.setattr(pipeline, "local_asset_names", lambda _work: ["frame-01.jpg"])
    monkeypatch.setattr(pipeline, "_record", no_write)
    monkeypatch.setattr(pipeline, "store_summary", no_write)
    monkeypatch.setattr(pipeline, "_replace_chunks", no_write)

    monkeypatch.setattr(pipeline.shutil, "which", lambda _name: "ffmpeg")

    work = SimpleNamespace(
        id=9,
        platform_work_id="semantic-video",
        title="教程",
        description="",
        author_name="作者",
        kind="video",
        media_urls=["https://v3-web.douyinvod.com/video.mp4"],
        image_urls=[],
        raw_metadata={"media_policy": {"download_permission": "allowed"}},
        duration_seconds=10,
        processing_state="discovered",
        process_error=None,
        process_attempts=0,
        content_text="",
    )

    state = await pipeline.process_work(Session(), work, "job")  # type: ignore[arg-type]

    frames = [row for row in added if isinstance(row, Keyframe)]
    assert state == "processed"
    assert any(frame.timestamp_seconds == 4 for frame in frames)
    semantic = next(frame for frame in frames if frame.timestamp_seconds == 4)
    assert semantic.selection_reason == "画面显示自动保存已开启"
    assert semantic.ocr_text == "自动保存 已开启"
    assert "[transcript] 打开设置并开启自动保存" in captured_material
    assert "[visual] 设置页中的自动保存开关" in captured_material


async def test_silent_full_video_uses_visual_evidence_without_calling_asr(
    tmp_path, monkeypatch
):
    class Session:
        async def execute(self, _query):
            class Result:
                def scalars(self):
                    return self

                def all(self):
                    return []

            return Result()

        def add(self, _row):
            return None

    class Provider:
        async def transcribe_detailed(self, *_args, **_kwargs):
            raise AssertionError("静音视频不应调用 ASR")

        async def analyze_keyframe(self, _path):
            return {
                "ocr_text": "步骤一",
                "visual_description": "画面展示咖啡滤杯中的研磨咖啡",
            }, ProviderUsage("vision", 2, 1, 0, quantity=3)

        async def summarize(self, content, **_kwargs):
            assert "[visual] 画面展示咖啡滤杯中的研磨咖啡" in content
            return {
                "one_sentence": "咖啡冲煮步骤",
                "sections": [{"kind": "content", "title": "步骤", "body": "冲煮"}],
                "tags": [],
                "asset_ids": [],
            }, ProviderUsage("summary", 2, 1, 0, quantity=3)

        async def embed(self, texts):
            return [[0.0] for _ in texts], ProviderUsage(
                "embedding", 1, 0, 0, quantity=1
            )

    async def fake_download(_urls, target, **_kwargs):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"video")
        return target

    async def fake_extract(_video, output_dir, **_kwargs):
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / "candidate-000.jpg"
        path.write_bytes(b"frame")
        return [
            KeyframeCandidate(
                path,
                SceneCandidate(2.0, 0.5, "uniform"),
                "hash",
                0.8,
            )
        ], 5.0

    async def fake_finalize(_video, _output_dir, selected, **_kwargs):
        return selected

    async def fake_runtime(_session):
        return {
            "scene_threshold": 0.4,
            "max_scene_candidates": 12,
            "max_keyframes": 2,
            "min_keyframe_gap_seconds": 2,
            "summary_prompt": "",
            "processing_model": "model",
        }

    async def no_write(*_args, **_kwargs):
        return None

    monkeypatch.setattr(pipeline, "DATA_DIR", tmp_path)
    monkeypatch.setattr(pipeline, "get_secret", lambda *_args: _async_value("key"))
    monkeypatch.setattr(pipeline, "get_runtime_settings", fake_runtime)
    monkeypatch.setattr(pipeline, "DashScopeProvider", lambda _key: Provider())
    monkeypatch.setattr(pipeline, "_download_first", fake_download)
    monkeypatch.setattr(
        pipeline, "probe_video_duration", lambda *_args: _async_value(5.0)
    )
    monkeypatch.setattr(
        pipeline, "probe_video_has_audio", lambda *_args: _async_value(False)
    )
    monkeypatch.setattr(pipeline, "extract_keyframe_candidates", fake_extract)
    monkeypatch.setattr(pipeline, "finalize_keyframes", fake_finalize)
    monkeypatch.setattr(
        pipeline, "summary_prompt_for_work", lambda *_args: _async_value("")
    )
    monkeypatch.setattr(pipeline, "local_asset_names", lambda _work: [])
    monkeypatch.setattr(pipeline, "_record", no_write)
    monkeypatch.setattr(pipeline, "store_summary", no_write)
    monkeypatch.setattr(pipeline, "_replace_chunks", no_write)
    monkeypatch.setattr(pipeline.shutil, "which", lambda _name: "tool")

    work = SimpleNamespace(
        id=10,
        platform_work_id="silent-video",
        title="静音教程",
        description="",
        author_name=None,
        kind="video",
        media_urls=["https://v3-web.douyinvod.com/video.mp4"],
        image_urls=[],
        raw_metadata={"media_policy": {"download_permission": "allowed"}},
        duration_seconds=5,
        processing_state="discovered",
        process_error=None,
        process_attempts=0,
        content_text="",
    )

    state = await pipeline.process_work(Session(), work, "silent-job")  # type: ignore[arg-type]

    assert state == "processed"
    assert work.evidence_state == "sufficient"
    assert work.supplement_state == "none"
    assert work.track_report["audio"]["status"] == "no_stream"


async def test_image_post_keeps_partial_grounded_result_and_requests_supplement(
    tmp_path, monkeypatch
):
    class Session:
        async def execute(self, _query):
            class Result:
                def scalars(self):
                    return self

                def all(self):
                    return []

            return Result()

        def add(self, _row):
            return None

    class Provider:
        async def analyze_keyframe(self, path):
            assert path.name == "image-01.jpg"
            return {
                "ocr_text": "配方：咖啡粉 15 克",
                "visual_description": "图片展示手冲咖啡器具",
                "quality_issue": "",
            }, ProviderUsage("vision", 2, 1, 0, quantity=3)

        async def summarize(self, content, **_kwargs):
            assert "咖啡粉 15 克" in content
            return {
                "one_sentence": "手冲咖啡配方",
                "sections": [{"kind": "content", "title": "配方", "body": "15 克"}],
                "tags": [],
                "asset_ids": [],
            }, ProviderUsage("summary", 2, 1, 0, quantity=3)

        async def embed(self, texts):
            return [[0.0] for _ in texts], ProviderUsage(
                "embedding", 1, 0, 0, quantity=1
            )

    async def fake_download(url, target, **_kwargs):
        if url.endswith("2.webp"):
            raise pipeline.DownloadError("media_expired", "图片失效")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"image")
        return target

    async def fake_runtime(_session):
        return {"summary_prompt": "", "processing_model": "model"}

    async def no_write(*_args, **_kwargs):
        return None

    monkeypatch.setattr(pipeline, "DATA_DIR", tmp_path)
    monkeypatch.setattr(pipeline, "get_secret", lambda *_args: _async_value("key"))
    monkeypatch.setattr(pipeline, "get_runtime_settings", fake_runtime)
    monkeypatch.setattr(pipeline, "DashScopeProvider", lambda _key: Provider())
    monkeypatch.setattr(pipeline, "download_media", fake_download)
    monkeypatch.setattr(
        pipeline, "summary_prompt_for_work", lambda *_args: _async_value("")
    )
    monkeypatch.setattr(pipeline, "local_asset_names", lambda _work: [])
    monkeypatch.setattr(pipeline, "_record", no_write)
    monkeypatch.setattr(pipeline, "store_summary", no_write)
    monkeypatch.setattr(pipeline, "_replace_chunks", no_write)

    old_directory = tmp_path / "media" / "denied-image-post"
    old_directory.mkdir(parents=True)
    (old_directory / "image-01.jpg").write_bytes(b"old-one")
    (old_directory / "image-02.jpg").write_bytes(b"old-two")

    work = SimpleNamespace(
        id=11,
        platform_work_id="denied-image-post",
        title="咖啡图文",
        description="作者正文",
        author_name="作者",
        kind="image",
        media_urls=[],
        image_urls=[
            "https://p1.douyinpic.com/1.webp",
            "https://p1.douyinpic.com/2.webp",
        ],
        raw_metadata={"media_policy": {"download_permission": "denied"}},
        duration_seconds=0,
        processing_state="discovered",
        process_error=None,
        process_attempts=0,
        content_text="",
    )

    state = await pipeline.process_work(Session(), work, "image-job")  # type: ignore[arg-type]

    assert state == "processed"
    assert work.evidence_state == "sufficient"
    assert work.supplement_state == "required"
    assert work.supplement_reason == "image_set_incomplete"
    assert work.track_report["images"]["expected_count"] == 2
    assert work.track_report["images"]["evidence_count"] == 1
    assert work.track_report["images"]["failed_positions"] == [2]
    assert (old_directory / "image-01.jpg").read_bytes() == b"image"
    assert not (old_directory / "image-02.jpg").exists()


async def test_insufficient_evidence_removes_old_ungrounded_summary_and_chunks(
    monkeypatch,
):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    class Provider:
        async def summarize(self, *_args, **_kwargs):
            raise AssertionError("无原始证据时不得调用总结")

        async def embed(self, *_args, **_kwargs):
            raise AssertionError("无原始证据时不得调用向量化")

    monkeypatch.setattr(pipeline, "get_secret", lambda *_args: _async_value("key"))
    monkeypatch.setattr(
        pipeline,
        "get_runtime_settings",
        lambda *_args: _async_value({"summary_prompt": "", "processing_model": "m"}),
    )
    monkeypatch.setattr(pipeline, "DashScopeProvider", lambda _key: Provider())

    async with factory() as session:
        work = Work(
            platform_work_id="old-title-only",
            kind="image",
            title="只有标题",
            description="作者正文",
            library_state="pending",
            processing_state="discovered",
        )
        session.add(work)
        await session.flush()
        session.add_all(
            [
                KnowledgeChunk(
                    work_id=work.id,
                    chunk_index=0,
                    source_kind="metadata",
                    text="只有标题",
                ),
                WorkSummary(work_id=work.id, one_sentence="无依据旧总结"),
                Keyframe(
                    work_id=work.id,
                    timestamp_seconds=0,
                    path="stale.jpg",
                ),
            ]
        )
        await session.commit()

        state = await pipeline.process_work(session, work, "cleanup-job")
        await session.commit()

        assert state == "evidence_insufficient"
        assert work.evidence_state == "insufficient"
        assert (
            await session.scalar(
                select(func.count(KnowledgeChunk.id)).where(
                    KnowledgeChunk.work_id == work.id
                )
            )
            == 0
        )
        assert (
            await session.scalar(
                select(func.count(WorkSummary.id)).where(WorkSummary.work_id == work.id)
            )
            == 0
        )
        assert (
            await session.scalar(
                select(func.count(Keyframe.id)).where(Keyframe.work_id == work.id)
            )
            == 0
        )

    await engine.dispose()


async def _async_value(value):
    return value
