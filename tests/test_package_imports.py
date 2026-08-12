import asyncio
import io
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest
import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.package_imports as packages
from app.models import (
    Base,
    DailyLinkQuota,
    ImportBatch,
    ImportItem,
    PackageImportFile,
    Work,
    WorkSourceAsset,
)
from app.database import get_db
from app.main import create_application
from app.schemas import PackageImportBatchCreate
from app.services.import_integrations import update_import_item
from app.services.import_queue import confirm_import_items
from app.services.package_imports import (
    _analyze,
    create_package_batch,
    package_batch_view,
    queue_package_analysis,
    upload_package_file,
)


class Upload:
    def __init__(self, data: bytes, filename: str):
        self.stream = io.BytesIO(data)
        self.filename = filename

    async def read(self, size: int) -> bytes:
        return self.stream.read(size)

    async def close(self) -> None:
        self.stream.close()


class PausingUpload(Upload):
    def __init__(self, data: bytes, filename: str):
        super().__init__(data, filename)
        self.started = asyncio.Event()
        self.resume = asyncio.Event()
        self.reads = 0

    async def read(self, size: int) -> bytes:
        self.reads += 1
        if self.reads == 1:
            value = self.stream.read(size)
            self.started.set()
            return value
        await self.resume.wait()
        return self.stream.read(size)


def fake_mp4(label: bytes = b"video") -> bytes:
    return b"\x00\x00\x00\x18ftypisom" + label


async def factory_for(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'package-test.db').as_posix()}"
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def test_folder_manifest_is_detected_then_uses_manual_confirm_flow(
    tmp_path, monkeypatch
):
    engine, factory = await factory_for(tmp_path)
    monkeypatch.setattr(packages, "async_session_factory", factory)
    monkeypatch.setattr(packages, "DATA_DIR", tmp_path)
    monkeypatch.setattr(packages, "_verify_video", lambda _path: 18.5)

    async def no_queue(_batch_id: str) -> None:
        return None

    monkeypatch.setattr(packages.coordinator, "enqueue", no_queue)
    video_name = "folder/7351234567890123456.mp4"
    manifest = json.dumps(
        {
            "items": [
                {
                    "video_file": video_name,
                    "platform_work_id": "7351234567890123456",
                    "title": "来自外部工具的标题",
                    "description": "外部说明",
                    "author_name": "测试作者",
                }
            ]
        },
        ensure_ascii=False,
    ).encode()
    request = PackageImportBatchCreate.model_validate(
        {
            "rights_attested": True,
            "upload_mode": "folder",
            "files": [
                {
                    "client_file_id": "video-1",
                    "relative_path": video_name,
                    "size_bytes": len(fake_mp4()),
                },
                {
                    "client_file_id": "manifest-1",
                    "relative_path": "manifest.json",
                    "size_bytes": len(manifest),
                },
            ],
        }
    )
    async with factory() as session:
        created = await create_package_batch(
            session,
            rights_attested=True,
            upload_mode=request.upload_mode,
            target_collection_id=None,
            files=request.files,
        )
        server_files = {row["relative_path"]: row for row in created["package_files"]}
        first = await upload_package_file(
            session,
            batch_id=created["id"],
            file_id=server_files[video_name]["id"],
            upload=Upload(fake_mp4(), Path(video_name).name),
        )
        assert first["idempotent"] is False
        second = await upload_package_file(
            session,
            batch_id=created["id"],
            file_id=server_files[video_name]["id"],
            upload=Upload(fake_mp4(), Path(video_name).name),
        )
        assert second["idempotent"] is True
        await upload_package_file(
            session,
            batch_id=created["id"],
            file_id=server_files["manifest.json"]["id"],
            upload=Upload(manifest, "manifest.json"),
        )
        await queue_package_analysis(session, created["id"])

    await _analyze(created["id"])

    async with factory() as session:
        view = await package_batch_view(session, created["id"])
        assert view["state"] == "succeeded"
        assert view["source_type"] == "package_upload"
        assert len(view["items"]) == 1
        item = view["items"][0]
        assert item["platform"] == "douyin"
        assert item["platform_work_id"] == "7351234567890123456"
        assert item["title"] == "来自外部工具的标题"
        assert item["status"] == "ready"
        assert await session.scalar(select(func.count(DailyLinkQuota.day))) == 0

        result = await confirm_import_items(session, created["id"], [item["id"]])
        work = await session.get(Work, result["work_ids"][0])
        assert work.import_source == "package_upload"
        assert work.refresh_policy == "never"
        asset = await session.scalar(
            select(WorkSourceAsset).where(WorkSourceAsset.work_id == work.id)
        )
        assert asset and Path(asset.path).is_file()

    await engine.dispose()


async def test_unmatched_video_falls_back_to_local_sha_identity(tmp_path, monkeypatch):
    engine, factory = await factory_for(tmp_path)
    monkeypatch.setattr(packages, "async_session_factory", factory)
    monkeypatch.setattr(packages, "DATA_DIR", tmp_path)
    monkeypatch.setattr(packages, "_verify_video", lambda _path: 5.0)
    monkeypatch.setattr(packages.coordinator, "enqueue", lambda _batch_id: _noop())
    data = fake_mp4(b"unmatched")
    async with factory() as session:
        created = await create_package_batch(
            session,
            rights_attested=True,
            upload_mode="folder",
            target_collection_id=None,
            files=[
                {
                    "client_file_id": "plain-video",
                    "relative_path": "我的普通视频.mp4",
                    "size_bytes": len(data),
                }
            ],
        )
        row = created["package_files"][0]
        await upload_package_file(
            session,
            batch_id=created["id"],
            file_id=row["id"],
            upload=Upload(data, "我的普通视频.mp4"),
        )
        await queue_package_analysis(session, created["id"])
    await _analyze(created["id"])
    async with factory() as session:
        item = await session.scalar(
            select(ImportItem).where(ImportItem.batch_id == created["id"])
        )
        assert item.platform == "local"
        assert item.platform_work_id.startswith("local-")
        assert item.title == "我的普通视频"
        assert (
            item.raw_metadata["import_provenance"]["match_source"] == "local_fallback"
        )
        updated = await update_import_item(
            session, item_id=item.id, title="修改后的本地标题"
        )
        assert updated["title"] == "修改后的本地标题"
    await engine.dispose()


async def test_analysis_queue_wins_over_inflight_replacement_without_losing_old_file(
    tmp_path, monkeypatch
):
    engine, factory = await factory_for(tmp_path)
    monkeypatch.setattr(packages, "DATA_DIR", tmp_path)
    monkeypatch.setattr(packages.coordinator, "enqueue", lambda _batch_id: _noop())
    original = fake_mp4(b"original")
    replacement = fake_mp4(b"replaced")
    assert len(original) == len(replacement)

    async with factory() as session:
        created = await create_package_batch(
            session,
            rights_attested=True,
            upload_mode="folder",
            target_collection_id=None,
            files=[
                {
                    "client_file_id": "video-1",
                    "relative_path": "video.mp4",
                    "size_bytes": len(original),
                }
            ],
        )
        file_id = created["package_files"][0]["id"]
        await upload_package_file(
            session,
            batch_id=created["id"],
            file_id=file_id,
            upload=Upload(original, "video.mp4"),
        )

    paused = PausingUpload(replacement, "video.mp4")

    async def replace_file():
        async with factory() as session:
            await upload_package_file(
                session,
                batch_id=created["id"],
                file_id=file_id,
                upload=paused,
                replace=True,
            )

    replacement_task = asyncio.create_task(replace_file())
    await paused.started.wait()
    async with factory() as session:
        await queue_package_analysis(session, created["id"])
    paused.resume.set()
    with pytest.raises(packages.IntegrationImportError) as captured:
        await replacement_task
    assert captured.value.code == "batch_locked"

    async with factory() as session:
        row = await session.get(PackageImportFile, file_id)
        assert row is not None
        stored = Path(row.stored_path or "")
        assert stored.read_bytes() == original
        files = [path for path in stored.parent.iterdir() if path.is_file()]
        assert files == [stored]
    await engine.dispose()


async def _noop():
    return None


async def test_f2_download_folder_database_matches_video_id(tmp_path, monkeypatch):
    engine, factory = await factory_for(tmp_path)
    monkeypatch.setattr(packages, "async_session_factory", factory)
    monkeypatch.setattr(packages, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(packages, "_verify_video", lambda _path: 9.0)
    monkeypatch.setattr(packages.coordinator, "enqueue", lambda _batch_id: _noop())
    work_id = "7351234567890123999"
    database_path = tmp_path / "douyin_videos.db"
    connection = sqlite3.connect(database_path)
    connection.execute(
        "CREATE TABLE video_info(aweme_id TEXT, desc TEXT, nickname TEXT, uid TEXT, create_time INTEGER)"
    )
    connection.execute(
        "INSERT INTO video_info VALUES(?,?,?,?,?)",
        (work_id, "F2 自动匹配标题", "F2 作者", "author-1", 1_700_000_000),
    )
    connection.commit()
    connection.close()
    video = fake_mp4(b"f2-folder")
    database = database_path.read_bytes()
    async with factory() as session:
        created = await create_package_batch(
            session,
            rights_attested=True,
            upload_mode="folder",
            target_collection_id=None,
            files=[
                {
                    "client_file_id": "f2-video",
                    "relative_path": f"download/{work_id}.mp4",
                    "size_bytes": len(video),
                },
                {
                    "client_file_id": "f2-database",
                    "relative_path": "douyin_videos.db",
                    "size_bytes": len(database),
                },
            ],
        )
        by_path = {row["relative_path"]: row for row in created["package_files"]}
        await upload_package_file(
            session,
            batch_id=created["id"],
            file_id=by_path[f"download/{work_id}.mp4"]["id"],
            upload=Upload(video, f"{work_id}.mp4"),
        )
        await upload_package_file(
            session,
            batch_id=created["id"],
            file_id=by_path["douyin_videos.db"]["id"],
            upload=Upload(database, "douyin_videos.db"),
        )
        await queue_package_analysis(session, created["id"])
    await _analyze(created["id"])
    async with factory() as session:
        item = await session.scalar(
            select(ImportItem).where(ImportItem.batch_id == created["id"])
        )
        assert item.platform == "douyin"
        assert item.platform_work_id == work_id
        assert item.title == "F2 自动匹配标题"
        assert item.author_name == "F2 作者"
        assert item.raw_metadata["import_provenance"]["match_source"] == "f2_database"
    await engine.dispose()


async def test_zip_path_traversal_is_rejected_without_writing_outside(
    tmp_path, monkeypatch
):
    engine, factory = await factory_for(tmp_path)
    monkeypatch.setattr(packages, "async_session_factory", factory)
    monkeypatch.setattr(packages, "DATA_DIR", tmp_path)
    monkeypatch.setattr(packages.coordinator, "enqueue", lambda _batch_id: _noop())
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr("../escape.mp4", fake_mp4())
    data = content.getvalue()
    async with factory() as session:
        created = await create_package_batch(
            session,
            rights_attested=True,
            upload_mode="zip",
            target_collection_id=None,
            files=[
                {
                    "client_file_id": "archive-1",
                    "relative_path": "videos.zip",
                    "size_bytes": len(data),
                }
            ],
        )
        row = created["package_files"][0]
        await upload_package_file(
            session,
            batch_id=created["id"],
            file_id=row["id"],
            upload=Upload(data, "videos.zip"),
        )
        await queue_package_analysis(session, created["id"])
    await _analyze(created["id"])
    async with factory() as session:
        batch = await session.get(ImportBatch, created["id"])
        assert batch.state == "failed"
        assert batch.error_code == "invalid_relative_path"
        assert not (tmp_path / "escape.mp4").exists()
    await engine.dispose()


def test_f2_sqlite_reader_uses_fixed_video_info_contract(tmp_path):
    path = tmp_path / "douyin_videos.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE video_info(aweme_id TEXT, desc TEXT, nickname TEXT, uid TEXT, create_time INTEGER)"
    )
    connection.execute(
        "INSERT INTO video_info VALUES(?,?,?,?,?)",
        ("7351234567890123456", "F2 标题", "作者", "user-1", 1_700_000_000),
    )
    connection.commit()
    connection.close()
    rows = packages._read_f2_database(path)
    assert rows[0]["platform_work_id"] == "7351234567890123456"
    assert rows[0]["description"] == "F2 标题"
    assert rows[0]["published_at"].startswith("2023-")


async def test_package_http_create_requires_tokbrain_page_origin(tmp_path):
    engine, factory = await factory_for(tmp_path)

    async def override_db():
        async with factory() as session:
            yield session

    application = create_application()
    application.dependency_overrides[get_db] = override_db
    transport = httpx.ASGITransport(app=application)
    payload = {
        "rights_attested": True,
        "upload_mode": "folder",
        "files": [
            {
                "client_file_id": "video-1",
                "relative_path": "video.mp4",
                "size_bytes": 10,
            }
        ],
    }
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        missing = await client.post("/api/package-import-batches", json=payload)
        assert missing.status_code == 403
        accepted = await client.post(
            "/api/package-import-batches",
            json=payload,
            headers={"Origin": "http://127.0.0.1:3000"},
        )
        assert accepted.status_code == 201
        assert accepted.json()["source_type"] == "package_upload"
        jobs = await client.get("/api/jobs")
        assert jobs.status_code == 200
        assert jobs.json()[0]["state"] == "uploading"
    await engine.dispose()


async def test_package_worker_records_error_and_continues(monkeypatch):
    coordinator = packages.PackageImportCoordinator()

    async def fail(_batch_id):
        raise RuntimeError("package worker failed")

    monkeypatch.setattr(packages, "_analyze", fail)
    await coordinator.queue.put("batch-1")
    await coordinator.queue.put(None)

    await coordinator._run()

    assert coordinator.last_error == "package worker failed"


async def test_package_coordinator_restart_after_worker_already_stopped(monkeypatch):
    coordinator = packages.PackageImportCoordinator()
    coordinator.task = asyncio.create_task(asyncio.sleep(0))
    await coordinator.task

    await coordinator.stop()

    seen: list[str] = []

    async def analyze(batch_id):
        seen.append(batch_id)

    monkeypatch.setattr(packages, "_analyze", analyze)
    coordinator.task = asyncio.create_task(coordinator._run())
    await coordinator.enqueue("fresh-batch")
    await coordinator.queue.put(None)
    await coordinator.task

    assert seen == ["fresh-batch"]
