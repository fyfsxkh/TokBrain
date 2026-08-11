import asyncio

import app.services.keyframes as keyframes
from app.services.keyframes import (
    SceneCandidate,
    build_candidate_timeline,
    parse_duration_output,
    parse_scene_metadata,
)


def test_candidate_timeline_combines_settled_scene_frames_and_uniform_coverage():
    scenes = [
        SceneCandidate(timestamp=2.0, score=0.9),
        SceneCandidate(timestamp=9.0, score=0.8),
    ]

    chosen = build_candidate_timeline(
        scenes,
        duration_seconds=12,
        max_candidates=8,
        min_gap_seconds=0.35,
    )

    assert len(chosen) == 8
    assert {item.source for item in chosen} == {"scene", "uniform"}
    assert any(
        2.2 <= item.timestamp <= 2.6 for item in chosen if item.source == "scene"
    )
    assert chosen[0].timestamp < 1.5
    assert chosen[-1].timestamp > 10.5


def test_duration_parser_prefers_video_stream_and_rejects_invalid_values():
    assert (
        parse_duration_output(
            '{"streams":[{"duration":"9.75"}],"format":{"duration":"10.0"}}'
        )
        == 9.75
    )
    assert parse_duration_output('{"streams":[{}],"format":{"duration":"8.5"}}') == 8.5
    assert parse_duration_output('{"streams":[{"duration":"N/A"}]}') is None


def test_metadata_parser_has_hard_candidate_cap():
    output = "\n".join(
        f"frame:{index} pts:0 pts_time:{index}.0\nlavfi.scene_score=0.{index:02d}"
        for index in range(1, 20)
    )
    parsed = parse_scene_metadata(output, max_candidates=5)
    assert len(parsed) == 5
    assert min(item.score for item in parsed) >= 0.15


async def test_ffprobe_timeout_kills_process_and_uses_fallback(
    tmp_path, monkeypatch
):
    class Process:
        returncode = None

        def __init__(self):
            self.killed = False

        async def communicate(self):
            await asyncio.Event().wait()

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            return self.returncode

    process = Process()

    async def spawn(*_args, **_kwargs):
        return process

    monkeypatch.setattr(keyframes.shutil, "which", lambda _name: "ffprobe")
    monkeypatch.setattr(keyframes.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(keyframes, "PROBE_TIMEOUT_SECONDS", 0.01)

    result = await keyframes.probe_video_duration(tmp_path / "video.mp4", 7.5)

    assert result == 7.5
    assert process.killed is True


async def test_subprocess_is_killed_when_worker_is_cancelled():
    started = asyncio.Event()

    class Process:
        def __init__(self):
            self.killed = False

        async def communicate(self):
            started.set()
            await asyncio.Event().wait()

        def kill(self):
            self.killed = True

        async def wait(self):
            return -9

    process = Process()
    task = asyncio.create_task(keyframes._communicate_with_timeout(process, 60))
    await started.wait()
    task.cancel()

    try:
        await task
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("cancelled subprocess wait should propagate cancellation")
    assert process.killed is True
