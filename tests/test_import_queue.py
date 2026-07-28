from datetime import date

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.import_queue as imports
from app.models import (
    Base,
    CollectionMembership,
    DailyLinkQuota,
    ImportBatch,
    ImportItem,
    Job,
    Work,
)
from app.services.import_queue import (
    ImportCoordinator,
    _consume_link_quota,
    cancel_import_batch,
    circuit_state,
    confirm_import_items,
    create_import_batch,
    remaining_daily_quota,
)
from app.services.f2_links import (
    F2AccessGate,
    PublicLinkError,
    PublicWork,
)


async def database_factory(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}",
        connect_args={"timeout": 10},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def enable_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


class FakeClient:
    def __init__(
        self,
        *,
        media=True,
        download_permission="allowed",
        error: PublicLinkError | None = None,
    ):
        self.media = media
        self.download_permission = download_permission
        self.error = error
        self.calls: list[str] = []

    async def resolve(self, url: str, *, cookie: str = "") -> PublicWork:
        self.calls.append(url)
        if self.error:
            raise self.error
        work_id = url.rstrip("/").split("/")[-1]
        processing_mode = (
            "full_media"
            if self.download_permission == "allowed"
            else "subtitle_or_audio"
        )
        return PublicWork(
            platform_work_id=work_id,
            canonical_url=f"https://www.douyin.com/video/{work_id}",
            kind="video",
            title=f"作品 {work_id}",
            download_permission=self.download_permission,
            processing_mode=processing_mode,
            media_urls=(
                ["https://v3-web.douyinvod.com/video.mp4"]
                if self.media and self.download_permission == "allowed"
                else []
            ),
            raw_metadata={
                "media_policy": {
                    "download_permission": self.download_permission,
                    "processing_mode": processing_mode,
                }
            },
        )


async def no_sleep(_seconds: float) -> None:
    return None


def install_test_runtime(monkeypatch, factory):
    monkeypatch.setattr(imports, "async_session_factory", factory)
    monkeypatch.setattr(
        imports,
        "f2_access_gate",
        F2AccessGate(sleep=no_sleep, uniform=lambda _low, _high: 4.0),
    )


async def test_batch_limit_dedupes_normalized_url_and_reports_overflow(tmp_path):
    engine, factory = await database_factory(tmp_path)
    async with factory() as session:
        links = [f"https://v.douyin.com/item-{index}/" for index in range(9)]
        links.append("https://v.douyin.com/item-0/?tracking=ignored")
        links.extend(
            ["https://v.douyin.com/overflow-1", "https://v.douyin.com/overflow-2"]
        )
        batch, queued, rejected = await create_import_batch(session, "\n".join(links))
        items = (
            (
                await session.execute(
                    select(ImportItem).where(ImportItem.batch_id == batch.id)
                )
            )
            .scalars()
            .all()
        )
        duplicate = [item for item in items if item.status == "duplicate"]
        job = await session.get(Job, batch.job_id)
        assert batch.total_items == 10
        assert len(items) == 10
        assert len(queued) == 9
        assert rejected == 2
        assert len(duplicate) == 1
        assert duplicate[0].error_code == "duplicate_input"
        assert job.progress["rejected_count"] == 2
    await engine.dispose()


async def test_direct_existing_work_skips_network_and_daily_quota(
    tmp_path, monkeypatch
):
    engine, factory = await database_factory(tmp_path)
    install_test_runtime(monkeypatch, factory)
    work_id = "7351234567890123456"
    async with factory() as session:
        session.add(
            Work(
                platform_work_id=work_id,
                title="已经存在",
                library_state="in_library",
                processing_state="processed",
            )
        )
        await session.commit()
        batch, item_ids, _ = await create_import_batch(
            session, f"https://www.douyin.com/video/{work_id}?from=share"
        )
    client = FakeClient()
    coordinator = ImportCoordinator(client=client)
    await coordinator._process_item(item_ids[0], 1)
    async with factory() as session:
        item = await session.get(ImportItem, item_ids[0])
        assert item.status == "duplicate"
        assert item.error_code == "already_imported"
        assert not client.calls
        assert await session.scalar(select(func.count(DailyLinkQuota.day))) == 0
    await engine.dispose()


async def test_preview_is_persisted_without_creating_work_or_ai_job(
    tmp_path, monkeypatch
):
    engine, factory = await database_factory(tmp_path)
    install_test_runtime(monkeypatch, factory)
    async with factory() as session:
        batch, item_ids, _ = await create_import_batch(
            session, "https://www.douyin.com/video/7350000000000000001"
        )
    coordinator = ImportCoordinator(client=FakeClient())
    await coordinator._process_item(item_ids[0], 2)
    async with factory() as session:
        item = await session.get(ImportItem, item_ids[0])
        stored_batch = await session.get(ImportBatch, batch.id)
        assert item.status == "ready"
        assert item.worker_id is None
        assert stored_batch.state == "succeeded"
        assert await session.scalar(select(func.count(Work.id))) == 0
        assert (
            await session.scalar(
                select(func.count(Job.id)).where(Job.job_type != "link_preview")
            )
            == 0
        )
        assert (
            await session.get(DailyLinkQuota, imports.shanghai_day())
        ).attempted == 1
    await engine.dispose()


async def test_media_missing_can_wait_for_local_file(tmp_path, monkeypatch):
    engine, factory = await database_factory(tmp_path)
    install_test_runtime(monkeypatch, factory)
    async with factory() as session:
        _batch, item_ids, _ = await create_import_batch(
            session, "https://www.douyin.com/video/7350000000000000002"
        )
    coordinator = ImportCoordinator(client=FakeClient(media=False))
    await coordinator._process_item(item_ids[0], 1)
    async with factory() as session:
        item = await session.get(ImportItem, item_ids[0])
        assert item.status == "needs_local_file"
        assert item.error_code == "media_missing"
    await engine.dispose()


async def test_download_denied_is_ready_for_metadata_only_ingestion(
    tmp_path, monkeypatch
):
    engine, factory = await database_factory(tmp_path)
    install_test_runtime(monkeypatch, factory)
    async with factory() as session:
        batch, item_ids, _ = await create_import_batch(
            session, "https://www.douyin.com/video/7350000000000000003"
        )
    coordinator = ImportCoordinator(
        client=FakeClient(media=True, download_permission="denied")
    )
    await coordinator._process_item(item_ids[0], 1)
    async with factory() as session:
        item = await session.get(ImportItem, item_ids[0])
        assert item.status == "ready"
        assert item.media_urls == []
        assert item.raw_metadata["media_policy"]["processing_mode"] == (
            "subtitle_or_audio"
        )
        view = await imports.batch_view(session, batch.id)
        assert view["items"][0]["download_permission"] == "denied"
        assert view["items"][0]["processing_mode"] == "subtitle_or_audio"
    await engine.dispose()


async def test_cancel_preserves_ready_items_and_cancels_only_not_started(tmp_path):
    engine, factory = await database_factory(tmp_path)
    async with factory() as session:
        batch, item_ids, _ = await create_import_batch(
            session,
            "\n".join(
                f"https://www.douyin.com/video/735000000000000000{index}"
                for index in range(3)
            ),
        )
        first = await session.get(ImportItem, item_ids[0])
        first.status = "ready"
        first.title = "已完成"
        first.media_urls = ["https://v3-web.douyinvod.com/video.mp4"]
        await session.commit()
        await cancel_import_batch(session, batch.id)
        items = (
            (
                await session.execute(
                    select(ImportItem)
                    .where(ImportItem.batch_id == batch.id)
                    .order_by(ImportItem.ordinal)
                )
            )
            .scalars()
            .all()
        )
        assert items[0].status == "ready"
        assert [item.status for item in items[1:]] == ["cancelled", "cancelled"]
        assert all(item.error_code == "cancelled_by_user" for item in items[1:])
        assert (await session.get(ImportBatch, batch.id)).state == "cancelled"
    await engine.dispose()


async def test_risk_error_blocks_remaining_batch_and_persists_circuit(
    tmp_path, monkeypatch
):
    engine, factory = await database_factory(tmp_path)
    install_test_runtime(monkeypatch, factory)
    async with factory() as session:
        batch, item_ids, _ = await create_import_batch(
            session,
            "\n".join(
                [
                    "https://www.douyin.com/video/7350000000000000101",
                    "https://www.douyin.com/video/7350000000000000102",
                ]
            ),
        )
    coordinator = ImportCoordinator(
        client=FakeClient(error=PublicLinkError("rate_limited", opens_circuit=True))
    )
    await coordinator._process_item(item_ids[0], 1)
    async with factory() as session:
        items = (
            (
                await session.execute(
                    select(ImportItem)
                    .where(ImportItem.batch_id == batch.id)
                    .order_by(ImportItem.ordinal)
                )
            )
            .scalars()
            .all()
        )
        state = await circuit_state(session)
        assert items[0].status == "failed"
        assert items[0].error_code == "rate_limited"
        assert items[1].status == "blocked"
        assert state["active"]
        assert state["error_code"] == "rate_limited"
        with pytest.raises(PublicLinkError) as captured:
            await create_import_batch(
                session, "https://www.douyin.com/video/7350000000000000103"
            )
        assert captured.value.code == "rate_limited"
    await engine.dispose()


async def test_daily_quota_is_atomic_at_limit_and_resets_by_shanghai_day(
    tmp_path, monkeypatch
):
    engine, factory = await database_factory(tmp_path)
    first_day = date(2026, 7, 26)
    monkeypatch.setattr(imports, "shanghai_day", lambda: first_day)
    async with factory() as session:
        session.add(DailyLinkQuota(day=first_day, attempted=149))
        await session.commit()
        await _consume_link_quota(session)
        await session.commit()
        assert await remaining_daily_quota(session) == 0
        with pytest.raises(PublicLinkError) as captured:
            await _consume_link_quota(session)
        assert captured.value.code == "daily_limit_exceeded"
        await session.rollback()
    second_day = date(2026, 7, 27)
    monkeypatch.setattr(imports, "shanghai_day", lambda: second_day)
    async with factory() as session:
        assert await remaining_daily_quota(session) == 150
        await _consume_link_quota(session)
        await session.commit()
        assert await remaining_daily_quota(session) == 149
    await engine.dispose()


async def test_startup_always_launches_three_workers_and_never_resumes_access(
    tmp_path, monkeypatch
):
    engine, factory = await database_factory(tmp_path)
    install_test_runtime(monkeypatch, factory)
    async with factory() as session:
        batch, item_ids, _ = await create_import_batch(
            session,
            "\n".join(
                [
                    "https://www.douyin.com/video/7350000000000000201",
                    "https://www.douyin.com/video/7350000000000000202",
                ]
            ),
        )
        ready = await session.get(ImportItem, item_ids[0])
        ready.status = "ready"
        ready.title = "保留结果"
        ready.media_urls = ["https://v3-web.douyinvod.com/video.mp4"]
        await session.commit()
    client = FakeClient()
    coordinator = ImportCoordinator(client=client)
    await coordinator.start()
    assert len(coordinator.tasks) == 3
    await coordinator.stop()
    async with factory() as session:
        items = (
            (
                await session.execute(
                    select(ImportItem)
                    .where(ImportItem.batch_id == batch.id)
                    .order_by(ImportItem.ordinal)
                )
            )
            .scalars()
            .all()
        )
        assert items[0].status == "ready"
        assert items[1].status == "cancelled"
        assert (await session.get(ImportBatch, batch.id)).state == "cancelled"
        assert not client.calls
    await engine.dispose()


async def test_confirmation_creates_manual_group_work_and_ingest_job(
    tmp_path, monkeypatch
):
    engine, factory = await database_factory(tmp_path)
    install_test_runtime(monkeypatch, factory)
    async with factory() as session:
        batch, item_ids, _ = await create_import_batch(
            session, "https://www.douyin.com/video/7350000000000000301"
        )
    coordinator = ImportCoordinator(client=FakeClient())
    await coordinator._process_item(item_ids[0], 3)
    async with factory() as session:
        job = await confirm_import_items(session, batch.id, item_ids)
        work = (await session.execute(select(Work))).scalar_one()
        membership = (await session.execute(select(CollectionMembership))).scalar_one()
        item = await session.get(ImportItem, item_ids[0])
        assert job.job_type == "ingest"
        assert work.library_state == "pending"
        assert membership.work_id == work.id
        assert item.status == "confirmed"
        assert item.existing_work_id == work.id
    await engine.dispose()
