import asyncio
from types import SimpleNamespace

from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.jobs as jobs
from app.services import content_pipeline
from app.models import Base, DailyBudget, Job, UsageEvent, Work
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
            raw_metadata={
                "import_provenance": {
                    "source_type": "link",
                    "rights_attested": True,
                    "attested_at": "2026-08-07T08:00:00+00:00",
                }
            },
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
    assert work.raw_metadata["media_policy"]["download_permission"] == "allowed"
    assert work.raw_metadata["import_provenance"] == {
        "source_type": "link",
        "rights_attested": True,
        "attested_at": "2026-08-07T08:00:00+00:00",
    }
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


async def test_enqueue_reads_active_work_set_once_for_a_batch(tmp_path, monkeypatch):
    engine, factory = await database_factory(tmp_path)
    calls = 0
    original = jobs._active_work_ids

    async def counted(session):
        nonlocal calls
        calls += 1
        return await original(session)

    monkeypatch.setattr(jobs, "_active_work_ids", counted)
    async with factory() as session:
        works = [
            Work(
                platform_work_id=f"active-set-{index}",
                title=f"作品 {index}",
                library_state="pending",
            )
            for index in range(3)
        ]
        session.add_all(works)
        await session.commit()
        await jobs.enqueue_ingest_job(session, [work.id for work in works])

    assert calls == 1
    await engine.dispose()


async def test_concurrent_enqueue_allows_only_one_active_job_per_work(tmp_path):
    engine, factory = await database_factory(tmp_path)
    async with factory() as session:
        work = Work(
            platform_work_id="concurrent-enqueue",
            title="并发入队",
            library_state="pending",
        )
        session.add(work)
        await session.commit()
        work_id = work.id

    async def enqueue():
        async with factory() as session:
            try:
                job = await jobs.enqueue_ingest_job(session, [work_id])
                return job.id
            except ValueError:
                return None

    results = await asyncio.gather(enqueue(), enqueue())
    assert sum(result is not None for result in results) == 1
    async with factory() as session:
        stored = list(
            (
                await session.execute(
                    select(Job).where(Job.job_type == "ingest")
                )
            ).scalars()
        )
        assert len(stored) == 1
        assert stored[0].scope == {"work_ids": [work_id]}
    await engine.dispose()


async def test_failed_attempt_consumes_recorded_provider_tokens(
    tmp_path, monkeypatch
):
    engine, factory = await database_factory(tmp_path)

    async def limits(_session):
        return {
            "daily_media_minutes_limit": 60.0,
            "daily_llm_token_limit": 10_000,
        }

    monkeypatch.setattr("app.services.budget.get_runtime_settings", limits)
    async with factory() as session:
        work = Work(platform_work_id="failed-usage", title="失败但已调用模型")
        job = Job(id="failed-usage-job", job_type="ingest", state="running")
        session.add_all([work, job])
        await session.flush()
        reservation = await jobs.reserve(session, works=1, llm_tokens=1_000)
        session.add(
            UsageEvent(
                job_id=job.id,
                work_id=work.id,
                model="qwen3.6-flash",
                metric="tokens",
                quantity=125,
                unit="token",
                estimated_cost_cny=0.01,
                metadata_json={},
                price_version="test",
            )
        )
        await session.commit()

        await jobs._settle_failed_attempt(
            session,
            reservation,
            job_id=job.id,
            work_id=work.id,
        )
        await session.commit()
        counter = await session.scalar(select(DailyBudget))

        assert counter is not None
        assert counter.works_reserved == 0
        assert counter.works_used == 0
        assert counter.llm_tokens_reserved == 0
        assert counter.llm_tokens_used == 125
    await engine.dispose()


async def test_job_worker_retries_after_polling_failure(monkeypatch):
    coordinator = jobs.JobCoordinator()
    calls = 0

    class Result:
        def scalar_one_or_none(self):
            return None

    class Session:
        async def execute(self, _query):
            return Result()

    class Context:
        async def __aenter__(self):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("transient database failure")
            coordinator.stop_event.set()
            return Session()

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(jobs, "async_session_factory", lambda: Context())

    await asyncio.wait_for(coordinator._loop(), timeout=2.0)

    assert calls == 2
    assert coordinator.last_error == "transient database failure"


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


async def test_in_library_and_archived_uploaded_supplements_can_be_enqueued(tmp_path):
    engine, factory = await database_factory(tmp_path)
    async with factory() as session:
        works = [
            Work(
                platform_work_id="supplement-library",
                title="在库补件",
                library_state="in_library",
                processing_state="processed",
                supplement_state="uploaded",
                evidence_state="sufficient",
            ),
            Work(
                platform_work_id="supplement-archived",
                title="归档补件",
                library_state="archived",
                processing_state="processed",
                supplement_state="uploaded",
                evidence_state="sufficient",
            ),
        ]
        session.add_all(works)
        await session.flush()

        job = await jobs.enqueue_ingest_job(session, [item.id for item in works])

        assert sorted(job.scope["work_ids"]) == sorted(item.id for item in works)
        assert all(item.supplement_state == "processing" for item in works)
    await engine.dispose()


async def test_successful_archived_supplement_keeps_archived_state(
    tmp_path, monkeypatch
):
    engine, factory = await database_factory(tmp_path)

    async def reserve(*_args, **_kwargs):
        return SimpleNamespace(works=0, media_minutes=0, llm_tokens=0)

    async def no_op(*_args, **_kwargs):
        return None

    async def process(_session, work, _job_id):
        work.processing_state = "processed"
        work.evidence_state = "sufficient"
        work.supplement_state = "none"
        work.supplement_reason = None
        return "processed"

    monkeypatch.setattr(jobs, "reserve", reserve)
    monkeypatch.setattr(jobs, "consume", no_op)
    monkeypatch.setattr(jobs, "process_work", process)

    async with factory() as session:
        work = Work(
            platform_work_id="archived-reprocess",
            title="归档作品",
            library_state="archived",
            processing_state="processed",
            supplement_state="processing",
            evidence_state="sufficient",
            refresh_policy="never",
        )
        session.add(work)
        await session.flush()
        job = Job(
            id="job-archived-supplement",
            job_type="ingest",
            state="running",
            scope={"work_ids": [work.id]},
            total_items=1,
        )
        session.add(job)
        await session.commit()

        await jobs._ingest_works(session, job)
        await session.refresh(work)

        assert work.library_state == "archived"
        assert work.supplement_state == "none"
    await engine.dispose()


async def test_ingest_commit_failure_restores_media_generations_and_settles_usage(
    tmp_path, monkeypatch
):
    engine, factory = await database_factory(tmp_path)
    consumed: list[tuple[int | None, int]] = []

    async def reserve(*_args, **_kwargs):
        return SimpleNamespace(works=1, media_minutes=0, llm_tokens=100)

    async def consume(
        _session,
        _reservation,
        *,
        actual_works=None,
        actual_llm_tokens=None,
    ):
        consumed.append((actual_works, int(actual_llm_tokens or 0)))

    async def process(session, work, job_id):
        for kind in ("media", "keyframes"):
            destination = tmp_path / kind / work.platform_work_id
            destination.mkdir(parents=True)
            (destination / "old.jpg").write_bytes(b"old")
            staging = tmp_path / "tmp" / f"{kind}-new"
            staging.mkdir(parents=True)
            (staging / "new.jpg").write_bytes(b"new")
            await content_pipeline._queue_directory_promotion(
                session, staging, destination
            )
        session.add(
            UsageEvent(
                job_id=job_id,
                work_id=work.id,
                model="qwen-test",
                metric="tokens",
                quantity=7,
                unit="token",
                estimated_cost_cny=0,
                metadata_json={"unpriced": True},
                price_version="test",
            )
        )
        work.processing_state = "processed"
        return "processed"

    monkeypatch.setattr(jobs, "reserve", reserve)
    monkeypatch.setattr(jobs, "consume", consume)
    monkeypatch.setattr(jobs, "process_work", process)

    async with factory() as session:
        work = Work(
            platform_work_id="commit-generation",
            title="commit generation",
            library_state="pending",
            processing_state="discovered",
            refresh_policy="never",
        )
        session.add(work)
        await session.flush()
        job = Job(
            id="commit-generation-job",
            job_type="ingest",
            state="running",
            scope={"work_ids": [work.id]},
            total_items=1,
        )
        session.add(job)
        await session.commit()

        original_commit = session.commit
        commit_calls = 0

        async def fail_result_commit_once():
            nonlocal commit_calls
            commit_calls += 1
            if commit_calls == 4:
                raise RuntimeError("forced result commit failure")
            await original_commit()

        monkeypatch.setattr(session, "commit", fail_result_commit_once)
        await jobs._ingest_works(session, job)

        stored_work = await session.get(Work, work.id, populate_existing=True)
        stored_usage = list(
            (
                await session.execute(
                    select(UsageEvent).where(UsageEvent.job_id == job.id)
                )
            ).scalars()
        )

    assert stored_work is not None
    assert stored_work.last_error_code == "persistence_failed"
    # The first call belongs to the transaction that is rolled back; recovery
    # reapplies the same actual usage in the durable transaction.
    assert consumed == [(None, 7), (0, 7)]
    assert len(stored_usage) == 1
    for kind in ("media", "keyframes"):
        destination = tmp_path / kind / "commit-generation"
        assert (destination / "old.jpg").read_bytes() == b"old"
        assert not (destination / "new.jpg").exists()
    await engine.dispose()


async def test_ingest_reloads_remaining_works_after_provider_failure(
    tmp_path, monkeypatch
):
    engine, factory = await database_factory(tmp_path)
    processed: list[int] = []

    async def reserve(*_args, **_kwargs):
        return SimpleNamespace(works=1, media_minutes=1, llm_tokens=100)

    async def no_op(*_args, **_kwargs):
        return None

    async def process(_session, work, _job_id):
        processed.append(work.id)
        if len(processed) == 1:
            raise RuntimeError("provider network error")
        work.processing_state = "processed"
        return "processed"

    monkeypatch.setattr(jobs, "reserve", reserve)
    monkeypatch.setattr(jobs, "release", no_op)
    monkeypatch.setattr(jobs, "consume", no_op)
    monkeypatch.setattr(jobs, "process_work", process)

    async with factory() as session:
        works = [
            Work(
                platform_work_id=f"rollback-next-{index}",
                title=f"作品 {index}",
                library_state="pending",
                processing_state="discovered",
                refresh_policy="never",
            )
            for index in range(2)
        ]
        session.add_all(works)
        await session.flush()
        job = Job(
            id="rollback-next-job",
            job_type="ingest",
            state="running",
            scope={"work_ids": [work.id for work in works]},
            total_items=2,
        )
        session.add(job)
        await session.commit()

        cancelled = await jobs._ingest_works(session, job)
        stored = [
            await session.get(Work, work.id, populate_existing=True) for work in works
        ]
        stored_job = await session.get(Job, job.id, populate_existing=True)

    assert not cancelled
    assert len(processed) == 2
    assert stored[0].last_error_code == "network_error"
    assert stored[0].library_state == "issues"
    assert stored[1].processing_state == "processed"
    assert stored[1].library_state == "in_library"
    assert stored_job.failed_items == 1
    assert stored_job.processed_items == 1
    await engine.dispose()
