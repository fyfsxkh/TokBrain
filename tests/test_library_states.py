from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import (
    Base,
    Collection,
    CollectionMembership,
    KnowledgeChunk,
    Work,
)
from app.routers_v2.library import (
    add_works_to_collection,
    collections,
    create_collection,
    retry_work,
    works,
)
from app.schemas import CollectionAssignment, CollectionCreate


async def make_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
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
