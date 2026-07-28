"""Conservative China (Beijing) list-price estimates.

Source version: Alibaba Cloud Model Studio pricing, checked 2026-07-19.
Free quotas and temporary discounts are intentionally not deducted.
"""

from __future__ import annotations


PRICE_VERSION = "aliyun-cn-list-2026-07-19"

# CNY per one million tokens: (input, output)
TOKEN_PRICES = {
    "qwen3.5-ocr": (0.5, 2.0),
    "qwen3.6-flash": (1.2, 7.2),
    "qwen3.7-plus": (2.0, 8.0),
    "text-embedding-v4": (0.5, 0.0),
    "qwen3-rerank": (0.5, 0.0),
}

ASR_CNY_PER_SECOND = {"paraformer-v2": 0.00008}


def token_cost(model: str, input_tokens: int, output_tokens: int = 0) -> float:
    input_price, output_price = TOKEN_PRICES.get(model, (0.0, 0.0))
    return input_tokens / 1_000_000 * input_price + output_tokens / 1_000_000 * output_price


def asr_cost(model: str, seconds: float) -> float:
    return max(0.0, seconds) * ASR_CNY_PER_SECOND.get(model, 0.0)
