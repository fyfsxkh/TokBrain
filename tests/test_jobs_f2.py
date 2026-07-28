from types import SimpleNamespace

from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.jobs as jobs
from app.models import Base, Job, Work
from app.services.f2_links import PublicLinkError, PublicWork


async def database_factory(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'jobs.db').as_posix()}"
    )

    @event.listens_for(engine.sync_engine, "connect")
    def enable_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def test_confirmed_ingest_refreshes_f2_before_ai(tmp_path, monkeypatch):
    engine, factory = await database_factory(tmp_path)
    events: list[str] = []

    async def refresh(_client, _url):
        events.append("f2")
        return PublicWork(
            platform_work_id="7351234567890123456",
            canonical_url="https://www.douyin.com/video/7351234567890123456",
            kind="video",
            title="刷新后的标题",
            duration_seconds=12,
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

    async def reserve(*_args, **_kwargs):
        events.append("reserve")
        return SimpleNamespace(id=1)

    async def consume(*_args, **_kwargs):
        return None

    async def process(_session, work, _job_id):
        events.append("ai")
        work.processing_state = "processed"
        return "processed"

    monkeypatch.setattr(jobs, "resolve_submitted_link", refresh)
    monkeypatch.setattr(jobs, "reserve", reserve)
    monkeypatch.setattr(jobs, "consume", consume)
    monkeypatch.setattr(jobs, "process_work", process)

    async with factory() as session:
        work = Work(
            platform_work_id="7351234567890123456",
            title="待入库",
            source_url="https://www.douyin.com/video/7351234567890123456",
            library_state="pending",
            processing_state="discovered",
        )
        session.add(work)
        await session.flush()
        job = Job(
            id="job-1",
            job_type="ingest",
            state="running",
            scope={"work_ids": [work.id]},
            total_items=1,
        )
        session.add(job)
        await session.commit()
        cancelled = await jobs._ingest_works(session, job)
        await session.refresh(work)

    assert not cancelled
    assert events == ["f2", "reserve", "ai"]
    assert work.library_state == "in_library"
    assert work.title == "刷新后的标题"
    await engine.dispose()


async def test_refresh_clears_stale_full_video_when_download_is_denied(
    tmp_path, monkeypatch
):
    engine, factory = await database_factory(tmp_path)

    async def refresh(_client, _url):
        return PublicWork(
            platform_work_id="7351234567890123460",
            canonical_url="https://www.douyin.com/video/7351234567890123460",
            kind="video",
            title="禁止下载",
            download_permission="denied",
            processing_mode="subtitle_or_audio",
            audio_urls=["https://v3-web.douyinvod.com/audio.m4a"],
            media_urls=[],
            raw_metadata={
                "media_policy": {
                    "download_permission": "denied",
                    "processing_mode": "subtitle_or_audio",
                    "audio_urls": ["https://v3-web.douyinvod.com/audio.m4a"],
                }
            },
        )

    monkeypatch.setattr(jobs, "resolve_submitted_link", refresh)
    async with factory() as session:
        work = Work(
            platform_work_id="7351234567890123460",
            title="旧预览",
            source_url="https://www.douyin.com/video/7351234567890123460",
            media_urls=["https://v3-web.douyinvod.com/stale-video.mp4"],
        )
        session.add(work)
        await session.flush()

        await jobs._refresh_f2_media(session, work, object())  # type: ignore[arg-type]

        assert work.media_urls == []
        assert work.raw_metadata["media_policy"]["download_permission"] == "denied"
    await engine.dispose()


async def test_f2_refresh_failure_never_reserves_budget_or_calls_ai(
    tmp_path, monkeypatch
):
    engine, factory = await database_factory(tmp_path)
    called = False

    async def refresh(_client, _url):
        raise PublicLinkError("f2_cookie_required")

    async def forbidden(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("F2 失败后不得调用预算或 AI")

    monkeypatch.setattr(jobs, "resolve_submitted_link", refresh)
    monkeypatch.setattr(jobs, "reserve", forbidden)
    monkeypatch.setattr(jobs, "process_work", forbidden)

    async with factory() as session:
        work = Work(
            platform_work_id="7351234567890123457",
            title="待入库",
            source_url="https://www.douyin.com/video/7351234567890123457",
            library_state="pending",
            processing_state="discovered",
        )
        session.add(work)
        await session.flush()
        job = Job(
            id="job-2",
            job_type="ingest",
            state="running",
            scope={"work_ids": [work.id]},
            total_items=1,
        )
        session.add(job)
        await session.commit()
        await jobs._ingest_works(session, job)
        stored = await session.scalar(select(Work).where(Work.id == work.id))

    assert not called
    assert stored.library_state == "issues"
    assert stored.last_error_code == "f2_cookie_required"
    await engine.dispose()


async def test_processing_jobs_form_fifo_queue_instead_of_replacing_each_other(
    tmp_path,
):
    engine, factory = await database_factory(tmp_path)
    async with factory() as session:
        first = Work(
            platform_work_id="queue-1",
            title="第一批",
            library_state="pending",
            processing_state="discovered",
        )
        second = Work(
            platform_work_id="queue-2",
            title="第二批",
            library_state="pending",
            processing_state="discovered",
        )
        session.add_all([first, second])
        await session.flush()
        first_job = await jobs.enqueue_ingest_job(session, [first.id])
        second_job = await jobs.enqueue_ingest_job(session, [second.id])
        await session.commit()

        stored = list(
            (
                await session.execute(
                    select(Job)
                    .where(Job.job_type == "ingest")
                    .order_by(Job.created_at, Job.id)
                )
            ).scalars()
        )

    assert [job.id for job in stored] == [first_job.id, second_job.id]
    assert [job.state for job in stored] == ["queued", "queued"]
    assert first_job.progress["queue_position"] == 1
    assert second_job.progress["queue_position"] == 2
    await engine.dispose()


async def test_same_work_cannot_be_enqueued_twice(tmp_path):
    engine, factory = await database_factory(tmp_path)
    async with factory() as session:
        work = Work(
            platform_work_id="queue-duplicate",
            title="不能重复排队",
            library_state="pending",
            processing_state="discovered",
        )
        session.add(work)
        await session.flush()
        await jobs.enqueue_ingest_job(session, [work.id])
        try:
            await jobs.enqueue_ingest_job(session, [work.id])
        except ValueError as exc:
            assert "已在任务队列" in str(exc)
        else:
            raise AssertionError("同一作品不应重复进入任务队列")
    await engine.dispose()
