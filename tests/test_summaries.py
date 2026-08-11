from app.models import Work, WorkSummary, utcnow
from app.services.summaries import (
    GENERATED_END,
    GENERATED_START,
    PROMPT_VERSION,
    extract_partial_summary_json,
    obsidian_asset_name,
    obsidian_markdown,
    extract_summary_json,
    safe_filename,
    summary_payload,
    summary_markdown,
    source_without_generated_notes,
)
from app.services.prompts import DEFAULT_SUMMARY_PROMPT
from app.services.providers import SUMMARY_MAX_OUTPUT_TOKENS


def test_summary_markdown_omits_missing_optional_sections():
    rendered = summary_markdown(
        {
            "one_sentence": "一句话看懂",
            "sections": [{"kind": "content", "title": "讲了什么", "body": "核心内容"}],
            "tags": ["学习"],
        }
    )
    assert "讲了什么" in rendered
    assert "核心内容" in rendered
    assert "为什么有效" not in rendered


def test_detailed_summary_contract_and_regeneration_source():
    assert PROMPT_VERSION == "summary-v3-memory-chain"
    assert "你是一位知识架构师" in DEFAULT_SUMMARY_PROMPT
    assert "完整记忆链" in DEFAULT_SUMMARY_PROMPT
    assert "重点深析与关联锚点" in DEFAULT_SUMMARY_PROMPT
    assert "为什么重要" in DEFAULT_SUMMARY_PROMPT
    assert SUMMARY_MAX_OUTPUT_TOKENS == 16_384
    assert "作品精华总结" not in summary_markdown({})
    assert (
        source_without_generated_notes("原始转写\n\n[notes]\n旧的简短总结")
        == "原始转写"
    )


def test_obsidian_markdown_has_stable_markers_and_safe_filename():
    work = Work(
        platform_work_id="123456",
        title="不能使用:这些/字符?",
        author_name="作者",
        source_url="https://www.douyin.com/note/123456",
    )
    summary = WorkSummary(
        work_id=1,
        status="ready",
        one_sentence="一句话看懂",
        content_json={
            "sections": [{"kind": "content", "title": "讲了什么", "body": "内容"}]
        },
        tags=["知识"],
        asset_ids=["image-01.jpg"],
        generated_at=utcnow(),
    )
    markdown = obsidian_markdown(work, summary, ["学习"])
    assert GENERATED_START in markdown and GENERATED_END in markdown
    assert "![[123456-image-01.jpg]]" in markdown
    assert "_assets/拾光/123456" not in markdown
    assert obsidian_asset_name(work, "nested/image-01.jpg") == "123456-image-01.jpg"
    filename = safe_filename(work)
    assert filename.endswith("__douyin-123456.md")
    assert ":" not in filename and "/" not in filename and "?" not in filename


def test_fenced_summary_json_is_recovered_for_old_exports():
    raw = """```json
{"one_sentence":"一句话","sections":[{"kind":"content","title":"讲了什么","body":"正文"}],"tags":["数学"],"asset_ids":["frame.jpg"]}
```"""
    assert extract_summary_json(raw)["one_sentence"] == "一句话"
    summary = WorkSummary(
        work_id=1,
        status="ready",
        one_sentence=raw,
        content_json={"sections": []},
        tags=[],
        asset_ids=[],
        generated_at=utcnow(),
    )
    payload = summary_payload(summary)
    assert payload["sections"][0]["body"] == "正文"
    assert payload["asset_ids"] == ["frame.jpg"]


def test_truncated_summary_json_recovers_complete_fields_without_raw_export():
    raw = """```json
{"one_sentence":"一句话","sections":[
{"kind":"concept","title":"第一节","body":"完整正文"},
{"kind":"concept","title":"第二节","body":"尾部仍完整但对象未闭合"
"""
    payload = extract_partial_summary_json(raw)
    assert payload["one_sentence"] == "一句话"
    assert [section["title"] for section in payload["sections"]] == [
        "第一节",
        "第二节",
    ]
    summary = WorkSummary(
        work_id=1,
        status="ready",
        one_sentence="```json",
        content_json={
            "sections": [{"kind": "content", "title": "讲了什么", "body": raw}]
        },
        tags=[],
        asset_ids=[],
        generated_at=utcnow(),
    )
    assert "```json" not in summary_markdown(summary_payload(summary))
