"""Optional Alibaba Cloud BSS bill synchronization.

This deliberately uses separate read-only AccessKey credentials. A DashScope
API key cannot query the account bill. Official values are stored with their
refresh timestamp and are never used as a real-time hard limit.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppSetting
from app.services.secrets import SecretUnavailableError, get_secret


PRODUCT_MARKERS = ("百炼", "大模型服务平台", "model studio", "dashscope")


def _attr(item: Any, *names: str, default=None):
    for name in names:
        if isinstance(item, dict) and name in item:
            return item[name]
        value = getattr(item, name, None)
        if value is not None:
            return value
    return default


def _query_bill_sync(access_key_id: str, access_key_secret: str) -> float:
    try:
        from alibabacloud_bssopenapi20171214.client import Client as BssClient
        from alibabacloud_bssopenapi20171214.models import DescribeInstanceBillRequest
        from alibabacloud_tea_openapi.models import Config
    except ImportError as exc:
        raise RuntimeError("未安装阿里云 BSS OpenAPI 可选依赖") from exc

    config = Config(access_key_id=access_key_id, access_key_secret=access_key_secret)
    config.endpoint = "business.aliyuncs.com"
    client = BssClient(config)
    cycle = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m")
    next_token: str | None = None
    amount = 0.0
    for _ in range(100):
        request = DescribeInstanceBillRequest(
            billing_cycle=cycle,
            max_results=300,
            next_token=next_token,
            is_billing_item=False,
        )
        response = client.describe_instance_bill(request)
        data = _attr(_attr(response, "body"), "data")
        items = _attr(data, "items", default=[]) or []
        for item in items:
            label = " ".join(
                str(_attr(item, key, default="") or "")
                for key in ("product_name", "product_code", "product_detail")
            ).lower()
            if any(marker in label for marker in PRODUCT_MARKERS):
                amount += float(_attr(item, "pretax_amount", "pretax_gross_amount", default=0) or 0)
        next_token = _attr(data, "next_token")
        if not next_token or not items:
            break
    return amount


async def refresh_official_bill(session: AsyncSession) -> dict:
    try:
        access_key_id = await get_secret(session, "bss_access_key_id")
        access_key_secret = await get_secret(session, "bss_access_key_secret")
    except SecretUnavailableError:
        value = {
            "status": "credentials_unreadable",
            "amount_cny": None,
            "data_as_of": datetime.now(timezone.utc).isoformat(),
            "message": (
                "已保存的账单凭据无法由当前 Windows 用户解密。"
                "请删除全部 API Key / AccessKey 后重新输入账单凭据。"
            ),
        }
        record = await session.get(AppSetting, "official_bill")
        if record:
            record.value = value
        else:
            session.add(AppSetting(key="official_bill", value=value))
        return value
    if not access_key_id or not access_key_secret:
        return {"status": "not_configured", "message": "未配置只读 BSS AccessKey"}
    try:
        amount = await asyncio.to_thread(_query_bill_sync, access_key_id, access_key_secret)
        value = {
            "status": "available_delayed",
            "amount_cny": round(amount, 4),
            "data_as_of": datetime.now(timezone.utc).isoformat(),
            "message": "官方账单存在结算延迟，仅供对账",
        }
    except Exception as exc:
        value = {
            "status": "error",
            "amount_cny": None,
            "data_as_of": datetime.now(timezone.utc).isoformat(),
            "message": str(exc),
        }
    record = await session.get(AppSetting, "official_bill")
    if record:
        record.value = value
    else:
        session.add(AppSetting(key="official_bill", value=value))
    return value
