import io
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.local_assets as assets
from app.models import Base, ImportBatch, ImportItem, Job, Work, WorkSourceAsset
from app.services.local_assets import (
    LocalAssetError,
    _kind_and_extension,
    store_local_assets,
    store_work_supplement,
)


class Upload:
    def __init__(self, data: bytes, filename: str = "../../escape.jpg"):
        self.data = io.BytesIO(data)
        self.filename = filename
        self.closed = False

    async def read(self, size: int) -> bytes:
        return self.data.read(size)

    async def close(self) -> None:
        self.closed = True


def image_bytes(fmt: str = "PNG") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (12, 12), (220, 20, 40)).save(output, format=fmt)
    return output.getvalue()


async def asset_session(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    session = factory()
    job = Job(id="preview-job", job_type="link_preview", total_items=1)
    batch = ImportBatch(
        id="preview-batch",
        job_id=job.id,
        raw_input="https://www.douyin.com/video/1",
        total_items=1,
    )
    session.add_all([job, batch])
    await session.flush()
    item = ImportItem(
        batch_id=batch.id,
        ordinal=1,
        input_url="https://www.douyin.com/video/1",
        normalized_url="https://www.douyin.com/video/1",
        status="needs_local_file",
        error_code="media_missing",
    )
    session.add(item)
    await session.commit()
    return engine, session, item


def test_magic_detection_supports_declared_video_and_image_families():
    assert _kind_and_extension(b"\xff\xd8\xff" + b"x" * 20)[0] == "image"
    assert _kind_and_extension(b"\x89PNG\r\n\x1a\n" + b"x" * 20)[1] == ".png"
    assert _kind_and_extension(b"RIFFxxxxWEBP" + b"x" * 20)[1] == ".webp"
    assert _kind_and_extension(b"\x00\x00\x00\x18ftypqt  " + b"x" * 20)[1:] == (
        ".mov",
        "video/quicktime",
    )
    assert _kind_and_extension(b"\x00\x00\x00\x18ftypisom" + b"x" * 20)[1] == ".mp4"
    assert _kind_and_extension(b"\x1aE\xdf\xa3" + b"x" * 20)[1] == ".mkv"


async def test_images_use_uuid_paths_and_ignore_client_filename(tmp_path, monkeypatch):
    engine, session, item = await asset_session(tmp_path)
    monkeypatch.setattr(assets, "DATA_DIR", tmp_path)
    uploads = [
        Upload(image_bytes("PNG"), "../../outside.png"),
        Upload(image_bytes("WEBP"), r"C:\secret\name.webp"),
    ]
    await store_local_assets(session, item.id, uploads)
    stored = (
        (
            await session.execute(
                select(WorkSourceAsset).order_by(WorkSourceAsset.position)
            )
        )
        .scalars()
        .all()
    )
    root = (tmp_path / "source-assets" / f"item-{item.id}").resolve()
    assert len(stored) == 2
    assert all(Path(row.path).resolve().is_relative_to(root) for row in stored)
    assert all("outside" not in row.path and "secret" not in row.path for row in stored)
    assert all(Path(row.path).is_file() for row in stored)
    assert all(upload.closed for upload in uploads)
    assert (await session.get(ImportItem, item.id)).status == "ready"
    await session.close()
    await engine.dispose()


async def test_extension_disguise_is_rejected_by_magic_bytes(tmp_path, monkeypatch):
    engine, session, item = await asset_session(tmp_path)
    monkeypatch.setattr(assets, "DATA_DIR", tmp_path)
    upload = Upload(b"not an image", "looks-safe.jpg")
    with pytest.raises(LocalAssetError) as captured:
        await store_local_assets(session, item.id, [upload])
    assert captured.value.code == "unsupported_media"
    assert upload.closed
    assert not list((tmp_path / "source-assets").rglob("*.jpg"))
    await session.close()
    await engine.dispose()


async def test_oversized_upload_removes_partial_file(tmp_path, monkeypatch):
    engine, session, item = await asset_session(tmp_path)
    monkeypatch.setattr(assets, "DATA_DIR", tmp_path)
    monkeypatch.setattr(assets, "VIDEO_LIMIT", 16)
    upload = Upload(b"\x00\x00\x00\x18ftypisom" + b"x" * 32, "large.mp4")

    with pytest.raises(LocalAssetError) as captured:
        await store_local_assets(session, item.id, [upload])

    assert captured.value.code == "file_too_large"
    assert upload.closed
    source_root = tmp_path / "source-assets"
    assert not source_root.exists() or not any(source_root.rglob("*.*"))
    await session.close()
    await engine.dispose()


async def test_rejects_more_than_twelve_images_before_writing(tmp_path, monkeypatch):
    engine, session, item = await asset_session(tmp_path)
    monkeypatch.setattr(assets, "DATA_DIR", tmp_path)
    uploads = [Upload(image_bytes()) for _ in range(13)]
    with pytest.raises(LocalAssetError) as captured:
        await store_local_assets(session, item.id, uploads)
    assert captured.value.code == "file_too_large"
    assert not (tmp_path / "source-assets").exists()
    await session.close()
    await engine.dispose()


async def test_failure_reason_must_be_eligible_for_local_repair(tmp_path, monkeypatch):
    engine, session, item = await asset_session(tmp_path)
    monkeypatch.setattr(assets, "DATA_DIR", tmp_path)
    item.status = "failed"
    item.error_code = "rate_limited"
    await session.commit()
    with pytest.raises(LocalAssetError) as captured:
        await store_local_assets(session, item.id, [Upload(image_bytes())])
    assert captured.value.code == "local_file_required"
    await session.close()
    await engine.dispose()


async def test_expired_confirmed_work_accepts_local_asset_without_losing_linkage(
    tmp_path, monkeypatch
):
    engine, session, item = await asset_session(tmp_path)
    monkeypatch.setattr(assets, "DATA_DIR", tmp_path)
    work = Work(
        platform_work_id="expired-work",
        kind="image",
        title="expired",
        library_state="issues",
        processing_state="failed",
        last_error_code="media_expired",
    )
    session.add(work)
    await session.flush()
    item.existing_work_id = work.id
    await session.commit()

    updated = await store_local_assets(session, item.id, [Upload(image_bytes())])
    stored = await session.scalar(
        select(WorkSourceAsset).where(WorkSourceAsset.import_item_id == item.id)
    )

    assert updated.status == "needs_local_file"
    assert stored is not None
    assert stored.work_id == work.id
    assert work.library_state == "issues"
    assert work.last_error_code is None
    await session.close()
    await engine.dispose()


async def test_asset_replacement_keeps_old_file_when_database_commit_fails(
    tmp_path, monkeypatch
):
    engine, session, item = await asset_session(tmp_path)
    monkeypatch.setattr(assets, "DATA_DIR", tmp_path)
    directory = tmp_path / "source-assets" / f"item-{item.id}"
    directory.mkdir(parents=True)
    old_path = directory / "old.png"
    old_path.write_bytes(image_bytes())
    old_asset = WorkSourceAsset(
        import_item_id=item.id,
        kind="image",
        path=str(old_path),
        mime_type="image/png",
        size_bytes=old_path.stat().st_size,
        sha256="old-sha",
        position=0,
    )
    session.add(old_asset)
    await session.commit()
    item_id = item.id

    real_commit = session.commit

    async def fail_commit():
        raise RuntimeError("simulated commit failure")

    monkeypatch.setattr(session, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="simulated commit failure"):
        await store_local_assets(session, item.id, [Upload(image_bytes("WEBP"))])
    monkeypatch.setattr(session, "commit", real_commit)

    stored = list(
        (
            await session.execute(
                select(WorkSourceAsset).where(WorkSourceAsset.import_item_id == item_id)
            )
        ).scalars()
    )
    assert old_path.is_file()
    assert [row.path for row in stored] == [str(old_path)]
    assert sorted(path.name for path in directory.iterdir()) == [old_path.name]
    await session.close()
    await engine.dispose()


async def test_work_image_supplement_replaces_assets_without_changing_library_state(
    tmp_path, monkeypatch
):
    engine, session, _ = await asset_session(tmp_path)
    monkeypatch.setattr(assets, "DATA_DIR", tmp_path)
    work = Work(
        platform_work_id="image-supplement",
        kind="image",
        title="缺图图文",
        library_state="in_library",
        processing_state="processed",
        supplement_state="required",
        supplement_reason="image_set_incomplete",
        evidence_state="sufficient",
    )
    session.add(work)
    await session.commit()

    work, result = await store_work_supplement(
        session,
        work.id,
        [
            Upload(image_bytes("PNG"), "one.png"),
            Upload(image_bytes("WEBP"), "two.webp"),
        ],
        rights_attested=True,
    )
    stored = list(
        (
            await session.execute(
                select(WorkSourceAsset)
                .where(WorkSourceAsset.work_id == work.id)
                .order_by(WorkSourceAsset.position)
            )
        ).scalars()
    )
    original_paths = [row.path for row in stored]

    assert result["asset_count"] == 2
    assert result["idempotent"] is False
    assert work.library_state == "in_library"
    assert work.processing_state == "processed"
    assert work.supplement_state == "uploaded"
    assert work.supplement_reason == "image_set_incomplete"
    assert work.evidence_state == "unverified"
    assert work.track_report["images"]["processed"] == 2
    assert work.raw_metadata["supplement_provenance"]["rights_attested"] is True
    assert all(Path(row.path).parent.name == f"work-{work.id}" for row in stored)

    _, replay = await store_work_supplement(
        session,
        work.id,
        [
            Upload(image_bytes("PNG"), "one.png"),
            Upload(image_bytes("WEBP"), "two.webp"),
        ],
        rights_attested=True,
    )
    replayed_assets = list(
        (
            await session.execute(
                select(WorkSourceAsset)
                .where(WorkSourceAsset.work_id == work.id)
                .order_by(WorkSourceAsset.position)
            )
        ).scalars()
    )
    assert replay["idempotent"] is True
    assert [row.path for row in replayed_assets] == original_paths
    assert len(replayed_assets) == 2
    await session.close()
    await engine.dispose()


async def test_work_supplement_requires_attestation_and_matching_media_kind(
    tmp_path, monkeypatch
):
    engine, session, _ = await asset_session(tmp_path)
    monkeypatch.setattr(assets, "DATA_DIR", tmp_path)
    work = Work(
        platform_work_id="video-supplement",
        kind="video",
        title="缺视频",
        library_state="in_library",
        supplement_state="required",
    )
    session.add(work)
    await session.commit()

    unattested = Upload(image_bytes(), "wrong.png")
    with pytest.raises(LocalAssetError) as captured:
        await store_work_supplement(
            session, work.id, [unattested], rights_attested=False
        )
    assert captured.value.code == "rights_attestation_required"
    assert unattested.closed

    with pytest.raises(LocalAssetError) as captured:
        await store_work_supplement(
            session,
            work.id,
            [Upload(image_bytes(), "wrong.png")],
            rights_attested=True,
        )
    assert captured.value.code == "unsupported_media"
    assert not await session.scalar(
        select(WorkSourceAsset.id).where(WorkSourceAsset.work_id == work.id)
    )
    assert work.library_state == "in_library"
    await session.close()
    await engine.dispose()
