from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import and_, case, desc, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import DATA_DIR
from app.database import get_db
from app.models import (
    Collection,
    CollectionMembership,
    ImportItem,
    Job,
    KnowledgeChunk,
    Work,
    WorkSourceAsset,
    WorkSummary,
    utcnow,
)
from app.schemas import (
    CollectionAssignment,
    CollectionCreate,
    CollectionUpdate,
    IngestCreate,
    ObsidianManifestRequest,
    RetryBatchRequest,
    SummaryCreate,
    WorkSupplementUploadView,
)
from app.services.jobs import enqueue_ingest_job, enqueue_summary_job, job_to_dict
from app.services.local_assets import (
    LocalAssetError,
    store_work_supplement,
)
from app.services.summaries import (
    local_asset_names,
    obsidian_asset_name,
    obsidian_markdown,
    resolve_asset,
    safe_filename,
    summary_payload,
)


router = APIRouter(prefix="/api/library", tags=["library"])


def _safe_source_asset_path(value: str) -> Path | None:
    root = (DATA_DIR / "source-assets").resolve()
    path = Path(value).resolve()
    if root not in path.parents or not path.is_file():
        return None
    return path


async def _local_cover_path(
    session: AsyncSession,
    work: Work,
    *,
    source_images: list[WorkSourceAsset] | None = None,
) -> Path | None:
    for name in local_asset_names(work):
        path = resolve_asset(work, name)
        if path:
            return path
    if source_images is None:
        source_images = list(
            (
                await session.execute(
                    select(WorkSourceAsset)
                    .where(
                        WorkSourceAsset.work_id == work.id,
                        WorkSourceAsset.kind == "image",
                    )
                    .order_by(WorkSourceAsset.position, WorkSourceAsset.id)
                )
            ).scalars()
        )
    for asset in source_images:
        path = _safe_source_asset_path(asset.path)
        if path:
            return path
    return None


def _state_filter(value: str) -> str:
    if value not in {"pending", "in_library", "issues", "archived", "supplement"}:
        raise HTTPException(status_code=422, detail="未知的知识库状态")
    return value


async def _collection_titles(
    session: AsyncSession, work_ids: list[int]
) -> dict[int, list[str]]:
    result: dict[int, list[str]] = {work_id: [] for work_id in work_ids}
    if not work_ids:
        return result
    rows = (
        await session.execute(
            select(CollectionMembership.work_id, Collection.title)
            .join(Collection, Collection.id == CollectionMembership.collection_id)
            .where(CollectionMembership.work_id.in_(work_ids))
            .order_by(
                case((Collection.key == "manual-import", 0), else_=1),
                desc(CollectionMembership.created_at),
                Collection.title,
            )
        )
    ).all()
    for work_id, title in rows:
        result.setdefault(int(work_id), []).append(str(title))
    return result


@router.get("/collections")
async def collections(session: AsyncSession = Depends(get_db)):
    latest_membership = (
        select(
            CollectionMembership.collection_id.label("collection_id"),
            func.max(CollectionMembership.created_at).label("latest_work_added_at"),
        )
        .group_by(CollectionMembership.collection_id)
        .subquery()
    )
    groups = (
        (
            await session.execute(
                select(Collection)
                .outerjoin(
                    latest_membership,
                    latest_membership.c.collection_id == Collection.id,
                )
                .order_by(
                    case((Collection.key == "manual-import", 0), else_=1),
                    desc(latest_membership.c.latest_work_added_at),
                    desc(Collection.created_at),
                    Collection.title,
                )
            )
        )
        .scalars()
        .all()
    )
    collection_stats = (
        await session.execute(
            select(
                CollectionMembership.collection_id,
                func.count(Work.id).label("total"),
                func.sum(
                    case((Work.library_state == "in_library", 1), else_=0)
                ).label("local"),
                func.sum(
                    case((Work.library_state == "pending", 1), else_=0)
                ).label("pending"),
                func.sum(
                    case((Work.library_state == "issues", 1), else_=0)
                ).label("issues"),
                func.sum(
                    case((Work.supplement_state != "none", 1), else_=0)
                ).label("supplements"),
            )
            .select_from(CollectionMembership)
            .join(Work, Work.id == CollectionMembership.work_id)
            .group_by(CollectionMembership.collection_id)
        )
    ).all()
    stats_by_collection = {
        int(row.collection_id): {
            "total": int(row.total or 0),
            "local": int(row.local or 0),
            "pending": int(row.pending or 0),
            "issues": int(row.issues or 0),
            "supplements": int(row.supplements or 0),
        }
        for row in collection_stats
    }
    items = []
    for group in groups:
        stats = stats_by_collection.get(
            int(group.id),
            {"total": 0, "local": 0, "pending": 0, "issues": 0, "supplements": 0},
        )
        items.append(
            {
                "id": group.id,
                "key": group.key,
                "title": group.title,
                "cover_url": group.cover_url,
                "summary_prompt": group.summary_prompt,
                "item_count": stats["total"],
                "local_item_count": stats["local"],
                "pending_count": stats["pending"],
                "issue_count": stats["issues"],
                "supplement_count": stats["supplements"],
            }
        )
    global_stats = (
        await session.execute(
            select(
                *(
                    func.sum(case((Work.library_state == state, 1), else_=0)).label(
                        state
                    )
                    for state in ("pending", "in_library", "issues", "archived")
                ),
                func.sum(case((Work.supplement_state != "none", 1), else_=0)).label(
                    "supplements"
                ),
            )
        )
    ).one()
    counts = {
        state: int(getattr(global_stats, state) or 0)
        for state in ("pending", "in_library", "issues", "archived")
    }
    supplement_count = int(global_stats.supplements or 0)
    return {
        "items": items,
        "summary": {
            "candidate_count": counts["pending"],
            "selected_count": 0,
            "local_item_count": counts["in_library"],
            "issue_count": counts["issues"],
            "archived_count": counts["archived"],
            "supplement_count": supplement_count,
            "known_distinct_count": sum(counts.values()),
            "remote_folder_item_sum": 0,
        },
    }


@router.post("/collections", status_code=201)
async def create_collection(
    payload: CollectionCreate, session: AsyncSession = Depends(get_db)
):
    title = " ".join(payload.title.split()).strip()
    if not title:
        raise HTTPException(status_code=422, detail="收藏夹名称不能为空")
    duplicate = await session.scalar(
        select(Collection.id).where(func.lower(Collection.title) == title.lower())
    )
    if duplicate:
        raise HTTPException(status_code=409, detail="已存在同名收藏夹")
    sort_order = int(await session.scalar(select(func.max(Collection.sort_order))) or 0)
    group = Collection(
        key=f"local-{uuid.uuid4()}",
        title=title,
        sort_order=max(0, sort_order + 1),
    )
    session.add(group)
    await session.commit()
    await session.refresh(group)
    return {
        "id": group.id,
        "key": group.key,
        "title": group.title,
        "cover_url": group.cover_url,
        "summary_prompt": group.summary_prompt,
        "item_count": 0,
        "local_item_count": 0,
        "pending_count": 0,
        "issue_count": 0,
        "supplement_count": 0,
    }


@router.put("/collections/{collection_id}")
async def update_collection(
    collection_id: int,
    payload: CollectionUpdate,
    session: AsyncSession = Depends(get_db),
):
    group = await session.get(Collection, collection_id)
    if not group:
        raise HTTPException(status_code=404, detail="收藏夹不存在")
    prompt = str(payload.summary_prompt or "").strip()
    group.summary_prompt = prompt or None
    await session.commit()
    return {
        "id": group.id,
        "key": group.key,
        "title": group.title,
        "cover_url": group.cover_url,
        "summary_prompt": group.summary_prompt,
    }


@router.post("/collections/{collection_id}/works")
async def add_works_to_collection(
    collection_id: int,
    payload: CollectionAssignment,
    session: AsyncSession = Depends(get_db),
):
    group = await session.get(Collection, collection_id)
    if not group:
        raise HTTPException(status_code=404, detail="收藏夹不存在")
    requested = sorted(set(payload.work_ids))
    allowed = set(
        (
            await session.execute(
                select(Work.id).where(
                    Work.id.in_(requested),
                    Work.library_state.in_({"pending", "in_library"}),
                )
            )
        ).scalars()
    )
    existing = set(
        (
            await session.execute(
                select(CollectionMembership.work_id).where(
                    CollectionMembership.collection_id == collection_id,
                    CollectionMembership.work_id.in_(allowed),
                )
            )
        ).scalars()
    )
    additions = sorted(allowed - existing)
    session.add_all(
        [
            CollectionMembership(collection_id=collection_id, work_id=work_id)
            for work_id in additions
        ]
    )
    await session.commit()
    return {
        "collection_id": collection_id,
        "title": group.title,
        "requested": len(requested),
        "eligible": len(allowed),
        "added": len(additions),
    }


@router.get("/works")
async def works(
    library_state: str = Query(
        default="in_library",
        pattern="^(pending|in_library|issues|archived|supplement)$",
    ),
    collection_id: int | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=60, ge=1, le=200),
    session: AsyncSession = Depends(get_db),
):
    state = _state_filter(library_state)
    query = (
        select(Work).where(Work.supplement_state != "none")
        if state == "supplement"
        else select(Work).where(Work.library_state == state)
    )
    if state == "in_library":
        query = query.where(
            Work.processing_state == "processed",
            exists(select(KnowledgeChunk.id).where(KnowledgeChunk.work_id == Work.id)),
        )
    if collection_id is not None:
        query = query.join(
            CollectionMembership, CollectionMembership.work_id == Work.id
        ).where(CollectionMembership.collection_id == collection_id)
    total = int(
        await session.scalar(select(func.count()).select_from(query.subquery())) or 0
    )
    rows = (
        (
            await session.execute(
                query.order_by(desc(Work.updated_at), desc(Work.id))
                .offset(offset)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    work_ids = [work.id for work in rows]
    collection_map = await _collection_titles(session, work_ids)
    summaries = (
        (
            await session.execute(
                select(WorkSummary).where(WorkSummary.work_id.in_(work_ids))
            )
        )
        .scalars()
        .all()
        if work_ids
        else []
    )
    summary_map = {row.work_id: row for row in summaries}
    source_image_map: dict[int, list[WorkSourceAsset]] = {}
    if work_ids:
        source_images = (
            (
                await session.execute(
                    select(WorkSourceAsset)
                    .where(
                        WorkSourceAsset.work_id.in_(work_ids),
                        WorkSourceAsset.kind == "image",
                    )
                    .order_by(
                        WorkSourceAsset.work_id,
                        WorkSourceAsset.position,
                        WorkSourceAsset.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        for asset in source_images:
            if asset.work_id is not None:
                source_image_map.setdefault(int(asset.work_id), []).append(asset)
    items = []
    for work in rows:
        local_cover = await _local_cover_path(
            session,
            work,
            source_images=source_image_map.get(int(work.id), []),
        )
        items.append(
            {
                "id": work.id,
                "platform_work_id": work.platform_work_id,
                "kind": work.kind,
                "title": work.title,
                "author_name": work.author_name,
                "duration_seconds": work.duration_seconds,
                "cover_url": (
                    f"/api/library/works/{work.id}/cover" if local_cover else None
                ),
                "source_url": work.source_url,
                "processing_state": work.processing_state,
                "library_state": work.library_state,
                "supplement_state": work.supplement_state,
                "supplement_reason": work.supplement_reason,
                "reason": work.supplement_reason,
                "evidence_state": work.evidence_state,
                "track_report": work.track_report or {},
                "selected": False,
                "process_error": work.process_error,
                "error_code": work.last_error_code,
                "last_seen_at": work.last_seen_at,
                "collections": collection_map.get(work.id, []),
                "summary_state": (
                    summary_map[work.id].status if work.id in summary_map else "missing"
                ),
                "summary_excerpt": (
                    summary_payload(summary_map[work.id])["one_sentence"]
                    if work.id in summary_map
                    else None
                ),
            }
        )
    return {
        "items": items,
        "total": total,
        "selected_count": 0,
        "account_selected_count": 0,
        "next_offset": offset + len(items) if offset + len(items) < total else None,
    }


@router.post("/summaries/jobs")
async def create_summary_job(
    payload: SummaryCreate, session: AsyncSession = Depends(get_db)
):
    try:
        return job_to_dict(
            await enqueue_summary_job(session, work_ids=payload.work_ids)
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/ingest/jobs")
async def create_ingest_job(
    payload: IngestCreate, session: AsyncSession = Depends(get_db)
):
    try:
        job = await enqueue_ingest_job(session, work_ids=payload.work_ids)
        await session.commit()
        return job_to_dict(job)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/works/retry-batch")
async def retry_works_batch(
    payload: RetryBatchRequest, session: AsyncSession = Depends(get_db)
):
    query = select(Work).where(Work.library_state == "issues")
    if payload.work_ids:
        query = query.where(Work.id.in_(sorted(set(payload.work_ids))))
    if payload.error_code:
        query = query.where(Work.last_error_code == payload.error_code)
    if payload.collection_id is not None:
        query = query.join(
            CollectionMembership, CollectionMembership.work_id == Work.id
        ).where(CollectionMembership.collection_id == payload.collection_id)
    rows = (await session.execute(query.distinct())).scalars().all()
    for work in rows:
        work.library_state = "pending"
        work.processing_state = "discovered"
        work.process_error = None
        work.last_error_code = None
    job = (
        await enqueue_ingest_job(session, [work.id for work in rows]) if rows else None
    )
    await session.commit()
    return {
        "changed": len(rows),
        "model_called": False,
        "job": job_to_dict(job) if job else None,
    }


async def _summary_work(
    session: AsyncSession, work_id: int
) -> tuple[Work, WorkSummary, list[str]]:
    work = await session.get(Work, work_id)
    if not work or work.library_state != "in_library":
        raise HTTPException(status_code=404, detail="该作品不在本地知识库中")
    summary = (
        await session.execute(select(WorkSummary).where(WorkSummary.work_id == work.id))
    ).scalar_one_or_none()
    if not summary or summary.status != "ready":
        raise HTTPException(status_code=409, detail="这个作品还没有可用总结")
    collections = (await _collection_titles(session, [work.id])).get(work.id, [])
    return work, summary, collections


@router.get("/works/{work_id}/summary")
async def work_summary(work_id: int, session: AsyncSession = Depends(get_db)):
    work, summary, collections = await _summary_work(session, work_id)
    payload = summary_payload(summary)
    local_cover = await _local_cover_path(session, work)
    return {
        "work": {
            "id": work.id,
            "platform_work_id": work.platform_work_id,
            "title": work.title,
            "author_name": work.author_name,
            "cover_url": (
                f"/api/library/works/{work.id}/cover" if local_cover else None
            ),
            "source_url": work.source_url,
            "kind": work.kind,
            "duration_seconds": work.duration_seconds,
            "library_state": work.library_state,
            "supplement_state": work.supplement_state,
            "supplement_reason": work.supplement_reason,
            "reason": work.supplement_reason,
            "evidence_state": work.evidence_state,
            "track_report": work.track_report or {},
            "collections": collections,
        },
        "summary": {
            **payload,
            "status": summary.status,
            "generated_at": summary.generated_at,
            "model": summary.model,
        },
        "assets": [
            {
                "name": name,
                "url": f"/api/library/works/{work.id}/assets/{quote(name)}",
            }
            for name in payload.get("asset_ids") or []
            if resolve_asset(work, name)
        ],
    }


@router.get("/works/{work_id}/cover")
async def work_cover(work_id: int, session: AsyncSession = Depends(get_db)):
    work = await session.get(Work, work_id)
    if not work:
        raise HTTPException(status_code=404, detail="作品不存在")
    path = await _local_cover_path(session, work)
    if not path:
        raise HTTPException(status_code=404, detail="本地封面不存在")
    return FileResponse(
        path,
        headers={"Cache-Control": "private, max-age=86400"},
    )


@router.get("/works/{work_id}/assets/{asset_name}")
async def work_asset(
    work_id: int, asset_name: str, session: AsyncSession = Depends(get_db)
):
    work, _, _ = await _summary_work(session, work_id)
    path = resolve_asset(work, asset_name)
    if not path:
        raise HTTPException(status_code=404, detail="图片不存在")
    return FileResponse(path)


@router.post("/obsidian/manifest")
async def obsidian_manifest(
    payload: ObsidianManifestRequest, session: AsyncSession = Depends(get_db)
):
    items = []
    for work_id in list(dict.fromkeys(payload.work_ids)):
        work, summary, collections = await _summary_work(session, work_id)
        summary_data = summary_payload(summary)
        items.append(
            {
                "work_id": work.id,
                "platform_work_id": work.platform_work_id,
                "filename": safe_filename(work),
                "markdown": obsidian_markdown(work, summary, collections),
                "assets": [
                    {
                        "name": name,
                        "export_name": obsidian_asset_name(work, name),
                        "url": f"/api/library/works/{work.id}/assets/{quote(name)}",
                    }
                    for name in summary_data.get("asset_ids") or []
                    if resolve_asset(work, name)
                ],
            }
        )
    return {"items": items}


@router.get("/works/{work_id}/location")
async def work_location(
    work_id: int,
    page_size: int = Query(default=60, ge=1, le=200),
    session: AsyncSession = Depends(get_db),
):
    target = (
        await session.execute(
            select(Work.id, Work.updated_at).where(
                Work.id == work_id,
                Work.library_state == "in_library",
                Work.processing_state == "processed",
            )
        )
    ).one_or_none()
    if target is None:
        raise HTTPException(
            status_code=404, detail="该作品当前不在可检索知识库中"
        )
    eligible = (
        Work.library_state == "in_library",
        Work.processing_state == "processed",
    )
    index = int(
        await session.scalar(
            select(func.count(Work.id)).where(
                *eligible,
                or_(
                    Work.updated_at > target.updated_at,
                    and_(Work.updated_at == target.updated_at, Work.id > target.id),
                ),
            )
        )
        or 0
    )
    total = int(
        await session.scalar(select(func.count(Work.id)).where(*eligible)) or 0
    )
    return {
        "work_id": work_id,
        "index": index,
        "offset": (index // page_size) * page_size,
        "page_size": page_size,
        "total": total,
    }


@router.post("/works/{work_id}/retry")
async def retry_work(work_id: int, session: AsyncSession = Depends(get_db)):
    work = await session.get(Work, work_id)
    if not work or work.library_state not in {"pending", "issues"}:
        raise HTTPException(
            status_code=409, detail="只有待入库或处理异常的作品可以开始处理"
        )
    work.library_state = "pending"
    work.processing_state = "discovered"
    work.process_error = None
    work.last_error_code = None
    job = await enqueue_ingest_job(session, [work.id])
    await session.execute(
        update(ImportItem)
        .where(
            ImportItem.existing_work_id == work.id,
            ImportItem.status == "needs_local_file",
        )
        .values(
            status="confirmed",
            error_code=None,
            error_message=None,
            confirmed_at=utcnow(),
        )
    )
    await session.commit()
    return {"id": work_id, "library_state": "pending", "job": job_to_dict(job)}


async def _active_job_for_work(session: AsyncSession, work_id: int) -> Job | None:
    jobs = (
        (
            await session.execute(
                select(Job)
                .where(
                    Job.job_type.in_({"ingest", "summarize"}),
                    Job.state.in_({"queued", "running", "cancelling"}),
                )
                .order_by(Job.created_at)
            )
        )
        .scalars()
        .all()
    )
    for job in jobs:
        scope = job.scope if isinstance(job.scope, dict) else {}
        for value in scope.get("work_ids") or []:
            try:
                if int(value) == work_id:
                    return job
            except (TypeError, ValueError):
                continue
    return None


@router.post(
    "/works/{work_id}/supplement",
    response_model=WorkSupplementUploadView,
)
async def upload_work_supplement(
    work_id: int,
    rights_attested: bool = Form(...),
    files: list[UploadFile] = File(...),
    session: AsyncSession = Depends(get_db),
):
    active = await _active_job_for_work(session, work_id)
    if active:
        raise HTTPException(status_code=409, detail="该作品已在处理队列中")
    try:
        work, stored = await store_work_supplement(
            session,
            work_id,
            files,
            rights_attested=rights_attested,
        )
        job = await enqueue_ingest_job(session, [work.id])
        work.supplement_state = "processing"
        await session.commit()
        return {
            "id": work.id,
            "library_state": work.library_state,
            "supplement_state": work.supplement_state,
            "supplement_reason": work.supplement_reason,
            "reason": work.supplement_reason,
            "evidence_state": work.evidence_state,
            "track_report": work.track_report or {},
            **stored,
            "job": job_to_dict(job),
        }
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except LocalAssetError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/works/{work_id}/restore")
async def restore_work(work_id: int, session: AsyncSession = Depends(get_db)):
    work = await session.get(Work, work_id)
    if not work:
        raise HTTPException(status_code=404, detail="作品不存在")
    has_knowledge = bool(
        work.processing_state == "processed"
        and await session.scalar(
            select(KnowledgeChunk.id).where(KnowledgeChunk.work_id == work_id).limit(1)
        )
    )
    work.library_state = "in_library" if has_knowledge else "pending"
    work.archived_at = None
    await session.commit()
    return {"id": work_id, "library_state": work.library_state}


@router.delete("/works/{work_id}")
async def permanently_delete_work(
    work_id: int, session: AsyncSession = Depends(get_db)
):
    work = await session.get(Work, work_id)
    if not work:
        raise HTTPException(status_code=404, detail="作品不存在")
    platform_work_id = work.platform_work_id
    source_paths = list(
        (
            await session.execute(
                select(WorkSourceAsset.path).where(WorkSourceAsset.work_id == work_id)
            )
        ).scalars()
    )
    await session.delete(work)
    await session.commit()
    assets_deleted = True
    for source_path in source_paths:
        path = Path(source_path)
        root = (DATA_DIR / "source-assets").resolve()
        try:
            resolved = path.resolve()
        except OSError:
            assets_deleted = False
            continue
        if root in resolved.parents:
            try:
                resolved.unlink(missing_ok=True)
            except OSError:
                # The database deletion has already committed. A transient
                # Windows file lock must not turn that successful deletion
                # into a misleading API failure.
                assets_deleted = False
                continue
    for asset_root in (DATA_DIR / "media", DATA_DIR / "keyframes"):
        root = asset_root.resolve()
        target = (root / platform_work_id).resolve()
        if root in target.parents:
            shutil.rmtree(target, ignore_errors=True)
            if target.exists():
                assets_deleted = False
    return {"deleted": True, "id": work_id, "assets_deleted": assets_deleted}
