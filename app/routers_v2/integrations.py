"""Versioned, token-authenticated loopback API for external import tools."""

from __future__ import annotations

from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    Security,
    UploadFile,
    status,
)
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.database import get_db
from app.schemas import (
    ExternalAssetUploadView,
    ExternalCommitView,
    ExternalImportBatchCreate,
    ExternalImportBatchCreated,
    ExternalImportBatchView,
    ExternalImportCommit,
)
from app.services.import_integrations import (
    MAX_EXTERNAL_REQUEST_BYTES,
    IntegrationImportError,
    commit_external_batch,
    create_external_import_batch,
    external_batch_view,
    external_item_by_client_id,
    refresh_manifest_progress,
    verify_integration_token,
)
from app.services.local_assets import LocalAssetError, store_import_video


router = APIRouter(prefix="/api/integrations/v1", tags=["external-import-v1"])
_bearer = HTTPBearer(
    auto_error=False,
    bearerFormat="TokBrain external import token",
    scheme_name="ExternalImportToken",
)


def _detail(
    code: str, message: str, *, retryable: bool = False, field: str | None = None
) -> dict[str, object]:
    return {
        "code": code,
        "message": message,
        "retryable": retryable,
        "field": field,
    }


def _raise_import_error(exc: IntegrationImportError) -> None:
    headers = {"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None
    raise HTTPException(
        status_code=exc.status_code,
        detail=exc.detail(),
        headers=headers,
    ) from exc


async def integration_session(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
    session: AsyncSession = Depends(get_db),
) -> AsyncSession:
    try:
        authorization = (
            f"{credentials.scheme} {credentials.credentials}" if credentials else None
        )
        await verify_integration_token(session, authorization)
    except IntegrationImportError as exc:
        _raise_import_error(exc)
    is_manifest_create = (
        request.method == "POST"
        and request.url.path == "/api/integrations/v1/import-batches"
    )
    content_length = request.headers.get("content-length")
    if is_manifest_create and content_length:
        try:
            length = int(content_length)
        except ValueError:
            length = 0
        if length > MAX_EXTERNAL_REQUEST_BYTES:
            raise HTTPException(
                status_code=413,
                detail=_detail(
                    "request_too_large", "JSON 清单超过 2 MB 上限", field="body"
                ),
            )
    if is_manifest_create and len(await request.body()) > MAX_EXTERNAL_REQUEST_BYTES:
        raise HTTPException(
            status_code=413,
            detail=_detail(
                "request_too_large", "JSON 清单超过 2 MB 上限", field="body"
            ),
        )
    return session


@router.post(
    "/import-batches",
    status_code=status.HTTP_201_CREATED,
    response_model=ExternalImportBatchCreated,
)
async def create_batch(
    payload: ExternalImportBatchCreate,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    session: AsyncSession = Depends(integration_session),
):
    if not idempotency_key:
        raise HTTPException(
            status_code=422,
            detail=_detail(
                "idempotency_key_required",
                "创建批次必须提供 Idempotency-Key",
                field="Idempotency-Key",
            ),
        )
    try:
        return await create_external_import_batch(
            session,
            idempotency_key=idempotency_key,
            rights_attested=payload.rights_attested,
            items=payload.items,
        )
    except IntegrationImportError as exc:
        _raise_import_error(exc)


@router.get("/import-batches/{batch_id}", response_model=ExternalImportBatchView)
async def get_batch(
    batch_id: str, session: AsyncSession = Depends(integration_session)
):
    try:
        return await external_batch_view(session, batch_id)
    except IntegrationImportError as exc:
        _raise_import_error(exc)


@router.put(
    "/import-batches/{batch_id}/items/{client_item_id}/asset",
    response_model=ExternalAssetUploadView,
)
async def upload_asset(
    batch_id: str,
    client_item_id: str,
    file: UploadFile = File(...),
    replace: bool = Query(default=False),
    session: AsyncSession = Depends(integration_session),
):
    try:
        item = await external_item_by_client_id(
            session, batch_id=batch_id, client_item_id=client_item_id
        )
        metadata = dict(item.raw_metadata or {})
        expected_sha256 = (
            str(
                dict(metadata.get("external_import") or {}).get("expected_sha256") or ""
            )
            or None
        )
        item, stored = await store_import_video(
            session,
            item.id,
            file,
            batch_id=batch_id,
            expected_sha256=expected_sha256,
            replace=replace,
        )
        await refresh_manifest_progress(session, batch_id)
        return {
            "batch_id": batch_id,
            "client_item_id": client_item_id,
            "item_id": item.id,
            "status": item.status,
            "duration_seconds": float(stored.get("duration_seconds") or 0),
            "sha256": stored["sha256"],
            "budget_estimate": stored.get("budget_estimate"),
            "idempotent": bool(stored.get("idempotent")),
        }
    except IntegrationImportError as exc:
        _raise_import_error(exc)
    except LookupError as exc:
        raise HTTPException(
            status_code=404,
            detail=_detail("item_not_found", str(exc)),
        ) from exc
    except LocalAssetError as exc:
        status_code = (
            413
            if exc.code == "file_too_large"
            else (
                503
                if exc.code == "ffmpeg_unavailable"
                else 409 if exc.code in {"asset_conflict", "already_imported"} else 422
            )
        )
        raise HTTPException(
            status_code=status_code,
            detail=_detail(
                exc.code,
                str(exc),
                retryable=exc.code in {"upload_interrupted"},
                field="asset",
            ),
        ) from exc


@router.post("/import-batches/{batch_id}/commit", response_model=ExternalCommitView)
async def commit_batch(
    batch_id: str,
    payload: ExternalImportCommit,
    session: AsyncSession = Depends(integration_session),
):
    try:
        return await commit_external_batch(
            session,
            batch_id=batch_id,
            start_processing=payload.start_processing,
        )
    except IntegrationImportError as exc:
        _raise_import_error(exc)
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail=_detail("commit_conflict", str(exc)),
        ) from exc
