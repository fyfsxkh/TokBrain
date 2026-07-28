from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas import ImportBatchCreate, ImportConfirm
from app.services.import_queue import (
    batch_view,
    cancel_import_batch,
    confirm_import_items,
    coordinator,
    create_import_batch,
)
from app.services.jobs import job_to_dict
from app.services.local_assets import LocalAssetError, store_local_assets
from app.services.f2_links import PublicLinkError


router = APIRouter(prefix="/api", tags=["imports"])


@router.post("/import-batches", status_code=status.HTTP_202_ACCEPTED)
async def create_batch(
    payload: ImportBatchCreate, session: AsyncSession = Depends(get_db)
):
    try:
        batch, item_ids, rejected_count = await create_import_batch(
            session, payload.text
        )
    except PublicLinkError as exc:
        code = 423 if exc.code == "security_cleanup_required" else 429 if exc.code in {
            "daily_limit_exceeded",
            "access_forbidden",
            "rate_limited",
            "risk_verification",
        } else 422
        raise HTTPException(
            status_code=code, detail={"code": exc.code, "message": str(exc)}
        ) from exc
    await coordinator.enqueue(item_ids)
    return {
        "batch_id": batch.id,
        "job_id": batch.job_id,
        "accepted_count": batch.total_items,
        "rejected_count": rejected_count,
    }


@router.get("/import-batches/{batch_id}")
async def get_batch(batch_id: str, session: AsyncSession = Depends(get_db)):
    try:
        return await batch_view(session, batch_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/import-batches/{batch_id}/cancel")
async def cancel_batch(batch_id: str, session: AsyncSession = Depends(get_db)):
    try:
        await cancel_import_batch(session, batch_id)
        return await batch_view(session, batch_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/import-batches/{batch_id}/confirm", status_code=202)
async def confirm_batch(
    batch_id: str,
    payload: ImportConfirm,
    session: AsyncSession = Depends(get_db),
):
    try:
        job = await confirm_import_items(session, batch_id, payload.item_ids)
        return job_to_dict(job)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/import-items/{item_id}/assets")
async def upload_assets(
    item_id: int, request: Request, session: AsyncSession = Depends(get_db)
):
    try:
        form = await request.form()
        uploads = [item for item in form.getlist("files") if hasattr(item, "read")]
        item = await store_local_assets(session, item_id, uploads)
        return {
            "item_id": item.id,
            "status": item.status,
            "kind": item.kind,
            "message": "本地补件已验证并保存",
        }
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except LocalAssetError as exc:
        raise HTTPException(
            status_code=422, detail={"code": exc.code, "message": str(exc)}
        ) from exc
