import asyncio
from datetime import date

import pytest
from pydantic import ValidationError
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.routers_v2.imports as import_routes
import app.services.import_queue as imports
import app.services.jobs as jobs
from app.main import app
from app.models import (
    Base,
    Collection,
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
    delete_import_item,
    remaining_daily_quota,
)
from app.services.f2_links import (
    F2AccessGate,
    PublicLinkError,
    PublicWork,
)
from app.schemas import ImportBatchCreate
from app.services.secrets import SecretUnavailableError


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


class SameWorkClient(FakeClient):
    async def resolve(self, url: str, *, cookie: str = "") -> PublicWork:
        self.calls.append(url)
        return PublicWork(
            platform_work_id="7350000000000000599",
            canonical_url="https://www.douyin.com/video/7350000000000000599",
            kind="video",
            title="同一个短链作品",
            download_permission="allowed",
            processing_mode="full_media",
            media_urls=["https://v3-web.douyinvod.com/video.mp4"],
            raw_metadata={
                "media_policy": {
                    "download_permission": "allowed",
                    "processing_mode": "full_media",
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


def test_import_batch_create_restores_manual_preview_contract():
    payload = ImportBatchCreate(text="https://v.douyin.com/a/")
    assert payload.model_dump() == {"text": "https://v.douyin.com/a/"}
    with pytest.raises(ValidationError):
        ImportBatchCreate(text="https://v.douyin.com/a/", start_processing=True)
    with pytest.raises(ValidationError):
        ImportBatchCreate(text="https://v.douyin.com/a/", auto_confirm=True)
    with pytest.raises(ValidationError):
        ImportBatchCreate(
            text="https://v.douyin.com/a/",
            target_collection_id=0,
        )

    schema = app.openapi()
    request_schema = schema["paths"]["/api/import-batches"]["post"]["requestBody"][
        "content"
    ]["application/json"]["schema"]
    assert request_schema["$ref"].endswith("/ImportBatchCreate")
    properties = schema["components"]["schemas"]["ImportBatchCreate"]["properties"]
    assert set(properties) == {"text"}


async def test_batch_limit_applies_after_duplicate_links_are_removed(tmp_path):
    engine, factory = await database_factory(tmp_path)
    async with factory() as session:
        links = [
            "https://v.douyin.com/item-0/",
            "https://v.douyin.com/item-0/?first-copy=ignored",
            "https://v.douyin.com/item-0/?second-copy=ignored",
            *[f"https://v.douyin.com/item-{index}/" for index in range(12)],
        ]
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
        job = await session.get(Job, batch.job_id)
        assert batch.total_items == 10
        assert len(items) == 10
        assert len(queued) == 10
        assert rejected == 2
        assert job.progress["duplicates"] == 3
        assert job.progress["rejected_count"] == 2
    await engine.dispose()


async def test_duplicate_link_in_same_submission_is_removed_while_unique_links_queue(
    tmp_path,
):
    engine, factory = await database_factory(tmp_path)
    async with factory() as session:
        batch, queued, rejected = await create_import_batch(
            session,
            "\n".join(
                [
                    "https://v.douyin.com/item-0/",
                    "https://v.douyin.com/item-1/",
                    "https://v.douyin.com/item-0/?tracking=ignored",
                ]
            ),
        )
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

        assert rejected == 0
        assert len(queued) == 2
        job = await session.get(Job, batch.job_id)
        assert [item.status for item in items] == ["queued", "queued"]
        assert job.progress["duplicates"] == 1
        assert await session.scalar(select(func.count(ImportBatch.id))) == 1
        assert await session.scalar(select(func.count(ImportItem.id))) == 2
        assert await session.scalar(select(func.count(Job.id))) == 1
    await engine.dispose()


async def test_create_route_reports_removed_and_queued_link_counts(
    tmp_path, monkeypatch
):
    engine, factory = await database_factory(tmp_path)
    enqueued: list[int] = []

    async def capture(item_ids: list[int]) -> None:
        enqueued.extend(item_ids)

    monkeypatch.setattr(import_routes.coordinator, "enqueue", capture)
    async with factory() as session:
        result = await import_routes.create_batch(
            ImportBatchCreate(
                text="\n".join(
                    [
                        "https://v.douyin.com/item-0/",
                        "https://v.douyin.com/item-1/",
                        "https://v.douyin.com/item-0/?tracking=ignored",
                    ]
                )
            ),
            session,
        )

    assert result["accepted_count"] == 2
    assert result["queued_count"] == 2
    assert result["duplicate_count"] == 1
    assert result["rejected_count"] == 0
    assert len(enqueued) == 2
    await engine.dispose()


async def test_same_link_in_a_later_batch_is_removed_without_new_preview_work(tmp_path):
    engine, factory = await database_factory(tmp_path)
    url = "https://www.douyin.com/video/7350000000000000501"
    async with factory() as session:
        first_batch, first_ids, _ = await create_import_batch(session, url)
        first = await session.get(ImportItem, first_ids[0])
        first.status = "ready"
        first.title = "唯一预检结果"
        await imports._refresh_batch(session, first_batch.id)
        await session.commit()

        second_batch, second_queued, _ = await create_import_batch(session, url)
        second_job = await session.get(Job, second_batch.job_id)

        assert second_queued == []
        assert second_batch.total_items == 0
        assert second_job.progress["duplicates"] == 1
        assert second_batch.state == "succeeded"
        assert second_job.state == "succeeded"
        assert await session.scalar(select(func.count(ImportBatch.id))) == 2
        assert await session.scalar(select(func.count(ImportItem.id))) == 1
        assert await session.scalar(select(func.count(Job.id))) == 2
    await engine.dispose()


async def test_later_batch_removes_previewed_link_and_queues_new_link(tmp_path):
    engine, factory = await database_factory(tmp_path)
    existing_url = "https://www.douyin.com/video/7350000000000000503"
    new_url = "https://www.douyin.com/video/7350000000000000504"
    async with factory() as session:
        first_batch, first_ids, _ = await create_import_batch(session, existing_url)
        first = await session.get(ImportItem, first_ids[0])
        first.status = "ready"
        first.title = "已预检作品"
        await imports._refresh_batch(session, first_batch.id)
        await session.commit()

        second_batch, second_queued, _ = await create_import_batch(
            session, f"{existing_url}\n{new_url}"
        )
        second_items = (
            (
                await session.execute(
                    select(ImportItem).where(ImportItem.batch_id == second_batch.id)
                )
            )
            .scalars()
            .all()
        )
        second_job = await session.get(Job, second_batch.job_id)

        assert len(second_queued) == 1
        assert len(second_items) == 1
        assert second_items[0].normalized_url == new_url
        assert second_items[0].status == "queued"
        assert second_job.progress["duplicates"] == 1
    await engine.dispose()


async def test_concurrent_same_link_submissions_queue_only_one_preview_item(tmp_path):
    engine, factory = await database_factory(tmp_path)
    url = "https://www.douyin.com/video/7350000000000000502"

    async def submit():
        async with factory() as session:
            batch, item_ids, _ = await create_import_batch(session, url)
            return batch.id, item_ids

    results = await asyncio.gather(submit(), submit())
    assert sorted(len(result[1]) for result in results) == [0, 1]

    async with factory() as session:
        items = (await session.execute(select(ImportItem))).scalars().all()
        jobs = (await session.execute(select(Job))).scalars().all()
        assert [item.status for item in items] == ["queued"]
        assert sum(int(job.progress.get("duplicates", 0)) for job in jobs) == 1
        assert await session.scalar(select(func.count(ImportBatch.id))) == 2
        assert await session.scalar(select(func.count(ImportItem.id))) == 1
        assert await session.scalar(select(func.count(Job.id))) == 2
    await engine.dispose()


async def test_different_short_links_resolving_to_same_work_keep_one_preview(
    tmp_path, monkeypatch
):
    engine, factory = await database_factory(tmp_path)
    install_test_runtime(monkeypatch, factory)
    async with factory() as session:
        first_batch, first_ids, _ = await create_import_batch(
            session, "https://v.douyin.com/short-alias-a/"
        )
        second_batch, second_ids, _ = await create_import_batch(
            session, "https://v.douyin.com/short-alias-b/"
        )

    client = SameWorkClient()
    coordinator = ImportCoordinator(client=client)
    await coordinator._process_item(first_ids[0], 1)
    await coordinator._process_item(second_ids[0], 2)

    async with factory() as session:
        first = await session.get(ImportItem, first_ids[0])
        second = await session.get(ImportItem, second_ids[0])
        first_stored_batch = await session.get(ImportBatch, first_batch.id)
        second_stored_batch = await session.get(ImportBatch, second_batch.id)

        assert first.status == "ready"
        assert first.error_code is None
        assert second.status == "duplicate"
        assert second.error_code == "duplicate_input"
        assert first.platform_work_id == second.platform_work_id
        assert first_stored_batch.state == "succeeded"
        assert second_stored_batch.state == "succeeded"
        assert await session.scalar(select(func.count(Work.id))) == 0
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


async def test_completed_preview_item_can_be_deleted_without_deleting_batch(tmp_path):
    engine, factory = await database_factory(tmp_path)
    async with factory() as session:
        batch, item_ids, _ = await create_import_batch(
            session,
            "\n".join(
                [
                    "https://www.douyin.com/video/7350000000000000401",
                    "https://www.douyin.com/video/7350000000000000402",
                ]
            ),
        )
        for item_id in item_ids:
            item = await session.get(ImportItem, item_id)
            item.status = "ready"
            item.title = f"待确认 {item_id}"
        await imports._refresh_batch(session, batch.id)
        await session.commit()

        deleted_batch_id = await delete_import_item(session, item_ids[0])
        view = await imports.batch_view(session, batch.id)
        job = await session.get(Job, batch.job_id)

        assert deleted_batch_id == batch.id
        assert await session.get(ImportItem, item_ids[0]) is None
        assert [item["id"] for item in view["items"]] == [item_ids[1]]
        assert view["total_items"] == 1
        assert job.total_items == 1
    await engine.dispose()


async def test_confirmed_preview_item_can_be_deleted_without_deleting_work(tmp_path):
    engine, factory = await database_factory(tmp_path)
    async with factory() as session:
        batch, item_ids, _ = await create_import_batch(
            session, "https://www.douyin.com/video/7350000000000000403"
        )
        item = await session.get(ImportItem, item_ids[0])
        item.status = "ready"
        item.title = "已确认待入库作品"
        await imports._refresh_batch(session, batch.id)
        await session.commit()

        confirmation = await confirm_import_items(session, batch.id, item_ids)
        work_id = confirmation["work_ids"][0]
        deleted_batch_id = await delete_import_item(session, item_ids[0])

        assert deleted_batch_id == batch.id
        assert await session.get(ImportItem, item_ids[0]) is None
        work = await session.get(Work, work_id)
        assert work is not None
        assert work.library_state == "pending"
        membership = await session.scalar(
            select(CollectionMembership).where(CollectionMembership.work_id == work_id)
        )
        assert membership is not None
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


async def test_download_denied_is_ready_for_evidence_check_and_supplement(
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


async def test_download_denied_image_post_keeps_public_images_and_is_ready(
    tmp_path, monkeypatch
):
    engine, factory = await database_factory(tmp_path)
    install_test_runtime(monkeypatch, factory)

    class DeniedImageClient:
        async def resolve(self, url: str, *, cookie: str = "") -> PublicWork:
            work_id = url.rstrip("/").split("/")[-1]
            return PublicWork(
                platform_work_id=work_id,
                canonical_url=f"https://www.douyin.com/note/{work_id}",
                kind="image",
                title="公开图文",
                download_permission="denied",
                processing_mode="full_images",
                image_urls=[
                    "https://p1.douyinpic.com/1.webp",
                    "https://p1.douyinpic.com/2.webp",
                ],
                raw_metadata={
                    "media_policy": {
                        "download_permission": "denied",
                        "processing_mode": "full_images",
                        "expected_image_count": 2,
                    }
                },
            )

    async with factory() as session:
        _batch, item_ids, _ = await create_import_batch(
            session, "https://www.douyin.com/note/7350000000000000004"
        )
    await ImportCoordinator(client=DeniedImageClient())._process_item(item_ids[0], 1)

    async with factory() as session:
        item = await session.get(ImportItem, item_ids[0])
        assert item.status == "ready"
        assert item.kind == "image"
        assert len(item.image_urls) == 2
        assert item.raw_metadata["media_policy"]["processing_mode"] == "full_images"
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


async def test_unreadable_f2_cookie_marks_item_failed_without_leaking_secret_error(
    tmp_path, monkeypatch
):
    engine, factory = await database_factory(tmp_path)
    install_test_runtime(monkeypatch, factory)

    async def unreadable_cookie(_session, _name):
        raise SecretUnavailableError("sensitive DPAPI detail")

    monkeypatch.setattr(imports, "get_secret", unreadable_cookie)
    async with factory() as session:
        batch, item_ids, _ = await create_import_batch(
            session, "https://www.douyin.com/video/7350000000000000731"
        )

    client = FakeClient()
    coordinator = ImportCoordinator(client=client)
    await coordinator._process_item(item_ids[0], 1)

    async with factory() as session:
        item = await session.get(ImportItem, item_ids[0])
        stored_batch = await session.get(ImportBatch, batch.id)
        job = await session.get(Job, stored_batch.job_id)

        assert item.status == "failed"
        assert item.worker_id is None
        assert item.error_code == "f2_cookie_unreadable"
        assert "sensitive" not in item.error_message
        assert stored_batch.state == "partial"
        assert job.state == "partial"
        assert not client.calls
        assert await session.scalar(select(func.count(DailyLinkQuota.day))) == 0
    await engine.dispose()


async def test_unexpected_preview_error_marks_failed_and_worker_survives(
    tmp_path, monkeypatch
):
    engine, factory = await database_factory(tmp_path)
    install_test_runtime(monkeypatch, factory)
    async with factory() as session:
        batch, item_ids, _ = await create_import_batch(
            session, "https://www.douyin.com/video/7350000000000000732"
        )

    coordinator = ImportCoordinator(
        client=FakeClient(error=RuntimeError("api_key=top-secret upstream detail"))
    )
    await coordinator.queue.put(item_ids[0])
    await coordinator.queue.put(None)
    await coordinator._worker(1)
    await coordinator.queue.join()

    async with factory() as session:
        item = await session.get(ImportItem, item_ids[0])
        stored_batch = await session.get(ImportBatch, batch.id)

        assert item.status == "failed"
        assert item.worker_id is None
        assert item.error_code == "preview_internal_error"
        assert "sensitive" not in item.error_message
        assert stored_batch.state == "partial"
    assert "top-secret" not in str(coordinator.last_error)
    assert "[REDACTED]" in str(coordinator.last_error)
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


async def test_stop_cancels_workers_without_waiting_for_pending_queue():
    coordinator = ImportCoordinator(client=FakeClient())
    blocker = asyncio.Event()
    coordinator.tasks = [
        asyncio.create_task(blocker.wait(), name=f"blocked-worker-{index}")
        for index in range(3)
    ]
    for item_id in range(5):
        coordinator.queue.put_nowait(item_id)

    await asyncio.wait_for(coordinator.stop(), timeout=1)

    assert coordinator.tasks == []
    assert coordinator.queue.empty()


async def test_confirmation_creates_pending_work_in_selected_collection_without_ingest_job(
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
        group = Collection(
            key="local-learning",
            title="学习",
            summary_prompt="只总结可执行步骤",
        )
        session.add(group)
        await session.commit()
        result = await confirm_import_items(
            session,
            batch.id,
            item_ids,
            collection_ids={item_ids[0]: group.id},
        )
        work = (await session.execute(select(Work))).scalar_one()
        membership = (await session.execute(select(CollectionMembership))).scalar_one()
        item = await session.get(ImportItem, item_ids[0])
        ingest_jobs = int(
            await session.scalar(
                select(func.count(Job.id)).where(Job.job_type == "ingest")
            )
            or 0
        )
        assert result == {
            "confirmed_count": 1,
            "work_ids": [work.id],
            "library_state": "pending",
        }
        assert ingest_jobs == 0
        assert work.library_state == "pending"
        assert membership.work_id == work.id
        assert membership.collection_id == group.id
        assert item.status == "confirmed"
        assert item.existing_work_id == work.id
    await engine.dispose()


async def test_confirming_cross_batch_duplicate_is_idempotent(tmp_path):
    engine, factory = await database_factory(tmp_path)
    async with factory() as session:
        first_batch, first_ids, _ = await create_import_batch(
            session, "https://www.douyin.com/video/7350000000000000601"
        )
        second_batch, second_ids, _ = await create_import_batch(
            session, "https://www.douyin.com/video/7350000000000000602"
        )
        for item_id in [first_ids[0], second_ids[0]]:
            item = await session.get(ImportItem, item_id)
            item.status = "ready"
            item.platform_work_id = "7350000000000000699"
            item.title = "同一个作品"
        await imports._refresh_batch(session, first_batch.id)
        await imports._refresh_batch(session, second_batch.id)
        await session.commit()

        first_result = await confirm_import_items(session, first_batch.id, first_ids)
        second_result = await confirm_import_items(session, second_batch.id, second_ids)
        works = (await session.execute(select(Work))).scalars().all()
        second_item = await session.get(ImportItem, second_ids[0])

        assert len(works) == 1
        assert first_result["work_ids"] == [works[0].id]
        assert second_result["work_ids"] == [works[0].id]
        assert second_result["confirmed_count"] == 1
        assert second_item.status == "confirmed"
        assert second_item.error_code is None
        assert second_item.existing_work_id == works[0].id
    await engine.dispose()
