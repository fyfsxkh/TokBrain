import io
from datetime import timedelta

from fastapi import UploadFile
from PIL import Image
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.routers_v2.library as library_routes
import app.services.local_assets as local_assets
from app.models import (
    Base,
    Collection,
    CollectionMembership,
    Keyframe,
    KnowledgeChunk,
    Work,
    WorkSourceAsset,
    WorkSummary,
    utcnow,
)
from app.routers_v2.library import (
    add_works_to_collection,
    collections,
    create_collection,
    permanently_delete_work,
    retry_work,
    upload_work_supplement,
    work_location,
    works,
)
from app.schemas import CollectionAssignment, CollectionCreate


async def make_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def enable_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, factory()


async def test_library_has_one_local_space_and_four_states():
    engine, session = await make_session()
    group = Collection(key="manual-import", title="手动导入")
    session.add(group)
    await session.flush()
    states = ["pending", "in_library", "issues", "archived"]
    for index, state in enumerate(states):
        work = Work(
            platform_work_id=f"state-{index}",
            title=state,
            library_state=state,
            processing_state="processed" if state == "in_library" else "discovered",
        )
        session.add(work)
        await session.flush()
        session.add(CollectionMembership(collection_id=group.id, work_id=work.id))
        if state == "in_library":
            session.add(
                KnowledgeChunk(
                    work_id=work.id,
                    chunk_index=0,
                    source_kind="metadata",
                    text="有效知识",
                )
            )
    await session.commit()
    payload = await collections(session)
    assert payload["summary"] == {
        "candidate_count": 1,
        "selected_count": 0,
        "local_item_count": 1,
        "issue_count": 1,
        "archived_count": 1,
        "supplement_count": 0,
        "known_distinct_count": 4,
        "remote_folder_item_sum": 0,
    }
    for state in states:
        page = await works(
            library_state=state,
            collection_id=None,
            offset=0,
            limit=60,
            session=session,
        )
        assert page["total"] == 1
        assert page["items"][0]["library_state"] == state
    await session.close()
    await engine.dispose()


async def test_work_location_counts_rank_without_loading_every_work_id():
    engine, session = await make_session()
    now = utcnow()
    entries = [
        Work(
            platform_work_id=f"location-{index}",
            title=f"作品 {index}",
            library_state="in_library",
            processing_state="processed",
            updated_at=now - timedelta(minutes=index),
        )
        for index in range(3)
    ]
    session.add_all(entries)
    await session.commit()

    result = await work_location(entries[1].id, page_size=2, session=session)

    assert result == {
        "work_id": entries[1].id,
        "index": 1,
        "offset": 0,
        "page_size": 2,
        "total": 3,
    }
    await session.close()
    await engine.dispose()


async def test_collections_keep_manual_first_then_sort_by_latest_membership():
    engine, session = await make_session()
    manual = Collection(key="manual-import", title="手动导入", sort_order=-1)
    older = Collection(key="older", title="较早收藏夹")
    newer = Collection(key="newer", title="最近收藏夹")
    empty = Collection(key="empty", title="空收藏夹")
    session.add_all([manual, older, newer, empty])
    await session.flush()
    now = utcnow()
    works_to_add = [
        (manual, "manual", now - timedelta(days=10)),
        (older, "older", now - timedelta(days=2)),
        (newer, "newer", now - timedelta(hours=1)),
    ]
    for group, platform_work_id, added_at in works_to_add:
        work = Work(platform_work_id=platform_work_id, title=platform_work_id)
        session.add(work)
        await session.flush()
        session.add(
            CollectionMembership(
                collection_id=group.id,
                work_id=work.id,
                created_at=added_at,
            )
        )
    await session.commit()

    payload = await collections(session)

    assert [item["key"] for item in payload["items"]] == [
        "manual-import",
        "newer",
        "older",
        "empty",
    ]
    await session.close()
    await engine.dispose()


async def test_supplement_is_an_independent_virtual_library_state():
    engine, session = await make_session()
    searchable = Work(
        platform_work_id="restricted-with-subtitle",
        title="已有字幕但缺画面",
        library_state="in_library",
        processing_state="processed",
        supplement_state="required",
        supplement_reason="full_video_unavailable",
        evidence_state="sufficient",
        track_report={"subtitle": {"valid": True}, "video": {"available": False}},
    )
    insufficient = Work(
        platform_work_id="restricted-title-only",
        title="只有标题",
        library_state="in_library",
        processing_state="processed",
        supplement_state="required",
        supplement_reason="full_video_unavailable",
        evidence_state="insufficient",
        track_report={"video": {"available": False}},
    )
    session.add_all([searchable, insufficient])
    await session.flush()
    session.add(
        KnowledgeChunk(
            work_id=searchable.id,
            chunk_index=0,
            source_kind="subtitle",
            text="有效字幕内容",
        )
    )
    await session.commit()

    page = await works(
        library_state="supplement",
        collection_id=None,
        offset=0,
        limit=60,
        session=session,
    )
    assert page["total"] == 2
    assert {item["library_state"] for item in page["items"]} == {"in_library"}
    assert {item["supplement_state"] for item in page["items"]} == {"required"}
    assert {item["evidence_state"] for item in page["items"]} == {
        "sufficient",
        "insufficient",
    }
    assert all("track_report" in item for item in page["items"])
    summary = await collections(session)
    assert summary["summary"]["supplement_count"] == 2
    assert summary["summary"]["local_item_count"] == 2
    await session.close()
    await engine.dispose()


async def test_work_supplement_upload_preserves_main_state_and_queues_reprocess(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(local_assets, "DATA_DIR", tmp_path)
    engine, session = await make_session()
    work = Work(
        platform_work_id="partial-images",
        kind="image",
        title="图片不完整",
        library_state="in_library",
        processing_state="processed",
        supplement_state="required",
        supplement_reason="image_set_incomplete",
        evidence_state="sufficient",
    )
    session.add(work)
    await session.flush()
    session.add(
        KnowledgeChunk(
            work_id=work.id,
            chunk_index=0,
            source_kind="ocr",
            text="已有图片文字",
        )
    )
    await session.commit()
    image_data = io.BytesIO()
    Image.new("RGB", (12, 12), (20, 120, 220)).save(image_data, format="PNG")
    upload = UploadFile(filename="complete.png", file=io.BytesIO(image_data.getvalue()))

    response = await upload_work_supplement(
        work.id,
        rights_attested=True,
        files=[upload],
        session=session,
    )

    await session.refresh(work)
    assert response["library_state"] == "in_library"
    assert response["supplement_state"] == "processing"
    assert response["evidence_state"] == "unverified"
    assert response["job"]["job_type"] == "ingest"
    assert work.library_state == "in_library"
    assert work.supplement_state == "processing"
    assert await session.scalar(
        select(WorkSourceAsset.id).where(WorkSourceAsset.work_id == work.id)
    )
    await session.close()
    await engine.dispose()


async def test_in_library_without_knowledge_is_not_exposed_as_searchable():
    engine, session = await make_session()
    session.add(
        Work(
            platform_work_id="invalid-in-library",
            title="缺少知识块",
            library_state="in_library",
            processing_state="processed",
        )
    )
    await session.commit()
    page = await works(
        library_state="in_library",
        collection_id=None,
        offset=0,
        limit=60,
        session=session,
    )
    assert page["total"] == 0
    await session.close()
    await engine.dispose()


async def test_pending_or_issue_work_can_create_ingest_job():
    engine, session = await make_session()
    work = Work(
        platform_work_id="pending-retry",
        title="待处理",
        library_state="pending",
        processing_state="discovered",
    )
    session.add(work)
    await session.commit()
    response = await retry_work(work.id, session=session)
    assert response["job"]["job_type"] == "ingest"
    assert response["job"]["state"] == "queued"
    refreshed = (await session.execute(select(Work))).scalar_one()
    assert refreshed.library_state == "pending"
    await session.close()
    await engine.dispose()


async def test_permanent_delete_removes_completed_knowledge_and_local_assets(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(library_routes, "DATA_DIR", tmp_path)
    source_root = tmp_path / "source-assets"
    media_root = tmp_path / "media" / "delete-completed"
    keyframe_root = tmp_path / "keyframes" / "delete-completed"
    source_root.mkdir(parents=True)
    media_root.mkdir(parents=True)
    keyframe_root.mkdir(parents=True)
    source_path = source_root / "uploaded.mp4"
    source_path.write_bytes(b"local source")
    (media_root / "image.jpg").write_bytes(b"derived image")
    keyframe_path = keyframe_root / "frame.jpg"
    keyframe_path.write_bytes(b"keyframe")

    engine, session = await make_session()
    work = Work(
        platform_work_id="delete-completed",
        title="已完成总结作品",
        library_state="in_library",
        processing_state="processed",
    )
    session.add(work)
    await session.flush()
    session.add_all(
        [
            WorkSummary(
                work_id=work.id,
                one_sentence="待删除总结",
                content_json={"outline": ["待删除"]},
                tags=["测试"],
                asset_ids=["frame.jpg"],
            ),
            KnowledgeChunk(
                work_id=work.id,
                chunk_index=0,
                source_kind="summary",
                text="待删除索引内容",
                embedding=[0.1, 0.2],
            ),
            Keyframe(
                work_id=work.id,
                timestamp_seconds=1.0,
                path=str(keyframe_path),
            ),
            WorkSourceAsset(
                work_id=work.id,
                kind="video",
                path=str(source_path),
                mime_type="video/mp4",
                size_bytes=source_path.stat().st_size,
                sha256="a" * 64,
            ),
        ]
    )
    await session.commit()
    work_id = work.id

    response = await permanently_delete_work(work_id, session=session)

    assert response == {
        "deleted": True,
        "id": work_id,
        "assets_deleted": True,
    }
    assert await session.get(Work, work_id) is None
    for model in (WorkSummary, KnowledgeChunk, Keyframe, WorkSourceAsset):
        assert await session.scalar(select(func.count()).select_from(model)) == 0
    assert not source_path.exists()
    assert not media_root.exists()
    assert not keyframe_root.exists()
    await session.close()
    await engine.dispose()


async def test_permanent_delete_stays_successful_when_source_asset_is_locked(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(library_routes, "DATA_DIR", tmp_path)
    source_root = tmp_path / "source-assets"
    source_root.mkdir(parents=True)
    source_path = source_root / "locked.mp4"
    source_path.write_bytes(b"locked source")

    engine, session = await make_session()
    work = Work(
        platform_work_id="delete-locked",
        title="本地文件被占用",
        library_state="in_library",
        processing_state="processed",
    )
    session.add(work)
    await session.flush()
    session.add(
        WorkSourceAsset(
            work_id=work.id,
            kind="video",
            path=str(source_path),
            mime_type="video/mp4",
            size_bytes=source_path.stat().st_size,
            sha256="b" * 64,
        )
    )
    await session.commit()
    work_id = work.id
    original_unlink = type(source_path).unlink

    def locked_unlink(path, *args, **kwargs):
        if path == source_path:
            raise PermissionError("file is in use")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(type(source_path), "unlink", locked_unlink)
    response = await permanently_delete_work(work_id, session=session)

    assert response == {
        "deleted": True,
        "id": work_id,
        "assets_deleted": False,
    }
    assert await session.get(Work, work_id) is None
    assert source_path.exists()
    await session.close()
    await engine.dispose()


async def test_local_collection_is_shared_by_pending_and_in_library_states():
    engine, session = await make_session()
    pending = Work(
        platform_work_id="collection-pending",
        title="待处理作品",
        library_state="pending",
        processing_state="discovered",
    )
    ready = Work(
        platform_work_id="collection-ready",
        title="在库作品",
        library_state="in_library",
        processing_state="processed",
    )
    session.add_all([pending, ready])
    await session.flush()
    session.add(
        KnowledgeChunk(
            work_id=ready.id,
            chunk_index=0,
            source_kind="metadata",
            text="可检索内容",
        )
    )
    await session.commit()

    group = await create_collection(CollectionCreate(title="数学"), session=session)
    result = await add_works_to_collection(
        group["id"],
        CollectionAssignment(work_ids=[pending.id, ready.id]),
        session=session,
    )
    assert result["added"] == 2

    pending_page = await works(
        library_state="pending",
        collection_id=group["id"],
        offset=0,
        limit=60,
        session=session,
    )
    ready_page = await works(
        library_state="in_library",
        collection_id=group["id"],
        offset=0,
        limit=60,
        session=session,
    )
    assert [item["title"] for item in pending_page["items"]] == ["待处理作品"]
    assert [item["title"] for item in ready_page["items"]] == ["在库作品"]

    pending.library_state = "in_library"
    pending.processing_state = "processed"
    session.add(
        KnowledgeChunk(
            work_id=pending.id,
            chunk_index=0,
            source_kind="metadata",
            text="处理完成后仍在原收藏夹",
        )
    )
    await session.commit()
    transitioned = await works(
        library_state="in_library",
        collection_id=group["id"],
        offset=0,
        limit=60,
        session=session,
    )
    assert {item["title"] for item in transitioned["items"]} == {
        "待处理作品",
        "在库作品",
    }
    await session.close()
    await engine.dispose()
