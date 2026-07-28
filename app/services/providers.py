"""DashScope model calls with usage accounting returned to the caller."""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import shutil
import socket
import uuid
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import AsyncIterator, Callable
from urllib.parse import urljoin, urlsplit

import httpx
from openai import AsyncOpenAI

from app.config import settings
from app.services.pricing import PRICE_VERSION, asr_cost, token_cost
from app.services.prompts import DEFAULT_SUMMARY_PROMPT
from app.services.summaries import extract_summary_json
from app.services.temp_files import unlink_with_retries


SUMMARY_MAX_OUTPUT_TOKENS = 16_384
DETAILED_SUMMARY_SYSTEM_PROMPT = DEFAULT_SUMMARY_PROMPT
ASR_RESULT_MAX_BYTES = 10 * 1024 * 1024
ASR_RESULT_MAX_REDIRECTS = 3
ASR_RESULT_HOST_SUFFIXES = (".aliyuncs.com",)


def _validate_asr_result_url(url: str) -> str:
    """Allow only public Alibaba Cloud HTTPS result locations."""

    try:
        parsed = urlsplit(str(url).strip())
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise RuntimeError("ASR 转写结果地址无效") from exc
    host = (parsed.hostname or "").lower().strip(".")
    dotted_host = f".{host}"
    if (
        parsed.scheme.lower() != "https"
        or not host
        or parsed.username
        or parsed.password
        or port not in (None, 443)
        or not any(dotted_host.endswith(suffix) for suffix in ASR_RESULT_HOST_SUFFIXES)
    ):
        raise RuntimeError("ASR 转写结果地址不在允许范围内")
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise RuntimeError("ASR 转写结果域名解析失败") from exc
    if not addresses:
        raise RuntimeError("ASR 转写结果域名没有可用地址")
    for result in addresses:
        try:
            address = ipaddress.ip_address(result[4][0])
        except ValueError as exc:
            raise RuntimeError("ASR 转写结果域名解析异常") from exc
        if not address.is_global:
            raise RuntimeError("ASR 转写结果地址不是公网地址")
    return str(url)


def _download_asr_result(
    url: str,
    *,
    transport: httpx.BaseTransport | None = None,
    validate_url: Callable[[str], str] = _validate_asr_result_url,
) -> dict:
    """Download one bounded JSON result without leaking its signed query."""

    current = validate_url(url)
    try:
        with httpx.Client(
            timeout=30,
            follow_redirects=False,
            transport=transport,
            trust_env=False,
        ) as client:
            for redirect_count in range(ASR_RESULT_MAX_REDIRECTS + 1):
                with client.stream(
                    "GET", current, headers={"Accept": "application/json"}
                ) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if (
                            not location
                            or redirect_count >= ASR_RESULT_MAX_REDIRECTS
                        ):
                            raise RuntimeError("ASR 转写结果重定向次数过多")
                        current = validate_url(urljoin(current, location))
                        continue
                    if response.status_code != 200:
                        raise RuntimeError(
                            f"ASR 转写结果下载失败: HTTP {response.status_code}"
                        )
                    declared_length = response.headers.get("content-length")
                    if (
                        declared_length
                        and declared_length.isdigit()
                        and int(declared_length) > ASR_RESULT_MAX_BYTES
                    ):
                        raise RuntimeError("ASR 转写结果超过 10 MB 上限")
                    content = bytearray()
                    for chunk in response.iter_bytes():
                        if len(content) + len(chunk) > ASR_RESULT_MAX_BYTES:
                            raise RuntimeError("ASR 转写结果超过 10 MB 上限")
                        content.extend(chunk)
                    try:
                        payload = json.loads(bytes(content).decode("utf-8"))
                    except (UnicodeDecodeError, ValueError) as exc:
                        raise RuntimeError("ASR 转写结果不是有效 JSON") from exc
                    if not isinstance(payload, dict):
                        raise RuntimeError("ASR 转写结果结构无效")
                    return payload
    except httpx.HTTPError:
        raise RuntimeError("ASR 转写结果下载失败") from None
    raise RuntimeError("ASR 转写结果下载失败")


@dataclass(slots=True)
class ProviderUsage:
    model: str
    input_tokens: int
    output_tokens: int
    cost_cny: float
    metric: str = "tokens"
    quantity: float = 0
    unit: str = "token"


def asr_audio_command(ffmpeg: str, media_path: Path, audio_path: Path) -> list[str]:
    """Build the official 16 kHz mono Opus preprocessing command for Paraformer."""

    return [
        ffmpeg,
        "-y",
        "-i",
        str(media_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "libopus",
        "-b:a",
        "32k",
        str(audio_path),
    ]


class DashScopeProvider:
    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("尚未配置百炼 API Key")
        self.api_key = api_key
        self.client = AsyncOpenAI(api_key=api_key, base_url=settings.dashscope_base_url)

    async def ocr_image(self, image_path: Path) -> tuple[str, ProviderUsage]:
        mime = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"
        encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
        response = await self.client.chat.completions.create(
            model=settings.ocr_model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "按阅读顺序完整提取图片中的文字；没有文字时返回空字符串。",
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{encoded}"},
                        },
                    ],
                }
            ],
            temperature=0,
            max_tokens=2048,
        )
        text = response.choices[0].message.content or ""
        usage = response.usage
        input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        return text.strip(), ProviderUsage(
            settings.ocr_model,
            input_tokens,
            output_tokens,
            token_cost(settings.ocr_model, input_tokens, output_tokens),
            quantity=input_tokens + output_tokens,
        )

    async def enrich(self, content: str) -> tuple[str, ProviderUsage]:
        response = await self.client.chat.completions.create(
            model=settings.enrichment_model,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": "将抖音作品整理成忠于原文、便于检索的中文笔记。保留人物、数字、步骤和结论，不臆测。",
                },
                {"role": "user", "content": content[:40_000]},
            ],
            temperature=0.1,
            max_tokens=4096,
        )
        text = response.choices[0].message.content or ""
        usage = response.usage
        input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        return text.strip(), ProviderUsage(
            settings.enrichment_model,
            input_tokens,
            output_tokens,
            token_cost(settings.enrichment_model, input_tokens, output_tokens),
            quantity=input_tokens + output_tokens,
        )

    async def embed(self, texts: list[str]) -> tuple[list[list[float]], ProviderUsage]:
        if not texts:
            return [], ProviderUsage(settings.embedding_model, 0, 0, 0)
        vectors: list[list[float]] = []
        input_tokens = 0
        for start in range(0, len(texts), 10):
            response = await self.client.embeddings.create(
                model=settings.embedding_model,
                input=texts[start : start + 10],
                dimensions=settings.embedding_dimensions,
            )
            vectors.extend(item.embedding for item in response.data)
            input_tokens += int(getattr(response.usage, "prompt_tokens", 0) or 0)
        return vectors, ProviderUsage(
            settings.embedding_model,
            input_tokens,
            0,
            token_cost(settings.embedding_model, input_tokens),
            quantity=input_tokens,
        )

    @staticmethod
    def _answer_system_prompt() -> str:
        return (
            "只根据给定收藏知识回答，证据不足时明确说不知道，并使用[来源N]标注依据。"
            "使用清晰、通俗的中文 Markdown；可以使用标题、列表和表格。"
            "行内公式只能使用 $...$，独立公式只能使用 $$...$$；绝对不要使用 \\(...\\) 或 \\[...\\]。"
            "出现数学公式时必须紧接着用普通中文解释公式中每个关键符号和结论，"
            "不要只给公式或堆叠专业术语。"
        )

    async def chat(
        self, question: str, context: str, *, model: str | None = None
    ) -> tuple[str, ProviderUsage]:
        selected_model = model or settings.chat_model
        response = await self.client.chat.completions.create(
            model=selected_model,
            messages=[
                {
                    "role": "system",
                    "content": self._answer_system_prompt(),
                },
                {
                    "role": "user",
                    "content": f"问题：{question}\n\n收藏知识：\n{context}",
                },
            ],
            temperature=0.2,
            max_tokens=2048,
        )
        text = response.choices[0].message.content or ""
        usage = response.usage
        input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        return text.strip(), ProviderUsage(
            selected_model,
            input_tokens,
            output_tokens,
            token_cost(selected_model, input_tokens, output_tokens),
            quantity=input_tokens + output_tokens,
        )

    async def chat_stream(
        self, question: str, context: str, *, model: str
    ) -> AsyncIterator[tuple[str, str | ProviderUsage]]:
        stream = await self.client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": self._answer_system_prompt()},
                {
                    "role": "user",
                    "content": f"问题：{question}\n\n收藏知识：\n{context}",
                },
            ],
            temperature=0.2,
            max_tokens=2048,
            stream=True,
            stream_options={"include_usage": True},
        )
        input_tokens = 0
        output_tokens = 0
        output_characters = 0
        async for chunk in stream:
            usage = getattr(chunk, "usage", None)
            if usage:
                input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
                output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
            choices = getattr(chunk, "choices", None) or []
            if choices:
                delta = getattr(choices[0].delta, "content", None)
                if delta:
                    output_characters += len(delta)
                    yield "delta", delta
        if input_tokens <= 0:
            input_tokens = max(1, (len(question) + len(context)) // 2)
        if output_tokens <= 0 and output_characters:
            output_tokens = max(1, output_characters // 2)
        yield "usage", ProviderUsage(
            model,
            input_tokens,
            output_tokens,
            token_cost(model, input_tokens, output_tokens),
            quantity=input_tokens + output_tokens,
        )

    async def summarize(
        self,
        content: str,
        *,
        asset_ids: list[str] | None = None,
        system_prompt: str | None = None,
        model: str | None = None,
    ) -> tuple[dict, ProviderUsage]:
        selected_model = model or settings.enrichment_model
        allowed_assets = list(dict.fromkeys(asset_ids or []))[:12]
        source = content[:80_000]
        response = await self.client.chat.completions.create(
            model=selected_model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        (system_prompt or DEFAULT_SUMMARY_PROMPT).strip()
                        + "\n\n【固定输出约束】只返回一个 JSON 对象，"
                        "字段必须包含 one_sentence、sections、tags、asset_ids；"
                        "sections 的每项必须包含 kind、title、body。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"允许的图片文件名：{json.dumps(allowed_assets, ensure_ascii=False)}\n\n"
                        "请严格按照系统提示词建立完整记忆链。材料中出现几个概念、观点、"
                        "案例、方法或步骤，就逐项完整复现几个，不得只保留结论。\n\n"
                        f"作品材料开始：\n{source}\n作品材料结束。"
                    ),
                },
            ],
            temperature=0.1,
            max_tokens=SUMMARY_MAX_OUTPUT_TOKENS,
            response_format={"type": "json_object"},
        )
        text = (response.choices[0].message.content or "").strip()
        payload = extract_summary_json(text)
        if payload is None:
            payload = {
                "one_sentence": text.splitlines()[0][:300] if text else "内容概览",
                "sections": [
                    {
                        "kind": "content",
                        "title": "总结格式异常",
                        "body": "模型返回内容未能解析为结构化总结，请点击“补齐/更新总结”重试。",
                    }
                ],
                "tags": [],
                "asset_ids": allowed_assets[:1],
            }
        if not isinstance(payload, dict):
            payload = {}
        one_sentence = str(payload.get("one_sentence") or "内容概览")[:500]
        sections = []
        for item in payload.get("sections") or []:
            if not isinstance(item, dict) or not str(item.get("body") or "").strip():
                continue
            sections.append(
                {
                    "kind": str(item.get("kind") or "other")[:30],
                    "title": str(item.get("title") or "内容要点")[:100],
                    "body": str(item.get("body"))[:20_000],
                }
            )
        if not sections:
            sections = [{"kind": "content", "title": "讲了什么", "body": one_sentence}]
        tags = [
            str(item)[:50] for item in (payload.get("tags") or []) if str(item).strip()
        ][:12]
        chosen_assets = [
            str(item)
            for item in (payload.get("asset_ids") or [])
            if str(item) in allowed_assets
        ][:6]
        if not chosen_assets:
            chosen_assets = allowed_assets[: min(3, len(allowed_assets))]
        usage = response.usage
        input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        return {
            "one_sentence": one_sentence,
            "sections": sections,
            "tags": tags,
            "asset_ids": chosen_assets,
        }, ProviderUsage(
            selected_model,
            input_tokens,
            output_tokens,
            token_cost(selected_model, input_tokens, output_tokens),
            quantity=input_tokens + output_tokens,
        )

    async def transcribe(
        self, media_path: Path, duration_seconds: float
    ) -> tuple[str, ProviderUsage]:
        text = await asyncio.to_thread(self._transcribe_sync, media_path)
        return text, ProviderUsage(
            settings.asr_model,
            0,
            0,
            asr_cost(settings.asr_model, duration_seconds),
            metric="audio_seconds",
            quantity=duration_seconds,
            unit="second",
        )

    def _transcribe_sync(self, media_path: Path) -> str:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("未找到 ffmpeg")
        # A unique name prevents a briefly locked file from blocking a retry of
        # the same work on Windows.
        audio_path = media_path.with_name(
            f"{media_path.stem}-{uuid.uuid4().hex[:10]}.asr.opus"
        )
        try:
            import dashscope
            from dashscope.audio.asr import Transcription
            from dashscope.utils.oss_utils import OssUtils
        except ImportError as exc:
            raise RuntimeError("未安装 dashscope SDK") from exc
        process = __import__("subprocess").run(
            asr_audio_command(ffmpeg, media_path, audio_path),
            capture_output=True,
            text=True,
        )
        if process.returncode != 0:
            raise RuntimeError(f"音频提取失败: {process.stderr[-500:]}")
        dashscope.api_key = self.api_key
        try:
            oss_url = OssUtils.upload(
                model=settings.asr_model,
                file_path=str(audio_path),
                api_key=self.api_key,
            )
            response = Transcription.async_call(
                model=settings.asr_model,
                file_urls=[oss_url],
                language_hints=["zh", "en"],
            )
            task_id = getattr(response.output, "task_id", None) or response.output.get(
                "task_id"
            )
            result = Transcription.wait(task=task_id)
            if getattr(result, "status_code", None) != HTTPStatus.OK:
                raise RuntimeError(f"ASR 失败: {getattr(result, 'message', '')}")
            output = result.output
            results = (
                output.get("results", [])
                if isinstance(output, dict)
                else output.results
            )
            texts: list[str] = []
            for item in results or []:
                url = (
                    item.get("transcription_url")
                    if isinstance(item, dict)
                    else item.transcription_url
                )
                if not url:
                    continue
                payload = _download_asr_result(str(url))
                for transcript in payload.get("transcripts", []):
                    if transcript.get("text"):
                        texts.append(transcript["text"])
                    else:
                        texts.extend(
                            s.get("text", "") for s in transcript.get("sentences", [])
                        )
            return "\n".join(text for text in texts if text).strip()
        finally:
            unlink_with_retries(audio_path)
