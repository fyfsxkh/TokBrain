"""Resumable work processing pipeline for video and image posts."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import DATA_DIR, settings
from app.models import Keyframe, KnowledgeChunk, Work, WorkSourceAsset
from app.services.budget import record_usage
from app.services.downloader import DownloadError, download_media, download_subtitle
from app.services.f2_links import PublicLinkError
from app.services.keyframes import extract_keyframes
from app.services.pricing import PRICE_VERSION
from app.services.providers import DashScopeProvider, ProviderUsage
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


async def _ocr_many(
    provider: DashScopeProvider,
    images: list[tuple[Path, float | None]],
    *,
    concurrency: int,
) -> list[tuple[str | None, ProviderUsage | None, float | None, Exception | None]]:
    """OCR images concurrently while preserving their original reading order."""

    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def run_one(
        image_path: Path, timestamp: float | None
    ) -> tuple[str | None, ProviderUsage | None, float | None, Exception | None]:
        try:
            async with semaphore:
                text, usage = await provider.ocr_image(image_path)
            return text, usage, timestamp, None
        except (
            Exception
        ) as exc:  # Preserve completed usage before surfacing a failed call.
            return None, None, timestamp, exc

    return await asyncio.gather(
        *(run_one(image_path, timestamp) for image_path, timestamp in images)
    )


async def _append_ocr_material(
    provider: DashScopeProvider,
    inputs: list[tuple[Path, float | None]],
    material: list[tuple[str, str, float | None]],
) -> tuple[list[ProviderUsage], Exception | None]:
    results = await _ocr_many(provider, inputs, concurrency=settings.ocr_concurrency)
    usages: list[ProviderUsage] = []
    first_error: Exception | None = None
    for text, usage, timestamp, error in results:
        if usage:
            usages.append(usage)
        if error and first_error is None:
            first_error = error
        if text:
            material.append(("ocr", text, timestamp))
    return usages, first_error


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
        except Exception:
            # Subtitle endpoints are optional and short-lived. As in the
            # Bilibili-inspired fallback chain, continue to audio or metadata.
            pass
        finally:
            await asyncio.to_thread(unlink_with_retries, subtitle_path)

    if not shutil.which("ffmpeg"):
        return "metadata", "", None
    audio_urls = _policy_strings(policy, "audio_urls")
    if not audio_urls:
        return "metadata", "", None
    audio_path = DATA_DIR / "tmp" / f"{work.platform_work_id}.restricted-audio"
    try:
        await _download_first(
            audio_urls,
            audio_path,
            max_bytes=settings.max_download_megabytes * 1024 * 1024,
        )
        text, usage = await provider.transcribe(audio_path, work.duration_seconds)
        return ("transcript", text, usage) if text else ("metadata", "", usage)
    except PublicLinkError:
        raise
    except Exception:
        return "metadata", "", None
    finally:
        await asyncio.to_thread(unlink_with_retries, audio_path)


async def process_work(
    session: AsyncSession,
    work: Work,
    job_id: str,
) -> str:
    """Process one work. Returns processed, waiting_for_key, or metadata_only."""
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
        await _replace_chunks(
            session, work, [("metadata", metadata_text, None)], vectors=None
        )
        work.content_text = metadata_text
        work.processing_state = "waiting_for_key"
        return "waiting_for_key"

    provider = DashScopeProvider(api_key)
    runtime = await get_runtime_settings(session)
    material: list[tuple[str, str, float | None]] = [("metadata", metadata_text, None)]
    pending_usages: list[ProviderUsage] = []
    temp_video: Path | None = None
    downloaded_temp_video = False
    video_frames = None
    retained_dir = DATA_DIR / "media" / work.platform_work_id
    keyframe_dir = DATA_DIR / "keyframes" / work.platform_work_id
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
    try:
        if work.kind == "video" and full_video_available and shutil.which("ffmpeg"):
            if local_video:
                temp_video = local_video
            else:
                temp_video = DATA_DIR / "tmp" / f"{work.platform_work_id}.mp4"
                await _download_first(
                    list(work.media_urls or []),
                    temp_video,
                    max_bytes=settings.max_download_megabytes * 1024 * 1024,
                )
                downloaded_temp_video = True

            async def extract_visuals():
                extracted = await extract_keyframes(
                    temp_video,
                    keyframe_dir,
                    duration_seconds=work.duration_seconds,
                    threshold=float(runtime["scene_threshold"]),
                    max_candidates=int(runtime["max_scene_candidates"]),
                    max_frames=int(runtime["max_keyframes"]),
                    min_gap_seconds=float(runtime["min_keyframe_gap_seconds"]),
                    analysis_width=settings.keyframe_analysis_width,
                )
                results = await _ocr_many(
                    provider,
                    [(path, candidate.timestamp) for path, candidate, _ in extracted],
                    concurrency=settings.ocr_concurrency,
                )
                return extracted, results

            # Audio transcription waits on a remote job; use that time to analyze
            # frames and run OCR instead of adding both durations together.
            branch_results = await asyncio.gather(
                provider.transcribe(temp_video, work.duration_seconds),
                extract_visuals(),
                return_exceptions=True,
            )
            # A thread-backed transcription cannot be stopped safely midway through
            # Windows file I/O. Wait for both branches, then surface either error.
            for result in branch_results:
                if isinstance(result, Exception):
                    raise result
            (transcript, transcript_usage), (video_frames, ocr_results) = branch_results
            pending_usages.append(transcript_usage)
            if transcript:
                material.append(("transcript", transcript, 0.0))
            first_ocr_error: Exception | None = None
            for text, ocr_usage, timestamp, error in ocr_results:
                if ocr_usage:
                    pending_usages.append(ocr_usage)
                if error and first_ocr_error is None:
                    first_ocr_error = error
                if text:
                    material.append(("ocr", text, timestamp))
            if first_ocr_error:
                raise first_ocr_error
        elif work.kind == "video" and not local_video:
            source_kind, text, usage = await _restricted_text_or_audio(
                provider, work, media_policy
            )
            if usage:
                pending_usages.append(usage)
            if text:
                material.append((source_kind, text, 0.0))
        elif work.kind == "image" and (
            local_images or (remote_full_media_allowed and work.image_urls)
        ):
            retained_dir.mkdir(parents=True, exist_ok=True)
            ocr_inputs = []
            image_sources: list[Path | str] = (
                local_images if local_images else list(work.image_urls or [])
            )
            for index, source in enumerate(
                image_sources[: int(runtime["max_keyframes"])]
            ):
                if isinstance(source, Path):
                    image_path = source
                else:
                    image_path = retained_dir / f"image-{index + 1:02d}.jpg"
                    await download_media(
                        source,
                        image_path,
                        max_bytes=min(settings.max_download_megabytes, 30)
                        * 1024
                        * 1024,
                    )
                ocr_inputs.append((image_path, float(index)))
            ocr_usages, ocr_error = await _append_ocr_material(
                provider, ocr_inputs, material
            )
            pending_usages.extend(ocr_usages)
            if ocr_error:
                raise ocr_error

        combined = "\n\n".join(f"[{kind}] {text}" for kind, text, _ in material if text)
        summary, summary_usage = await provider.summarize(
            combined,
            asset_ids=local_asset_names(work),
            system_prompt=str(runtime.get("summary_prompt") or ""),
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
        if downloaded_temp_video and temp_video and temp_video.exists():
            await asyncio.to_thread(unlink_with_retries, temp_video)
    except Exception:
        # Usage calls may already have completed, but recording them is a short
        # local write and happens only after all long-running remote work stops.
        for pending_usage in pending_usages:
            await _record(session, pending_usage, job_id, work.id)
        raise

    # Keep the SQLite write transaction short: persist only after downloads,
    # transcription, OCR, summary generation, embedding and temp cleanup finish.
    for pending_usage in pending_usages:
        await _record(session, pending_usage, job_id, work.id)
    if video_frames is not None:
        await session.execute(delete(Keyframe).where(Keyframe.work_id == work.id))
        for frame_path, candidate, image_hash in video_frames:
            session.add(
                Keyframe(
                    work_id=work.id,
                    timestamp_seconds=candidate.timestamp,
                    scene_score=candidate.score,
                    path=str(frame_path),
                    perceptual_hash=image_hash,
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
    work.processing_state = "waiting_for_ffmpeg" if waiting_for_runtime else "processed"
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
