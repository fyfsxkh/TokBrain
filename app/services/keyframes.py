"""Content-change keyframe extraction with hard candidate and output caps."""

from __future__ import annotations

import asyncio
import heapq
import math
import re
import shutil
from dataclasses import dataclass
from pathlib import Path


PTS_RE = re.compile(r"pts_time:([0-9.]+)")
SCORE_RE = re.compile(r"lavfi\.scene_score=([0-9.]+)")


class KeyframeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SceneCandidate:
    timestamp: float
    score: float


def retain_top_candidates(
    candidates: list[SceneCandidate], max_candidates: int
) -> list[SceneCandidate]:
    if max_candidates <= 0:
        return []
    return heapq.nlargest(max_candidates, candidates, key=lambda item: item.score)


def choose_timestamps(
    candidates: list[SceneCandidate],
    *,
    duration_seconds: float,
    max_frames: int,
    min_gap_seconds: float,
) -> list[SceneCandidate]:
    """Select high-score, time-distributed candidates and add uniform fallback."""
    chosen: list[SceneCandidate] = []
    for candidate in sorted(candidates, key=lambda item: item.score, reverse=True):
        if candidate.timestamp < 0 or candidate.timestamp > max(duration_seconds, 0):
            continue
        if all(abs(candidate.timestamp - item.timestamp) >= min_gap_seconds for item in chosen):
            chosen.append(candidate)
        if len(chosen) >= max_frames:
            break

    minimum = min(max_frames, 3 if duration_seconds >= 3 else 1)
    if len(chosen) < minimum and duration_seconds > 0:
        fallback_count = max(minimum, min(max_frames, 3))
        for index in range(fallback_count):
            timestamp = duration_seconds * (index + 1) / (fallback_count + 1)
            candidate = SceneCandidate(timestamp=timestamp, score=0.0)
            if all(abs(timestamp - item.timestamp) >= min_gap_seconds / 2 for item in chosen):
                chosen.append(candidate)
            if len(chosen) >= minimum:
                break
    return sorted(chosen[:max_frames], key=lambda item: item.timestamp)


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
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
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
        tiny = gray.resize((8, 8))
        pixels = list(tiny.getdata())
        average = sum(pixels) / len(pixels)
        bits = "".join("1" if value >= average else "0" for value in pixels)
        return mean, edge_std, f"{int(bits, 2):016x}"


def _hamming(left: str, right: str) -> int:
    return (int(left, 16) ^ int(right, 16)).bit_count()


async def extract_keyframes(
    video_path: Path,
    output_dir: Path,
    *,
    duration_seconds: float,
    threshold: float = 0.4,
    max_candidates: int = 120,
    max_frames: int = 12,
    min_gap_seconds: float = 2.0,
    analysis_width: int = 480,
) -> list[tuple[Path, SceneCandidate, str]]:
    """Analyze first, then render at most 2× output cap; never dump every scene frame."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise KeyframeError("未找到 ffmpeg")
    output_dir.mkdir(parents=True, exist_ok=True)
    candidates = await analyze_scenes(
        video_path,
        threshold=threshold,
        max_candidates=max_candidates,
        analysis_width=analysis_width,
    )
    timestamps = choose_timestamps(
        candidates,
        duration_seconds=duration_seconds,
        max_frames=min(max_frames * 2, max_candidates),
        min_gap_seconds=min_gap_seconds,
    )
    accepted: list[tuple[Path, SceneCandidate, str]] = []
    hashes: list[str] = []
    for index, candidate in enumerate(timestamps):
        target = output_dir / f"candidate-{index:03d}.jpg"
        process = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-ss",
            f"{candidate.timestamp:.3f}",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(target),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0 or not target.exists():
            continue
        mean, edge_std, image_hash = _image_signature(target)
        if mean < 18 or mean > 245 or edge_std < 4 or any(_hamming(image_hash, old) < 8 for old in hashes):
            target.unlink(missing_ok=True)
            continue
        final = output_dir / f"frame-{len(accepted) + 1:02d}-{candidate.timestamp:.2f}s.jpg"
        target.replace(final)
        hashes.append(image_hash)
        accepted.append((final, candidate, image_hash))
        if len(accepted) >= max_frames:
            break
    return accepted
