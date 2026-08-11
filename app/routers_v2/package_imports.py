"""Same-origin browser API for folder/ZIP video data packages."""

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.schemas import PackageImportBatchCreate
from app.services.import_integrations import IntegrationImportError
from app.services.package_imports import (
    create_package_batch,
    package_batch_view,
    queue_package_analysis,
    upload_package_file,
)


router = APIRouter(prefix="/api/package-import-batches", tags=["package-imports"])


def _same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin not in {
        *settings.allowed_origins,
        "http://testserver",
        "https://testserver",
    }:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "same_origin_required",
                "message": "该上传接口只能由 TokBrain 本机网页调用",
                "retryable": False,
                "field": "Origin",
            },
        )


def _raise(exc: IntegrationImportError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.detail()) from exc


@router.post("", status_code=201)
async def create_batch(
    payload: PackageImportBatchCreate,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    _same_origin(request)
    try:
        return await create_package_batch(
            session,
            rights_attested=payload.rights_attested,
            upload_mode=payload.upload_mode,
            target_collection_id=payload.target_collection_id,
            files=payload.files,
        )
    except IntegrationImportError as exc:
        _raise(exc)


@router.get("/{batch_id}")
async def get_batch(batch_id: str, session: AsyncSession = Depends(get_db)):
    try:
        return await package_batch_view(session, batch_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put("/{batch_id}/files/{file_id}")
async def upload_file(
    batch_id: str,
    file_id: str,
    request: Request,
    file: UploadFile = File(...),
    replace: bool = Query(False),
    session: AsyncSession = Depends(get_db),
):
    _same_origin(request)
    try:
        return await upload_package_file(
            session,
            batch_id=batch_id,
            file_id=file_id,
            upload=file,
            replace=replace,
        )
    except IntegrationImportError as exc:
        _raise(exc)


@router.post("/{batch_id}/analyze", status_code=202)
async def analyze_batch(
    batch_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    _same_origin(request)
    try:
        return await queue_package_analysis(session, batch_id)
    except IntegrationImportError as exc:
        _raise(exc)
