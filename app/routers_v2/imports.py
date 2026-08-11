from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import ImportBatch
from app.schemas import (
    ImportBatchCreate,
    ImportConfirm,
    ImportItemUpdate,
    LocalImportBatchCreate,
)
from app.services.import_integrations import (
    IntegrationImportError,
    create_local_import_batch,
    refresh_manifest_progress,
    update_import_item,
)
from app.services.import_queue import (
    batch_view,
    cancel_import_batch,
    confirm_import_items,
    coordinator,
    create_import_batch,
    delete_import_item,
)
from app.services.local_assets import (
    LocalAssetError,
    store_import_video,
    store_local_assets,
)
from app.services.f2_links import PublicLinkError


router = APIRouter(prefix="/api", tags=["imports"])


def _import_error_detail(
    code: str, message: str, *, retryable: bool = False, field: str | None = None
) -> dict[str, object]:
    return {
        "code": code,
        "message": message,
        "retryable": retryable,
        "field": field,
    }


@router.post("/import-batches", status_code=status.HTTP_202_ACCEPTED)
async def create_batch(
    payload: ImportBatchCreate, session: AsyncSession = Depends(get_db)
):
    try:
        batch, item_ids, rejected_count = await create_import_batch(
            session,
            payload.text,
        )
    except PublicLinkError as exc:
        code = (
            423
            if exc.code == "security_cleanup_required"
            else (
                429
                if exc.code
                in {
                    "daily_limit_exceeded",
                    "access_forbidden",
                    "rate_limited",
                    "risk_verification",
                }
                else 422
            )
        )
        raise HTTPException(
            status_code=code, detail={"code": exc.code, "message": str(exc)}
        ) from exc
    created_view = await batch_view(session, batch.id)
    await coordinator.enqueue(item_ids)
    return {
        "batch_id": batch.id,
        "job_id": batch.job_id,
        "accepted_count": batch.total_items,
        "rejected_count": rejected_count,
        "queued_count": len(item_ids),
        "duplicate_count": int(created_view["progress"].get("duplicates", 0)),
    }


@router.post("/local-import-batches", status_code=status.HTTP_201_CREATED)
async def create_local_batch(
    payload: LocalImportBatchCreate, session: AsyncSession = Depends(get_db)
):
    try:
        return await create_local_import_batch(
            session,
            rights_attested=payload.rights_attested,
            items=payload.items,
        )
    except IntegrationImportError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail()) from exc


@router.put(
    "/local-import-batches/{batch_id}/items/{item_id}/video",
    status_code=status.HTTP_200_OK,
)
async def upload_local_import_video(
    batch_id: str,
    item_id: int,
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_db),
):
    try:
        item, stored = await store_import_video(
            session, item_id, file, batch_id=batch_id
        )
        await refresh_manifest_progress(session, batch_id)
        return {
            "batch_id": batch_id,
            "item_id": item.id,
            "status": item.status,
            "kind": item.kind,
            "duration_seconds": float(stored.get("duration_seconds") or 0),
            "sha256": stored["sha256"],
            "budget_estimate": stored.get("budget_estimate"),
            "existing_work_id": item.existing_work_id,
        }
    except LookupError as exc:
        raise HTTPException(
            status_code=404,
            detail=_import_error_detail("item_not_found", str(exc)),
        ) from exc
    except LocalAssetError as exc:
        status_code = (
            413
            if exc.code == "file_too_large"
            else (
                503
                if exc.code == "ffmpeg_unavailable"
                else 409
                if exc.code in {"already_imported", "asset_conflict", "size_mismatch"}
                else 422
            )
        )
        raise HTTPException(
            status_code=status_code,
            detail=_import_error_detail(exc.code, str(exc)),
        ) from exc


@router.patch("/import-items/{item_id}")
async def patch_import_item(
    item_id: int,
    payload: ImportItemUpdate,
    session: AsyncSession = Depends(get_db),
):
    if not payload.model_fields_set:
        raise HTTPException(
            status_code=422,
            detail=_import_error_detail(
                "empty_update", "至少提供一个要修改的字段", field="body"
            ),
        )
    if "title" in payload.model_fields_set and payload.title is None:
        raise HTTPException(
            status_code=422,
            detail=_import_error_detail(
                "invalid_title", "标题不能为 null", field="title"
            ),
        )
    try:
        return await update_import_item(
            session,
            item_id=item_id,
            title=payload.title if "title" in payload.model_fields_set else None,
            description=(
                payload.description
                if "description" in payload.model_fields_set
                else None
            ),
            description_provided="description" in payload.model_fields_set,
            target_collection_id=payload.target_collection_id,
            target_collection_provided="target_collection_id"
            in payload.model_fields_set,
        )
    except IntegrationImportError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail()) from exc


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
    assignments = {item.item_id: item.collection_id for item in payload.items}
    item_ids = list(dict.fromkeys([*payload.item_ids, *assignments]))
    if not item_ids:
        raise HTTPException(status_code=422, detail="请选择至少一个待确认作品")
    stored_batch = await session.get(ImportBatch, batch_id)
    maximum = 10 if stored_batch and stored_batch.source_type == "link" else 100
    if len(item_ids) > maximum:
        raise HTTPException(status_code=422, detail=f"本批次最多确认 {maximum} 个作品")
    try:
        result = await confirm_import_items(
            session,
            batch_id,
            item_ids,
            collection_ids=assignments,
        )
        await refresh_manifest_progress(session, batch_id)
        return result
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


@router.delete("/import-items/{item_id}")
async def delete_preview_item(item_id: int, session: AsyncSession = Depends(get_db)):
    try:
        batch_id = await delete_import_item(session, item_id)
        return await batch_view(session, batch_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
