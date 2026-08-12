from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import DPAPI_WARNING
from app.config import settings
from app.database import get_db
from app.models import AppSetting
from app.schemas import (
    IntegrationTokenCreated,
    IntegrationTokenView,
    SettingsUpdate,
    SettingsView,
    UsageSummary,
)
from app.services.bss_bill import refresh_official_bill
from app.services.budget import usage_summary
from app.services.runtime_settings import (
    CHAT_MODEL_OPTIONS,
    PROCESSING_MODEL_OPTIONS,
    get_runtime_settings,
    update_runtime_settings,
)
from app.services.prompts import DEFAULT_SUMMARY_PROMPT
from app.services.secrets import has_readable_secret, set_secret
from app.services.import_integrations import (
    integration_token_status,
    revoke_integration_token,
    rotate_integration_token,
)


router = APIRouter(prefix="/api/settings", tags=["settings"])


async def _view(session: AsyncSession) -> SettingsView:
    runtime = await get_runtime_settings(session)
    cleanup = await session.get(AppSetting, "security_cleanup")
    cleanup_value = cleanup.value if cleanup and isinstance(cleanup.value, dict) else {}
    return SettingsView(
        **runtime,
        default_summary_prompt=DEFAULT_SUMMARY_PROMPT,
        processing_model_options=list(PROCESSING_MODEL_OPTIONS),
        chat_model_options=list(CHAT_MODEL_OPTIONS),
        dpapi_warning=DPAPI_WARNING,
        has_dashscope_key=await has_readable_secret(session, "dashscope_api_key"),
        has_bss_credentials=(
            await has_readable_secret(session, "bss_access_key_id")
            and await has_readable_secret(session, "bss_access_key_secret")
        ),
        has_f2_cookie=await has_readable_secret(session, "f2_cookie"),
        security_cleanup_required=bool(cleanup_value.get("required")),
        security_cleanup_message=str(cleanup_value.get("message") or ""),
        ocr_model=settings.ocr_model,
        asr_model=settings.asr_model,
        embedding_model=settings.embedding_model,
    )


@router.get("", response_model=SettingsView)
async def get_settings(session: AsyncSession = Depends(get_db)):
    return await _view(session)


@router.put("", response_model=SettingsView)
async def put_settings(
    payload: SettingsUpdate, session: AsyncSession = Depends(get_db)
):
    values = payload.model_dump(exclude_unset=True)
    if values.pop("clear_f2_cookie", False):
        await set_secret(session, "f2_cookie", None)
    for field in (
        "dashscope_api_key",
        "bss_access_key_id",
        "bss_access_key_secret",
        "f2_cookie",
    ):
        if field in values:
            await set_secret(session, field, values.pop(field))
    await update_runtime_settings(session, values)
    await session.commit()
    return await _view(session)


@router.get("/usage", response_model=UsageSummary)
async def get_usage(session: AsyncSession = Depends(get_db)):
    return await usage_summary(session)


@router.post("/official-bill/refresh")
async def refresh_bill(session: AsyncSession = Depends(get_db)):
    result = await refresh_official_bill(session)
    await session.commit()
    if result.get("status") in {"credentials_unreadable", "not_configured"}:
        raise HTTPException(status_code=409, detail=result.get("message"))
    if result.get("status") == "error":
        raise HTTPException(status_code=502, detail=result.get("message"))
    return result


@router.get("/integration-token", response_model=IntegrationTokenView)
async def get_integration_token(session: AsyncSession = Depends(get_db)):
    return await integration_token_status(session)


@router.post("/integration-token", response_model=IntegrationTokenCreated)
async def create_integration_token(session: AsyncSession = Depends(get_db)):
    """Generate or rotate the token; plaintext is returned exactly once."""

    return await rotate_integration_token(session)


@router.delete("/integration-token", response_model=IntegrationTokenView)
async def delete_integration_token(session: AsyncSession = Depends(get_db)):
    return await revoke_integration_token(session)


@router.delete("/secrets", response_model=SettingsView)
async def clear_api_credentials(session: AsyncSession = Depends(get_db)):
    """Delete every model API Key and billing AccessKey stored by TokBrain."""

    for name in (
        "dashscope_api_key",
        "bss_access_key_id",
        "bss_access_key_secret",
    ):
        await set_secret(session, name, None)
    official_bill = await session.get(AppSetting, "official_bill")
    if official_bill:
        await session.delete(official_bill)
    await session.commit()
    return await _view(session)
