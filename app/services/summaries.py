"""Grounded work summaries and Obsidian-friendly rendering helpers."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import DATA_DIR
from app.models import Work, WorkSummary, utcnow


PROMPT_VERSION = "summary-v3-memory-chain"
GENERATED_START = "<!-- shiguang:generated:start -->"
GENERATED_END = "<!-- shiguang:generated:end -->"


def extract_summary_json(value: str) -> dict | None:
    """Recover JSON even when a model wrapped it in prose or a code fence."""

    text = str(value or "").strip()
    candidates = [text]
    candidates.extend(
        match.group(1).strip()
        for match in re.finditer(
            r"```(?:json)?\s*([\s\S]*?)```",
            text,
            flags=re.IGNORECASE,
        )
    )
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace >= 0 and last_brace > first_brace:
        candidates.append(text[first_brace : last_brace + 1])
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(payload, dict):
            return payload
    return None


def extract_partial_summary_json(value: str) -> dict | None:
    """Recover complete JSON string fields from an older truncated response."""

    text = str(value or "")
    if '"one_sentence"' not in text or '"sections"' not in text:
        return None
    decoder = json.JSONDecoder()

    def string_values(key: str) -> list[tuple[int, str]]:
        values: list[tuple[int, str]] = []
        pattern = re.compile(rf'"{re.escape(key)}"\s*:\s*')
        for match in pattern.finditer(text):
            try:
                decoded, _ = decoder.raw_decode(text[match.end() :])
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(decoded, str) and decoded.strip():
                values.append((match.start(), decoded.strip()))
        return values

    one_sentences = string_values("one_sentence")
    bodies = string_values("body")
    if not one_sentences and not bodies:
        return None
    titles = string_values("title")
    kinds = string_values("kind")
    sections = []
    for body_position, body in bodies:
        prior_titles = [item for item in titles if item[0] < body_position]
        prior_kinds = [item for item in kinds if item[0] < body_position]
        sections.append(
            {
                "kind": prior_kinds[-1][1] if prior_kinds else "content",
                "title": prior_titles[-1][1] if prior_titles else "内容要点",
                "body": body,
            }
        )
    one_sentence = (
        one_sentences[0][1]
        if one_sentences
        else (sections[0]["body"][:300] if sections else "内容概览")
    )
    return {
        "one_sentence": one_sentence,
        "sections": sections
        or [{"kind": "content", "title": "内容概览", "body": one_sentence}],
        "tags": [],
        "asset_ids": [],
    }


def local_asset_names(work: Work) -> list[str]:
    names: list[str] = []
    for root_name in ("media", "keyframes"):
        root = DATA_DIR / root_name / work.platform_work_id
        if root.exists():
            names.extend(
                path.name
                for path in sorted(root.iterdir())
                if path.is_file()
                and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
            )
    return list(dict.fromkeys(names))


def resolve_asset(work: Work, asset_name: str) -> Path | None:
    if Path(asset_name).name != asset_name:
        return None
    for root_name in ("media", "keyframes"):
        root = (DATA_DIR / root_name / work.platform_work_id).resolve()
        target = (root / asset_name).resolve()
        if root in target.parents and target.is_file():
            return target
    return None


def summary_markdown(payload: dict) -> str:
    lines = [f"> {payload.get('one_sentence') or '内容概览'}"]
    for section in payload.get("sections") or []:
        title = str(section.get("title") or "内容要点").strip()
        body = str(section.get("body") or "").strip()
        if body:
            lines.extend(["", f"## {title}", "", body])
    tags = [str(item) for item in payload.get("tags") or [] if str(item).strip()]
    if tags:
        lines.extend(["", "## 关键词", "", " · ".join(f"#{item}" for item in tags)])
    return "\n".join(lines).strip()


async def store_summary(
    session: AsyncSession,
    work: Work,
    payload: dict,
    *,
    model: str,
    source_text: str,
) -> WorkSummary:
    row = (
        await session.execute(select(WorkSummary).where(WorkSummary.work_id == work.id))
    ).scalar_one_or_none()
    if not row:
        row = WorkSummary(work_id=work.id)
        session.add(row)
    row.status = "ready"
    row.one_sentence = str(payload.get("one_sentence") or "内容概览")[:500]
    row.content_json = {"sections": payload.get("sections") or []}
    row.tags = list(payload.get("tags") or [])[:12]
    row.asset_ids = list(payload.get("asset_ids") or [])[:6]
    row.model = model
    row.prompt_version = PROMPT_VERSION
    row.source_digest = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    row.error = None
    row.generated_at = utcnow()
    row.updated_at = utcnow()
    await session.flush()
    return row


async def mark_summary_failed(
    session: AsyncSession, work: Work, error: str
) -> WorkSummary:
    row = (
        await session.execute(select(WorkSummary).where(WorkSummary.work_id == work.id))
    ).scalar_one_or_none()
    if not row:
        row = WorkSummary(work_id=work.id)
        session.add(row)
    row.status = "failed"
    row.error = error[:500]
    row.updated_at = utcnow()
    await session.flush()
    return row


def summary_payload(row: WorkSummary) -> dict:
    payload = {
        "one_sentence": row.one_sentence,
        "sections": list((row.content_json or {}).get("sections") or []),
        "tags": list(row.tags or []),
        "asset_ids": list(row.asset_ids or []),
    }
    embedded_source = "\n\n".join(
        [
            str(payload["one_sentence"] or ""),
            *[
                str(section.get("body") or "")
                for section in payload["sections"]
                if isinstance(section, dict)
            ],
        ]
    )
    embedded = extract_summary_json(embedded_source) or extract_partial_summary_json(
        embedded_source
    )
    if embedded and isinstance(embedded.get("sections"), list):
        return {
            "one_sentence": str(
                embedded.get("one_sentence") or payload["one_sentence"] or "内容概览"
            )[:500],
            "sections": [
                {
                    "kind": str(section.get("kind") or "content")[:30],
                    "title": str(section.get("title") or "内容要点")[:100],
                    "body": str(section.get("body") or "")[:20_000],
                }
                for section in embedded["sections"]
                if isinstance(section, dict) and str(section.get("body") or "").strip()
            ],
            "tags": [
                str(item)[:50]
                for item in (embedded.get("tags") or payload["tags"])
                if str(item).strip()
            ][:12],
            "asset_ids": [
                str(item)
                for item in (embedded.get("asset_ids") or payload["asset_ids"])
                if str(item).strip()
            ][:6],
        }
    return payload


def source_without_generated_notes(content: str | None) -> str:
    """Keep regenerated summaries grounded in source material, not the old summary."""

    value = (content or "").strip()
    return value.split("\n\n[notes]\n", 1)[0].strip()


def _yaml(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def safe_filename(work: Work) -> str:
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", work.title or "未命名作品")
    title = re.sub(r"\s+", " ", title).strip(" ._")[:80] or "未命名作品"
    return f"{title}__douyin-{work.platform_work_id}.md"


def obsidian_asset_name(work: Work, asset: str) -> str:
    """Return a vault-wide unique, flat attachment filename for Obsidian."""

    basename = Path(asset).name
    basename = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", basename)
    basename = basename.strip(" ._") or "image.jpg"
    return f"{work.platform_work_id}-{basename}"


def obsidian_markdown(work: Work, row: WorkSummary, collections: list[str]) -> str:
    payload = summary_payload(row)
    generated = [
        GENERATED_START,
        f"# {work.title or '未命名作品'}",
        "",
        summary_markdown(payload),
    ]
    assets = list(payload.get("asset_ids") or [])
    if assets:
        generated.extend(["", "## 相关图片", ""])
        for asset in assets:
            generated.append(f"![[{obsidian_asset_name(work, asset)}]]")
    generated.append(GENERATED_END)
    frontmatter = [
        "---",
        f"title: {_yaml(work.title or '未命名作品')}",
        f"author: {_yaml(work.author_name or '')}",
        f"source: {_yaml(work.source_url or '')}",
        f"douyin_id: {_yaml(work.platform_work_id)}",
        f"collections: {_yaml(collections)}",
        f"tags: {_yaml(list(row.tags or []))}",
        f"generated_at: {_yaml(row.generated_at.isoformat() if row.generated_at else '')}",
        "---",
        "",
    ]
    return "\n".join([*frontmatter, *generated, "", "## 我的笔记", ""])
