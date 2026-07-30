from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.routers_v2.library as library_routes
from app.models import (
    Base,
    Collection,
    CollectionMembership,
    Keyframe,
    KnowledgeChunk,
    Work,
    WorkSourceAsset,
    WorkSummary,
)
from app.routers_v2.library import (
    add_works_to_collection,
    collections,
    create_collection,
    permanently_delete_work,
    retry_work,
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
