"""High-recall video keyframe candidates with bounded, accurate final extraction."""

from __future__ import annotations

import asyncio
import heapq
import json
import math
import re
import shutil
from dataclasses import dataclass
from pathlib import Path


PTS_RE = re.compile(r"pts_time:([0-9.]+)")
SCORE_RE = re.compile(r"lavfi\.scene_score=([0-9.]+)")
PROBE_TIMEOUT_SECONDS = 30.0
FRAME_RENDER_TIMEOUT_SECONDS = 120.0


class KeyframeError(RuntimeError):
    pass


async def _communicate_with_timeout(process, timeout_seconds: float):
    try:
        return await asyncio.wait_for(
            process.communicate(), timeout=max(0.01, timeout_seconds)
        )
    except (asyncio.TimeoutError, asyncio.CancelledError):
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await asyncio.shield(process.wait())
        raise


@dataclass(frozen=True, slots=True)
class SceneCandidate:
    timestamp: float
    score: float
    source: str = "scene"


@dataclass(slots=True)
class KeyframeCandidate:
    path: Path
    candidate: SceneCandidate
    perceptual_hash: str
    quality_score: float
    ocr_text: str = ""
    visual_description: str = ""
    selection_score: float = 0.0
    selection_reason: str = ""


def _uniform_timestamps(duration_seconds: float, count: int) -> list[float]:
    if duration_seconds <= 0 or count <= 0:
        return []
    # Midpoints cover the opening and ending without selecting common black frames at
    # exactly 0 or the container duration.
    return [duration_seconds * (index + 0.5) / count for index in range(count)]


def build_candidate_timeline(
    scene_candidates: list[SceneCandidate],
    *,
    duration_seconds: float,
    max_candidates: int,
    min_gap_seconds: float = 0.35,
) -> list[SceneCandidate]:
    """Build a bounded pool containing both settled scene frames and full coverage."""

    if duration_seconds <= 0 or max_candidates <= 0:
        return []

    # Scene cuts are useful, but no more than half the pool may be consumed by rapid
    # edits. A short delay avoids selecting fades and transition frames themselves.
    scene_budget = max_candidates // 2
    selected_scenes: list[SceneCandidate] = []
    for item in sorted(scene_candidates, key=lambda value: value.score, reverse=True):
        settled_timestamp = min(
            max(item.timestamp + 0.4, 0.05), max(0.05, duration_seconds - 0.05)
        )
        settled = SceneCandidate(settled_timestamp, item.score, "scene")
        if all(
            abs(settled.timestamp - existing.timestamp) >= min_gap_seconds
            for existing in selected_scenes
        ):
            selected_scenes.append(settled)
        if len(selected_scenes) >= scene_budget:
            break

    chosen = list(selected_scenes)
    # Start with enough uniform points to fill every remaining slot. When one lands
    # near a scene candidate, use progressively denser midpoint grids to fill gaps.
    for multiplier in (1, 2, 4):
        for timestamp in _uniform_timestamps(
            duration_seconds, max_candidates * multiplier
        ):
            if all(
                abs(timestamp - existing.timestamp) >= min_gap_seconds
                for existing in chosen
            ):
                chosen.append(SceneCandidate(timestamp, 0.0, "uniform"))
            if len(chosen) >= max_candidates:
                break
        if len(chosen) >= max_candidates:
            break
    return sorted(chosen[:max_candidates], key=lambda item: item.timestamp)


def parse_scene_metadata(output: str, max_candidates: int) -> list[SceneCandidate]:
    heap: list[tuple[float, float]] = []
    pending_timestamp: float | None = None
    for line in output.splitlines():
        pts = PTS_RE.search(line)
        if pts:
            pending_timestamp = float(pts.group(1))
        score = SCORE_RE.search(line)
        if score and pending_timestamp is not None:
            item = (float(score.group(1)), pending_timestamp)
            if len(heap) < max_candidates:
                heapq.heappush(heap, item)
            elif item[0] > heap[0][0]:
                heapq.heapreplace(heap, item)
            pending_timestamp = None
    return [SceneCandidate(timestamp=ts, score=score) for score, ts in heap]


def parse_duration_output(output: str) -> float | None:
    try:
        payload = json.loads(output)
    except (TypeError, ValueError):
        return None
    candidates: list[object] = []
    for stream in payload.get("streams") or []:
        if isinstance(stream, dict):
            candidates.append(stream.get("duration"))
    format_payload = payload.get("format")
    if isinstance(format_payload, dict):
        candidates.append(format_payload.get("duration"))
    for value in candidates:
        try:
            duration = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(duration) and duration > 0:
            return duration
    return None


def parse_audio_stream_output(output: str) -> bool | None:
    """Return whether ffprobe found an audio stream, or None for invalid output."""

    try:
        payload = json.loads(output)
    except (TypeError, ValueError):
        return None
    streams = payload.get("streams")
    if not isinstance(streams, list):
        return None
    return any(
        isinstance(stream, dict) and stream.get("codec_type") == "audio"
        for stream in streams
    )


async def probe_video_has_audio(video_path: Path) -> bool | None:
    """Inspect the container before ASR so a genuinely silent video is not an error."""

    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    process = await asyncio.create_subprocess_exec(
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type",
        "-of",
        "json",
        str(video_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await _communicate_with_timeout(
            process, PROBE_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        return None
    if process.returncode != 0:
        return None
    return parse_audio_stream_output(stdout.decode("utf-8", "ignore"))


async def probe_video_duration(video_path: Path, fallback: float) -> float:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return fallback
    process = await asyncio.create_subprocess_exec(
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=duration:format=duration",
        "-of",
        "json",
        str(video_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await _communicate_with_timeout(
            process, PROBE_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        return fallback
    if process.returncode == 0:
        parsed = parse_duration_output(stdout.decode("utf-8", "ignore"))
        if parsed:
            return parsed
    return fallback


async def analyze_scenes(
    video_path: Path,
    *,
    threshold: float,
    max_candidates: int,
    analysis_width: int = 480,
    timeout_seconds: int = 900,
) -> list[SceneCandidate]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise KeyframeError("未找到 ffmpeg")
    filter_graph = (
        f"scale={analysis_width}:-2,select=gt(scene\\,{threshold}),metadata=print"
    )
    process = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-i",
        str(video_path),
        "-an",
        "-vf",
        filter_graph,
        "-f",
        "null",
        "-",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await _communicate_with_timeout(process, timeout_seconds)
    except asyncio.TimeoutError as exc:
        raise KeyframeError("场景分析超时") from exc
    if process.returncode != 0:
        message = stderr.decode("utf-8", "ignore")[-800:]
        raise KeyframeError(f"ffmpeg 场景分析失败: {message}")
    text = (stdout + stderr).decode("utf-8", "ignore")
    return parse_scene_metadata(text, max_candidates)


def _image_signature(path: Path) -> tuple[float, float, str]:
    try:
        from PIL import Image, ImageFilter, ImageStat
    except ImportError as exc:
        raise KeyframeError("未安装 Pillow") from exc
    with Image.open(path) as image:
        gray = image.convert("L")
        mean = float(ImageStat.Stat(gray).mean[0])
        edge_std = float(ImageStat.Stat(gray.filter(ImageFilter.FIND_EDGES)).stddev[0])
        # A 256-bit signature avoids collapsing slides that share a template but
        # contain different text, a common false duplicate in tutorial videos.
        tiny = gray.resize((16, 16))
        pixels = list(tiny.getdata())
        average = sum(pixels) / len(pixels)
        bits = "".join("1" if value >= average else "0" for value in pixels)
        return mean, edge_std, f"{int(bits, 2):064x}"


def _hamming(left: str, right: str) -> int:
    return (int(left, 16) ^ int(right, 16)).bit_count()


def _quality_score(mean: float, edge_std: float) -> float:
    exposure = max(0.0, 1.0 - abs(mean - 128.0) / 128.0)
    sharpness = min(1.0, edge_std / 32.0)
    return round(exposure * 0.35 + sharpness * 0.65, 4)


async def _render_frame(
    ffmpeg: str,
    video_path: Path,
    target: Path,
    timestamp: float,
    *,
    accurate: bool,
) -> bool:
    target.unlink(missing_ok=True)
    if accurate:
        # Seek close to the target, then decode the final two seconds precisely.
        coarse = max(0.0, timestamp - 2.0)
        fine = max(0.0, timestamp - coarse)
        seek_args = [
            "-ss",
            f"{coarse:.3f}",
            "-i",
            str(video_path),
            "-ss",
            f"{fine:.3f}",
        ]
    else:
        seek_args = ["-ss", f"{timestamp:.3f}", "-i", str(video_path)]
    process = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        *seek_args,
        "-frames:v",
        "1",
        "-q:v",
        "2",
        str(target),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        await _communicate_with_timeout(process, FRAME_RENDER_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        target.unlink(missing_ok=True)
        return False
    return process.returncode == 0 and target.is_file()


async def extract_keyframe_candidates(
    video_path: Path,
    output_dir: Path,
    *,
    duration_seconds: float,
    threshold: float = 0.4,
    max_candidates: int = 120,
    max_frames: int = 12,
    min_gap_seconds: float = 2.0,
    analysis_width: int = 480,
) -> tuple[list[KeyframeCandidate], float]:
    """Render a high-recall pool; the caller performs semantic selection later."""

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise KeyframeError("未找到 ffmpeg")
    output_dir.mkdir(parents=True, exist_ok=True)
    for stale in output_dir.glob("candidate-*.jpg"):
        stale.unlink(missing_ok=True)

    actual_duration = await probe_video_duration(video_path, duration_seconds)
    if actual_duration <= 0:
        raise KeyframeError("无法确定视频时长")
    scene_candidates = await analyze_scenes(
        video_path,
        threshold=threshold,
        max_candidates=max_candidates,
        analysis_width=analysis_width,
    )
    # Default 12 final frames produce 24 candidate analyses. Cap the expensive
    # image-model stage at 48 even when users configure a very large final set.
    pool_cap = min(max_candidates, max(12, min(48, max_frames * 2)))
    timeline = build_candidate_timeline(
        scene_candidates,
        duration_seconds=actual_duration,
        max_candidates=pool_cap,
        min_gap_seconds=max(0.25, min(0.75, min_gap_seconds / 4)),
    )

    accepted: list[KeyframeCandidate] = []
    hashes: list[str] = []
    for index, candidate in enumerate(timeline):
        target = output_dir / f"candidate-{index:03d}.jpg"
        if not await _render_frame(
            ffmpeg, video_path, target, candidate.timestamp, accurate=False
        ):
            continue
        mean, edge_std, image_hash = _image_signature(target)
        if mean < 12 or mean > 248 or edge_std < 2.5:
            target.unlink(missing_ok=True)
            continue
        # Only remove near-identical frames. Semantic diversity is handled after
        # OCR/visual analysis, where similar slide templates can be distinguished.
        if any(_hamming(image_hash, old) < 10 for old in hashes):
            target.unlink(missing_ok=True)
            continue
        hashes.append(image_hash)
        accepted.append(
            KeyframeCandidate(
                path=target,
                candidate=candidate,
                perceptual_hash=image_hash,
                quality_score=_quality_score(mean, edge_std),
            )
        )
    return accepted, actual_duration


def choose_default_keyframes(
    candidates: list[KeyframeCandidate],
    *,
    max_frames: int,
    min_gap_seconds: float,
) -> list[KeyframeCandidate]:
    """Deterministic fallback when semantic selection is unavailable."""

    if max_frames <= 0:
        return []
    ranked = sorted(
        candidates,
        key=lambda item: (
            item.candidate.score * 0.45
            + item.quality_score * 0.4
            + (0.15 if item.candidate.source == "uniform" else 0.0)
        ),
        reverse=True,
    )
    selected: list[KeyframeCandidate] = []
    for item in ranked:
        if all(
            abs(item.candidate.timestamp - old.candidate.timestamp) >= min_gap_seconds
            for old in selected
        ):
            if not item.selection_reason:
                item.selection_score = round(
                    item.candidate.score * 0.45 + item.quality_score * 0.4, 4
                )
                item.selection_reason = "画面变化、清晰度与时间覆盖综合选择"
            selected.append(item)
        if len(selected) >= max_frames:
            break
    if len(selected) < max_frames:
        for item in sorted(candidates, key=lambda value: value.candidate.timestamp):
            if item not in selected:
                item.selection_reason = "补充全时段画面覆盖"
                selected.append(item)
            if len(selected) >= max_frames:
                break
    return sorted(selected, key=lambda item: item.candidate.timestamp)


async def finalize_keyframes(
    video_path: Path,
    output_dir: Path,
    selected: list[KeyframeCandidate],
    *,
    max_frames: int,
) -> list[KeyframeCandidate]:
    """Precisely render selected frames, atomically promote them, and remove candidates."""

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise KeyframeError("未找到 ffmpeg")
    promoted: list[KeyframeCandidate] = []
    for index, item in enumerate(
        sorted(selected[:max_frames], key=lambda value: value.candidate.timestamp),
        start=1,
    ):
        temporary = output_dir / f"selected-{index:02d}.jpg"
        rendered = await _render_frame(
            ffmpeg,
            video_path,
            temporary,
            item.candidate.timestamp,
            accurate=True,
        )
        if rendered:
            mean, edge_std, image_hash = _image_signature(temporary)
            item.path = temporary
            item.perceptual_hash = image_hash
            item.quality_score = _quality_score(mean, edge_std)
        elif item.path.is_file():
            item.path.replace(temporary)
            item.path = temporary
        else:
            continue
        promoted.append(item)

    for stale in (*output_dir.glob("frame-*.jpg"), *output_dir.glob("candidate-*.jpg")):
        stale.unlink(missing_ok=True)
    for index, item in enumerate(promoted, start=1):
        final = output_dir / f"frame-{index:02d}-{item.candidate.timestamp:.2f}s.jpg"
        item.path.replace(final)
        item.path = final
    return promoted
