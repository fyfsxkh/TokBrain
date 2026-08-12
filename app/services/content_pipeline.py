"""Resumable work processing pipeline for video and image posts."""

from __future__ import annotations

import asyncio
import json
import math
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import DATA_DIR, settings
from app.models import Keyframe, KnowledgeChunk, Work, WorkSourceAsset, WorkSummary
from app.services.budget import record_usage
from app.services.collection_prompts import summary_prompt_for_work
from app.services.downloader import DownloadError, download_media, download_subtitle
from app.services.f2_links import PublicLinkError
from app.services.keyframes import (
    KeyframeCandidate,
    choose_default_keyframes,
    extract_keyframe_candidates,
    finalize_keyframes,
    probe_video_has_audio,
    probe_video_duration,
)
from app.services.pricing import PRICE_VERSION
from app.services.providers import (
    DashScopeProvider,
    ProviderUsage,
    TranscriptResult,
)
from app.services.runtime_settings import get_runtime_settings
from app.services.secrets import get_secret
from app.services.summaries import local_asset_names, store_summary, summary_markdown
from app.services.temp_files import unlink_with_retries


def chunk_text(text: str, size: int = 1200, overlap: int = 160) -> list[str]:
    normalized = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    if not normalized:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(normalized):
        chunks.append(normalized[start : start + size])
        if start + size >= len(normalized):
            break
        start += size - overlap
    return chunks


EVIDENCE_TEXT_KINDS = {"subtitle", "transcript"}
VISUAL_EVIDENCE_KINDS = {"ocr", "visual"}
EMPTY_EVIDENCE_VALUES = {
    "",
    "无",
    "无内容",
    "无文字",
    "无法识别",
    "未知",
    "none",
    "null",
    "n/a",
}
IMAGE_LIMIT = 12
IMAGE_MAX_BYTES = 30 * 1024 * 1024
IMAGE_TOTAL_MAX_BYTES = 180 * 1024 * 1024


def evidence_text_threshold(duration_seconds: float | int | None) -> int:
    """Minimum grounded subtitle/ASR text required by the product contract."""

    try:
        duration = float(duration_seconds or 0)
    except (TypeError, ValueError):
        duration = 0
    if not math.isfinite(duration) or duration <= 0:
        return 40
    return math.ceil(max(20.0, min(120.0, duration * 0.2)))


def _evidence_char_count(text: str) -> int:
    return len(re.sub(r"[^\w\u4e00-\u9fff]+", "", text, flags=re.UNICODE))


def normalize_evidence_text(
    text: str,
    *,
    metadata_values: tuple[str, ...] = (),
) -> str:
    """Remove subtitle plumbing, duplicate lines and metadata-only repetitions."""

    excluded: set[str] = set()
    for value in metadata_values:
        compact = re.sub(r"\s+", "", str(value or "")).strip().lower()
        if compact:
            excluded.add(compact)
        excluded.update(
            item.lower()
            for item in re.findall(r"#[\w\u4e00-\u9fff]+", str(value or ""))
        )
    seen: set[str] = set()
    result: list[str] = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or line.upper() == "WEBVTT" or line.isdigit() or "-->" in line:
            continue
        line = re.sub(r"<[^>]+>", "", line)
        line = re.sub(
            r"^\s*[\[(]?\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?[\])]?\s*[-:：]?\s*",
            "",
            line,
        ).strip()
        compact = re.sub(r"\s+", "", line).lower()
        if not compact or compact in seen or compact in excluded:
            continue
        if compact in EMPTY_EVIDENCE_VALUES:
            continue
        seen.add(compact)
        result.append(line)
    return "\n".join(result)


def normalize_evidence_rows(
    rows: list[tuple[str, str, float | None]],
    *,
    metadata_values: tuple[str, ...] = (),
) -> list[tuple[str, str, float | None]]:
    """Deduplicate evidence across timed segments while keeping first timestamps."""

    seen: set[str] = set()
    result: list[tuple[str, str, float | None]] = []
    for kind, text, timestamp in rows:
        kept: list[str] = []
        for line in normalize_evidence_text(
            text, metadata_values=metadata_values
        ).splitlines():
            compact = re.sub(r"\s+", "", line).lower()
            if not compact or compact in seen:
                continue
            seen.add(compact)
            kept.append(line)
        if kept:
            result.append((kind, "\n".join(kept), timestamp))
    return result


def _has_visual_evidence(text: str) -> bool:
    compact = re.sub(r"\s+", "", str(text or "")).strip().lower()
    return bool(compact and compact not in EMPTY_EVIDENCE_VALUES)


def _set_evidence_result(
    work: Work,
    *,
    evidence_state: str,
    supplement_state: str,
    supplement_reason: str | None,
    track_report: dict,
) -> None:
    # These columns were introduced with the evidence migration. setattr keeps
    # lightweight test doubles compatible without weakening the persisted model.
    setattr(work, "evidence_state", evidence_state)
    setattr(work, "supplement_state", supplement_state)
    setattr(work, "supplement_reason", supplement_reason)
    setattr(work, "track_report", track_report)


async def _download_first(
    urls: list[str],
    target: Path,
    *,
    max_bytes: int,
) -> Path:
    """Try fresh media candidates in order without exposing signed URLs in errors."""

    last_error: DownloadError | None = None
    for url in dict.fromkeys(value for value in urls if value):
        try:
            return await download_media(
                url,
                target,
                max_bytes=max_bytes,
            )
        except DownloadError as exc:
            await asyncio.to_thread(unlink_with_retries, target)
            last_error = exc
    if last_error:
        raise last_error
    raise DownloadError(
        "media_missing",
        "链接解析服务未返回可用的媒体地址，请上传本地文件后继续",
    )


@dataclass(slots=True)
class _DirectoryPromotion:
    destination: Path
    backup: Path | None

    def finalize(self) -> None:
        """Discard the old generation after the related database commit succeeds."""

        if self.backup is not None:
            shutil.rmtree(self.backup, ignore_errors=True)

    def rollback(self) -> None:
        """Restore the old generation after the related database transaction fails."""

        if self.destination.exists():
            shutil.rmtree(self.destination)
        if self.backup is not None and self.backup.exists():
            self.backup.replace(self.destination)


_DIRECTORY_PROMOTIONS_KEY = "tokbrain_directory_promotions"


def _promotion_queue(session: AsyncSession) -> list[_DirectoryPromotion]:
    info = getattr(session, "info", None)
    if isinstance(info, dict):
        return info.setdefault(_DIRECTORY_PROMOTIONS_KEY, [])
    queue = getattr(session, "_tokbrain_directory_promotions", None)
    if queue is None:
        queue = []
        setattr(session, "_tokbrain_directory_promotions", queue)
    return queue


def _take_promotions(session: AsyncSession) -> list[_DirectoryPromotion]:
    info = getattr(session, "info", None)
    if isinstance(info, dict):
        return list(info.pop(_DIRECTORY_PROMOTIONS_KEY, []))
    queue = list(getattr(session, "_tokbrain_directory_promotions", []))
    setattr(session, "_tokbrain_directory_promotions", [])
    return queue


def _promote_directory(staging: Path, destination: Path) -> _DirectoryPromotion:
    """Swap directory generations while retaining a transaction rollback copy."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    backup = destination.with_name(f".{destination.name}-{uuid.uuid4().hex}.old")
    had_previous = destination.exists()
    if had_previous:
        destination.replace(backup)
    try:
        staging.replace(destination)
    except Exception:
        if had_previous and backup.exists() and not destination.exists():
            backup.replace(destination)
        raise
    return _DirectoryPromotion(destination, backup if had_previous else None)


async def finalize_file_promotions(session: AsyncSession) -> None:
    """Finalize filesystem swaps whose database transaction has committed."""

    for promotion in _take_promotions(session):
        await asyncio.to_thread(promotion.finalize)


async def rollback_file_promotions(session: AsyncSession) -> None:
    """Undo filesystem swaps whose database transaction has rolled back."""

    errors: list[Exception] = []
    for promotion in reversed(_take_promotions(session)):
        try:
            await asyncio.to_thread(promotion.rollback)
        except Exception as exc:  # pragma: no cover - exceptional filesystem damage
            errors.append(exc)
    if errors:
        raise RuntimeError("无法回滚媒体文件代际") from errors[0]


async def _queue_directory_promotion(
    session: AsyncSession, staging: Path, destination: Path
) -> None:
    promotion = await asyncio.to_thread(_promote_directory, staging, destination)
    _promotion_queue(session).append(promotion)


async def _record(
    session: AsyncSession, usage: ProviderUsage, job_id: str, work_id: int
) -> None:
    await record_usage(
        session,
        model=usage.model,
        metric=usage.metric,
        quantity=usage.quantity,
        unit=usage.unit,
        estimated_cost_cny=usage.cost_cny,
        job_id=job_id,
        work_id=work_id,
        metadata={
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
        },
        price_version=PRICE_VERSION,
    )


async def _analyze_image_inputs(
    provider: DashScopeProvider,
    inputs: list[tuple[Path, float | None]],
    *,
    concurrency: int,
) -> list[dict]:
    """Analyze image-post pages independently and preserve their source order."""

    semaphore = asyncio.Semaphore(max(1, concurrency))
    analyze = getattr(provider, "analyze_keyframe", None)

    async def run_one(path: Path, position: float | None) -> dict:
        try:
            async with semaphore:
                if callable(analyze):
                    payload, usage = await analyze(path)
                    ocr_text = str(payload.get("ocr_text") or "")
                    visual = str(payload.get("visual_description") or "")
                    quality_issue = str(payload.get("quality_issue") or "")
                else:
                    ocr_text, usage = await provider.ocr_image(path)
                    visual = ""
                    quality_issue = ""
            return {
                "path": path,
                "position": position,
                "ocr_text": ocr_text,
                "visual_description": visual,
                "quality_issue": quality_issue,
                "usage": usage,
                "error": None,
            }
        except Exception as exc:
            return {
                "path": path,
                "position": position,
                "ocr_text": "",
                "visual_description": "",
                "quality_issue": "",
                "usage": None,
                "error": exc,
            }

    return await asyncio.gather(*(run_one(path, position) for path, position in inputs))


async def _transcribe_detailed(
    provider: DashScopeProvider,
    media_path: Path,
    duration_seconds: float,
) -> tuple[TranscriptResult, ProviderUsage]:
    """Use timed ASR when supported while keeping simple providers compatible."""

    detailed = getattr(provider, "transcribe_detailed", None)
    if callable(detailed):
        return await detailed(media_path, duration_seconds)
    text, usage = await provider.transcribe(media_path, duration_seconds)
    return TranscriptResult(text=text), usage


async def _analyze_keyframe_candidates(
    provider: DashScopeProvider,
    candidates: list[KeyframeCandidate],
    *,
    concurrency: int,
) -> tuple[list[ProviderUsage], list[Exception]]:
    """Analyze candidate visuals concurrently without making one bad frame fatal."""

    semaphore = asyncio.Semaphore(max(1, concurrency))
    analyze = getattr(provider, "analyze_keyframe", None)

    async def run_one(
        item: KeyframeCandidate,
    ) -> tuple[ProviderUsage | None, Exception | None]:
        try:
            async with semaphore:
                if callable(analyze):
                    result, usage = await analyze(item.path)
                    item.ocr_text = str(result.get("ocr_text") or "")
                    item.visual_description = str(
                        result.get("visual_description") or ""
                    )
                else:
                    text, usage = await provider.ocr_image(item.path)
                    item.ocr_text = text
            return usage, None
        except Exception as exc:
            return None, exc

    results = await asyncio.gather(*(run_one(item) for item in candidates))
    return (
        [usage for usage, _ in results if usage is not None],
        [error for _, error in results if error is not None],
    )


async def _plan_visual_requirements(
    provider: DashScopeProvider,
    transcript: TranscriptResult,
    *,
    duration_seconds: float,
    model: str,
) -> tuple[list[dict], ProviderUsage | None]:
    planner = getattr(provider, "plan_visual_requirements", None)
    if not transcript.text or not callable(planner):
        return [], None
    try:
        return await planner(
            transcript,
            duration_seconds=duration_seconds,
            model=model,
        )
    except Exception:
        return [], None


def _candidate_payload(candidates: list[KeyframeCandidate]) -> list[dict]:
    return [
        {
            "id": f"candidate-{index:03d}",
            "timestamp_seconds": round(item.candidate.timestamp, 3),
            "source": item.candidate.source,
            "scene_score": round(item.candidate.score, 4),
            "quality_score": item.quality_score,
            "ocr_text": item.ocr_text[:4_000],
            "visual_description": item.visual_description[:1_000],
        }
        for index, item in enumerate(candidates)
    ]


async def _select_keyframe_candidates(
    provider: DashScopeProvider,
    candidates: list[KeyframeCandidate],
    requirements: list[dict],
    *,
    max_frames: int,
    min_gap_seconds: float,
    model: str,
) -> tuple[list[KeyframeCandidate], ProviderUsage | None]:
    """Blend semantic picks with a reserved deterministic coverage quota."""

    if not candidates or max_frames <= 0:
        return [], None
    selector = getattr(provider, "select_keyframes", None)
    semantic_rows: list[dict] = []
    usage: ProviderUsage | None = None
    if callable(selector):
        reserve = min(max_frames - 1, max(1, max_frames // 4)) if max_frames > 1 else 0
        semantic_cap = max(1, max_frames - reserve)
        try:
            semantic_rows, usage = await selector(
                requirements,
                _candidate_payload(candidates),
                max_frames=semantic_cap,
                model=model,
            )
        except Exception:
            semantic_rows = []
            usage = None

    by_id = {f"candidate-{index:03d}": item for index, item in enumerate(candidates)}
    selected: list[KeyframeCandidate] = []
    for row in semantic_rows:
        item = by_id.get(str(row.get("id") or ""))
        if not item or item in selected:
            continue
        if any(
            abs(item.candidate.timestamp - old.candidate.timestamp)
            < min_gap_seconds / 2
            for old in selected
        ):
            continue
        item.selection_score = float(row.get("score") or 0)
        item.selection_reason = str(row.get("reason") or "与音频视觉需求匹配")
        selected.append(item)

    fallback = choose_default_keyframes(
        candidates,
        max_frames=max_frames,
        min_gap_seconds=min_gap_seconds,
    )
    for item in fallback:
        if item not in selected and all(
            abs(item.candidate.timestamp - old.candidate.timestamp)
            >= min_gap_seconds / 2
            for old in selected
        ):
            if not item.selection_reason:
                item.selection_reason = "补充全时段视觉覆盖"
            selected.append(item)
        if len(selected) >= max_frames:
            break
    if len(selected) < max_frames:
        for item in sorted(candidates, key=lambda value: value.candidate.timestamp):
            if item not in selected:
                item.selection_reason = item.selection_reason or "补充全时段视觉覆盖"
                selected.append(item)
            if len(selected) >= max_frames:
                break
    return (
        sorted(selected[:max_frames], key=lambda item: item.candidate.timestamp),
        usage,
    )


def _media_policy(work: Work) -> dict:
    metadata = getattr(work, "raw_metadata", None)
    if not isinstance(metadata, dict):
        return {}
    policy = metadata.get("media_policy")
    return dict(policy) if isinstance(policy, dict) else {}


def _policy_strings(policy: dict, key: str) -> list[str]:
    value = policy.get(key)
    if not isinstance(value, list):
        return []
    return list(
        dict.fromkeys(
            item.strip() for item in value if isinstance(item, str) and item.strip()
        )
    )


def _subtitle_file_text(path: Path) -> str:
    raw = path.read_text(encoding="utf-8-sig", errors="replace").strip()
    if not raw:
        return ""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = None
    if payload is not None:
        texts: list[str] = []

        def visit(value: object) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    if (
                        str(key).lower() in {"text", "content", "subtitle_text"}
                        and isinstance(child, str)
                        and child.strip()
                    ):
                        texts.append(child.strip())
                    elif isinstance(child, (dict, list)):
                        visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(payload)
        return "\n".join(dict.fromkeys(texts))

    lines: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if (
            not stripped
            or stripped.upper() == "WEBVTT"
            or stripped.isdigit()
            or "-->" in stripped
            or stripped.startswith(("NOTE ", "STYLE", "REGION"))
        ):
            continue
        cleaned = re.sub(r"<[^>]+>", "", stripped).strip()
        if cleaned:
            lines.append(cleaned)
    return "\n".join(lines)


async def _restricted_text_or_audio(
    provider: DashScopeProvider,
    work: Work,
    policy: dict,
) -> tuple[str, str, ProviderUsage | None]:
    """Prefer subtitles, then audio-only ASR, and never fetch the full video."""

    inline_subtitles = _policy_strings(policy, "subtitle_texts")
    if inline_subtitles:
        return "subtitle", "\n".join(inline_subtitles), None

    subtitle_path = DATA_DIR / "tmp" / f"{work.platform_work_id}.subtitle"
    for url in _policy_strings(policy, "subtitle_urls"):
        try:
            await download_subtitle(url, subtitle_path)
            text = await asyncio.to_thread(_subtitle_file_text, subtitle_path)
            if text:
                return "subtitle", text, None
        except PublicLinkError:
            raise
        except DownloadError:
            # Subtitle endpoints are optional and short-lived; continue with
            # the next permitted source when one is unavailable.
            pass
        finally:
            await asyncio.to_thread(unlink_with_retries, subtitle_path)

    audio_urls = _policy_strings(policy, "audio_urls")
    if not audio_urls:
        return "metadata", "", None
    if not shutil.which("ffmpeg"):
        raise RuntimeError("未找到 ffmpeg")
    audio_path = DATA_DIR / "tmp" / f"{work.platform_work_id}.restricted-audio"
    try:
        try:
            await _download_first(
                audio_urls,
                audio_path,
                max_bytes=settings.max_download_megabytes * 1024 * 1024,
            )
        except DownloadError:
            return "metadata", "", None
        text, usage = await provider.transcribe(audio_path, work.duration_seconds)
        return ("transcript", text, usage) if text else ("metadata", "", usage)
    except PublicLinkError:
        raise
    finally:
        await asyncio.to_thread(unlink_with_retries, audio_path)


async def process_work(
    session: AsyncSession,
    work: Work,
    job_id: str,
) -> str:
    """Process one work without allowing metadata to masquerade as evidence."""
    api_key = await get_secret(session, "dashscope_api_key")
    metadata_text = "\n".join(
        value
        for value in (
            work.title,
            work.description,
            f"作者：{work.author_name}" if work.author_name else "",
        )
        if value
    )
    if not api_key:
        if getattr(work, "evidence_state", "unverified") != "sufficient":
            setattr(work, "evidence_state", "unverified")
        work.processing_state = "waiting_for_key"
        return "waiting_for_key"

    provider = DashScopeProvider(api_key)
    runtime = await get_runtime_settings(session)
    material: list[tuple[str, str, float | None]] = [("metadata", metadata_text, None)]
    pending_usages: list[ProviderUsage] = []
    temp_video: Path | None = None
    downloaded_temp_video = False
    video_frames = None
    remote_image_stage: Path | None = None
    remote_image_asset_names: list[str] | None = None
    keyframe_stage: Path | None = None
    actual_video_duration: float | None = None
    previous_evidence_state = getattr(work, "evidence_state", "unverified")
    retained_dir = DATA_DIR / "media" / work.platform_work_id
    keyframe_dir = DATA_DIR / "keyframes" / work.platform_work_id
    track_report: dict = {
        "kind": work.kind,
        "audio": {"status": "not_applicable"},
        "subtitle": {"status": "not_applicable"},
        "visual": {"status": "not_applicable"},
        "images": {"status": "not_applicable"},
    }
    source_assets = (
        (
            await session.execute(
                select(WorkSourceAsset)
                .where(WorkSourceAsset.work_id == work.id)
                .order_by(WorkSourceAsset.position, WorkSourceAsset.id)
            )
        )
        .scalars()
        .all()
    )
    local_video = next(
        (
            Path(asset.path)
            for asset in source_assets
            if asset.kind == "video" and Path(asset.path).is_file()
        ),
        None,
    )
    local_images = [
        Path(asset.path)
        for asset in source_assets
        if asset.kind == "image" and Path(asset.path).is_file()
    ]
    media_policy = _media_policy(work)
    remote_full_media_allowed = media_policy.get("download_permission") == "allowed"
    full_video_available = bool(
        local_video or (remote_full_media_allowed and work.media_urls)
    )

    waiting_for_runtime = (
        work.kind == "video" and full_video_available and not shutil.which("ffmpeg")
    )
    if waiting_for_runtime:
        work.processing_state = "waiting_for_ffmpeg"
        return "waiting_for_ffmpeg"

    image_complete = False
    image_has_any_source = False
    full_video_processed = False
    try:
        if work.kind == "video" and full_video_available:
            keyframe_stage = (
                DATA_DIR / "tmp" / f"keyframes-{work.id}-{uuid.uuid4().hex}"
            )
            keyframe_stage.mkdir(parents=True, exist_ok=False)
            if local_video:
                temp_video = local_video
                video_source = "local"
            else:
                temp_video = DATA_DIR / "tmp" / f"{work.platform_work_id}.mp4"
                await _download_first(
                    list(work.media_urls or []),
                    temp_video,
                    max_bytes=settings.max_download_megabytes * 1024 * 1024,
                )
                downloaded_temp_video = True
                video_source = "remote"

            actual_duration = await probe_video_duration(
                temp_video, work.duration_seconds
            )
            if actual_duration > settings.max_work_duration_seconds:
                raise RuntimeError("视频实际时长超过单作品安全上限")
            if actual_duration > 0:
                actual_video_duration = actual_duration
            has_audio_stream = await probe_video_has_audio(temp_video)
            track_report["video"] = {
                "status": "processed",
                "source": video_source,
                "duration_seconds": round(float(actual_duration or 0), 3),
            }

            async def extract_visual_candidates():
                return await extract_keyframe_candidates(
                    temp_video,
                    keyframe_stage,
                    duration_seconds=actual_duration,
                    threshold=float(runtime["scene_threshold"]),
                    max_candidates=int(runtime["max_scene_candidates"]),
                    max_frames=int(runtime["max_keyframes"]),
                    min_gap_seconds=float(runtime["min_keyframe_gap_seconds"]),
                    analysis_width=settings.keyframe_analysis_width,
                )

            # Build a high-recall visual pool while ASR is running. A container
            # explicitly reported as silent skips ASR instead of failing ingestion.
            if has_audio_stream is False:
                branch_results = [
                    (TranscriptResult(text=""), None),
                    await extract_visual_candidates(),
                ]
            else:
                branch_results = await asyncio.gather(
                    _transcribe_detailed(provider, temp_video, actual_duration),
                    extract_visual_candidates(),
                    return_exceptions=True,
                )
            # A thread-backed transcription cannot be stopped safely midway through
            # Windows file I/O. Wait for both branches, then surface either error.
            for result in branch_results:
                if isinstance(result, Exception):
                    raise result
            (transcript, transcript_usage), (visual_candidates, actual_duration) = (
                branch_results
            )
            if transcript_usage:
                pending_usages.append(transcript_usage)
            if transcript.segments:
                material.extend(
                    ("transcript", segment.text, segment.start_seconds)
                    for segment in transcript.segments
                )
            elif transcript.text:
                material.append(("transcript", transcript.text, 0.0))
            track_report["audio"] = {
                "status": (
                    "no_stream"
                    if has_audio_stream is False
                    else ("processed" if transcript.text else "empty")
                ),
                "stream_present": has_audio_stream,
            }

            processing_model = str(
                runtime.get("processing_model") or settings.enrichment_model
            )
            (analysis_usages, analysis_errors), (
                visual_requirements,
                planning_usage,
            ) = await asyncio.gather(
                _analyze_keyframe_candidates(
                    provider,
                    visual_candidates,
                    concurrency=settings.ocr_concurrency,
                ),
                _plan_visual_requirements(
                    provider,
                    transcript,
                    duration_seconds=actual_duration,
                    model=processing_model,
                ),
            )
            pending_usages.extend(analysis_usages)
            if visual_candidates and len(analysis_errors) == len(visual_candidates):
                raise analysis_errors[0]
            if planning_usage:
                pending_usages.append(planning_usage)

            selected_candidates, selection_usage = await _select_keyframe_candidates(
                provider,
                visual_candidates,
                visual_requirements,
                max_frames=int(runtime["max_keyframes"]),
                min_gap_seconds=float(runtime["min_keyframe_gap_seconds"]),
                model=processing_model,
            )
            if selection_usage:
                pending_usages.append(selection_usage)
            video_frames = await finalize_keyframes(
                temp_video,
                keyframe_stage,
                selected_candidates,
                max_frames=int(runtime["max_keyframes"]),
            )
            for item in video_frames:
                item.path = keyframe_dir / item.path.name
            for item in video_frames:
                timestamp = item.candidate.timestamp
                if item.ocr_text:
                    material.append(("ocr", item.ocr_text, timestamp))
                if item.visual_description:
                    material.append(("visual", item.visual_description, timestamp))
            visual_evidence_frames = sum(
                1
                for item in video_frames
                if _has_visual_evidence(item.ocr_text)
                or _has_visual_evidence(item.visual_description)
            )
            track_report["visual"] = {
                "status": "processed",
                "candidate_count": len(visual_candidates),
                "selected_count": len(video_frames),
                "evidence_count": visual_evidence_frames,
                "failed_count": len(analysis_errors),
            }
            full_video_processed = True
        elif work.kind == "video" and not local_video:
            source_kind, text, usage = await _restricted_text_or_audio(
                provider, work, media_policy
            )
            if usage:
                pending_usages.append(usage)
            if text:
                material.append((source_kind, text, 0.0))
            track_report["video"] = {
                "status": "unavailable",
                "source": "none",
            }
            if source_kind == "subtitle":
                track_report["subtitle"] = {"status": "processed"}
                track_report["audio"] = {"status": "not_used"}
            elif source_kind == "transcript":
                track_report["subtitle"] = {"status": "unavailable"}
                track_report["audio"] = {"status": "processed"}
            else:
                track_report["subtitle"] = {"status": "unavailable"}
                track_report["audio"] = {"status": "unavailable"}
            track_report["visual"] = {"status": "unavailable"}
        elif work.kind == "image":
            image_inputs: list[tuple[Path, float | None]] = []
            image_sources: list[Path | str] = (
                local_images if local_images else list(work.image_urls or [])
            )
            if not local_images:
                remote_image_stage = (
                    DATA_DIR
                    / "tmp"
                    / f"remote-images-{work.id}-{uuid.uuid4().hex}"
                )
                remote_image_stage.mkdir(parents=True, exist_ok=False)
            image_sources = image_sources[:IMAGE_LIMIT]
            image_has_any_source = bool(image_sources)
            expected_count = len(image_sources)
            downloaded_positions: list[int] = []
            failed_positions: list[int] = []
            total_image_bytes = 0
            for index, source in enumerate(image_sources):
                if isinstance(source, Path):
                    image_path = source
                    image_size = image_path.stat().st_size
                    if (
                        image_size > IMAGE_MAX_BYTES
                        or total_image_bytes + image_size > IMAGE_TOTAL_MAX_BYTES
                    ):
                        failed_positions.append(index + 1)
                        continue
                else:
                    image_path = remote_image_stage / f"image-{index + 1:02d}.jpg"
                    try:
                        await download_media(
                            source,
                            image_path,
                            max_bytes=IMAGE_MAX_BYTES,
                        )
                        image_size = image_path.stat().st_size
                        if total_image_bytes + image_size > IMAGE_TOTAL_MAX_BYTES:
                            await asyncio.to_thread(unlink_with_retries, image_path)
                            failed_positions.append(index + 1)
                            continue
                    except PublicLinkError:
                        raise
                    except DownloadError:
                        await asyncio.to_thread(unlink_with_retries, image_path)
                        failed_positions.append(index + 1)
                        continue
                total_image_bytes += image_size
                downloaded_positions.append(index + 1)
                image_inputs.append((image_path, float(index)))
            if remote_image_stage is not None:
                remote_image_asset_names = [path.name for path, _ in image_inputs]

            image_results = await _analyze_image_inputs(
                provider,
                image_inputs,
                concurrency=settings.ocr_concurrency,
            )
            analysis_errors = [row["error"] for row in image_results if row["error"]]
            if image_results and len(analysis_errors) == len(image_results):
                raise analysis_errors[0]
            evidence_positions: list[int] = []
            quality_issue_positions: list[int] = []
            for row in image_results:
                usage = row["usage"]
                if usage:
                    pending_usages.append(usage)
                position = int(float(row["position"] or 0)) + 1
                ocr_text = str(row["ocr_text"] or "")
                visual_text = str(row["visual_description"] or "")
                if ocr_text:
                    material.append(("ocr", ocr_text, row["position"]))
                if visual_text:
                    material.append(("visual", visual_text, row["position"]))
                if _has_visual_evidence(ocr_text) or _has_visual_evidence(visual_text):
                    evidence_positions.append(position)
                else:
                    failed_positions.append(position)
                if _has_visual_evidence(str(row["quality_issue"] or "")):
                    quality_issue_positions.append(position)
                    failed_positions.append(position)
                if row["error"]:
                    failed_positions.append(position)
            failed_positions = sorted(set(failed_positions))
            image_complete = bool(
                expected_count
                and len(set(evidence_positions)) == expected_count
                and not failed_positions
            )
            track_report["images"] = {
                "status": "processed" if image_results else "unavailable",
                "expected_count": expected_count,
                "downloaded_count": len(downloaded_positions),
                "analyzed_count": len(image_results) - len(analysis_errors),
                "evidence_count": len(set(evidence_positions)),
                "failed_positions": failed_positions,
                "quality_issue_positions": sorted(set(quality_issue_positions)),
                "total_bytes": total_image_bytes,
            }

        metadata_values = (str(work.title or ""), str(work.description or ""))
        normalized_text_rows = normalize_evidence_rows(
            [
                (kind, text, timestamp)
                for kind, text, timestamp in material
                if kind in EVIDENCE_TEXT_KINDS
            ],
            metadata_values=metadata_values,
        )
        combined_evidence_text = "\n".join(text for _, text, _ in normalized_text_rows)
        text_char_count = _evidence_char_count(combined_evidence_text)
        threshold = evidence_text_threshold(
            actual_video_duration
            if actual_video_duration is not None
            else work.duration_seconds
        )
        text_is_valid = text_char_count >= threshold
        visual_rows = [
            (kind, text.strip(), timestamp)
            for kind, text, timestamp in material
            if kind in VISUAL_EVIDENCE_KINDS and _has_visual_evidence(text)
        ]
        visual_is_valid = bool(visual_rows)
        if work.kind == "image":
            evidence_is_sufficient = visual_is_valid
        else:
            evidence_is_sufficient = text_is_valid or visual_is_valid

        for key in ("audio", "subtitle"):
            if track_report[key].get("status") in {"processed", "empty"}:
                track_report[key].update(
                    {
                        "effective_char_count": text_char_count,
                        "threshold": threshold,
                        "evidence_valid": text_is_valid,
                    }
                )
        track_report["evidence"] = {
            "text_char_count": text_char_count,
            "text_threshold": threshold,
            "text_valid": text_is_valid,
            "visual_valid": visual_is_valid,
        }

        if evidence_is_sufficient:
            summary_material: list[tuple[str, str, float | None]] = []
            if metadata_text:
                summary_material.append(("metadata", metadata_text, None))
            # Short speech cannot make a work retrievable by itself, but when
            # independent visual evidence is sufficient it remains useful
            # auxiliary context for a grounded summary.
            summary_material.extend(normalized_text_rows)
            summary_material.extend(visual_rows)
            grounded_material: list[tuple[str, str, float | None]] = []
            if metadata_text:
                grounded_material.append(("metadata", metadata_text, None))
            if text_is_valid:
                grounded_material.extend(normalized_text_rows)
            grounded_material.extend(visual_rows)
            material = grounded_material
            combined = "\n\n".join(
                f"[{kind}] {text}" for kind, text, _ in summary_material if text
            )
            summary_prompt = await summary_prompt_for_work(
                session,
                work.id,
                str(runtime.get("summary_prompt") or ""),
            )
            summary, summary_usage = await provider.summarize(
                combined,
                asset_ids=(
                    remote_image_asset_names
                    if remote_image_asset_names is not None
                    else local_asset_names(work)
                ),
                system_prompt=summary_prompt,
                model=str(runtime.get("processing_model") or settings.enrichment_model),
            )
            pending_usages.append(summary_usage)
            notes = summary_markdown(summary)
            if notes:
                material.append(("notes", notes, None))
            expanded: list[tuple[str, str, float | None]] = []
            for kind, text, timestamp in material:
                expanded.extend((kind, chunk, timestamp) for chunk in chunk_text(text))
            vectors, embedding_usage = await provider.embed(
                [text for _, text, _ in expanded]
            )
            pending_usages.append(embedding_usage)
        else:
            combined = ""
            summary = {}
            summary_usage = None
            notes = ""
            expanded = []
            vectors = []
        if evidence_is_sufficient:
            if remote_image_stage is not None:
                await _queue_directory_promotion(
                    session, remote_image_stage, retained_dir
                )
                remote_image_stage = None
            if keyframe_stage is not None:
                await _queue_directory_promotion(session, keyframe_stage, keyframe_dir)
                keyframe_stage = None
        if downloaded_temp_video and temp_video and temp_video.exists():
            await asyncio.to_thread(unlink_with_retries, temp_video)
    except Exception:
        if remote_image_stage is not None:
            await asyncio.to_thread(
                shutil.rmtree, remote_image_stage, ignore_errors=True
            )
        if keyframe_stage is not None:
            await asyncio.to_thread(shutil.rmtree, keyframe_stage, ignore_errors=True)
        await rollback_file_promotions(session)
        # Usage calls may already have completed, but recording them is a short
        # local write and happens only after all long-running remote work stops.
        for pending_usage in pending_usages:
            await _record(session, pending_usage, job_id, work.id)
        raise

    # Keep the SQLite write transaction short: persist only after downloads,
    # transcription, OCR, summary generation, embedding and temp cleanup finish.
    for pending_usage in pending_usages:
        await _record(session, pending_usage, job_id, work.id)
    if not evidence_is_sufficient:
        if remote_image_stage is not None:
            await asyncio.to_thread(
                shutil.rmtree, remote_image_stage, ignore_errors=True
            )
            remote_image_stage = None
        if keyframe_stage is not None:
            await asyncio.to_thread(shutil.rmtree, keyframe_stage, ignore_errors=True)
            keyframe_stage = None
        if previous_evidence_state == "sufficient":
            _set_evidence_result(
                work,
                evidence_state="sufficient",
                supplement_state="failed",
                supplement_reason="evidence_insufficient",
                track_report=track_report,
            )
            work.processing_state = "processed"
            return "supplement_failed"
        await _replace_chunks(session, work, [], vectors=None)
        await session.execute(delete(WorkSummary).where(WorkSummary.work_id == work.id))
        await session.execute(delete(Keyframe).where(Keyframe.work_id == work.id))
        work.content_text = ""
        _set_evidence_result(
            work,
            evidence_state="insufficient",
            supplement_state="required",
            supplement_reason=(
                "image_set_incomplete"
                if work.kind == "image" and image_has_any_source
                else (
                    "full_video_unavailable"
                    if work.kind == "video" and not full_video_processed
                    else "evidence_insufficient"
                )
            ),
            track_report=track_report,
        )
        work.processing_state = "needs_supplement"
        work.process_error = "未提取到足够的原始内容，请补充完整素材"
        work.last_error_code = "evidence_insufficient"
        return "evidence_insufficient"

    if video_frames is not None:
        await session.execute(delete(Keyframe).where(Keyframe.work_id == work.id))
        for frame in video_frames:
            session.add(
                Keyframe(
                    work_id=work.id,
                    timestamp_seconds=frame.candidate.timestamp,
                    scene_score=frame.candidate.score,
                    path=str(frame.path),
                    perceptual_hash=frame.perceptual_hash,
                    candidate_source=frame.candidate.source,
                    selection_score=frame.selection_score,
                    selection_reason=frame.selection_reason,
                    ocr_text=frame.ocr_text,
                    visual_description=frame.visual_description,
                )
            )
    await store_summary(
        session,
        work,
        summary,
        model=summary_usage.model,
        source_text=combined,
    )
    await _replace_chunks(session, work, expanded, vectors)
    work.content_text = combined + (f"\n\n[notes]\n{notes}" if notes else "")
    if actual_video_duration is not None:
        work.duration_seconds = actual_video_duration
    if work.kind == "video" and not full_video_processed:
        supplement_state = "required"
        supplement_reason = "full_video_unavailable"
    elif work.kind == "image" and not image_complete:
        supplement_state = "required"
        supplement_reason = "image_set_incomplete"
    else:
        supplement_state = "none"
        supplement_reason = None
    _set_evidence_result(
        work,
        evidence_state="sufficient",
        supplement_state=supplement_state,
        supplement_reason=supplement_reason,
        track_report=track_report,
    )
    work.processing_state = "processed"
    work.process_error = None
    work.process_attempts = 0
    return work.processing_state


async def _replace_chunks(
    session: AsyncSession,
    work: Work,
    chunks: list[tuple[str, str, float | None]],
    vectors: list[list[float]] | None,
) -> None:
    await session.execute(
        delete(KnowledgeChunk).where(KnowledgeChunk.work_id == work.id)
    )
    for index, (source_kind, text, timestamp) in enumerate(chunks):
        if not text:
            continue
        session.add(
            KnowledgeChunk(
                work_id=work.id,
                chunk_index=index,
                source_kind=source_kind,
                text=text,
                start_seconds=timestamp,
                end_seconds=timestamp,
                embedding=vectors[index] if vectors and index < len(vectors) else None,
            )
        )
