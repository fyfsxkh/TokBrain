import asyncio
import hashlib
import io
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.local_assets as local_assets
from app.database import get_db
from app.main import API_CONTRACT_VERSION, app, create_application
from app.models import (
    AppSetting,
    Base,
    Collection,
    CollectionMembership,
    DailyLinkQuota,
    ImportItem,
    Job,
    Work,
    WorkSourceAsset,
)
from app.schemas import (
    ExternalImportBatchCreate,
    LocalImportBatchCreate,
)
from app.services.import_integrations import (
    IntegrationImportError,
    commit_external_batch,
    create_external_import_batch,
    create_local_import_batch,
    integration_token_status,
    revoke_integration_token,
    rotate_integration_token,
    verify_integration_token,
)
from app.services.import_queue import confirm_import_items
from app.services.jobs import _refresh_f2_media
from app.services.local_assets import LocalAssetError, store_import_video


class Upload:
    def __init__(self, data: bytes, filename: str = "video.mp4"):
        self.data = io.BytesIO(data)
        self.filename = filename
        self.closed = False

    async def read(self, size: int) -> bytes:
        return self.data.read(size)

    async def close(self) -> None:
        self.closed = True


class InterruptedUpload(Upload):
    async def read(self, size: int) -> bytes:
        if self.data.tell() == 0:
            return self.data.read(min(size, 16))
        raise OSError("simulated interrupted upload")


async def import_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, factory()


def fake_mp4(label: bytes = b"content") -> bytes:
    return b"\x00\x00\x00\x18ftypisom" + label


async def test_integration_token_is_hashed_rotated_and_revoked():
    engine, session = await import_session()

    assert (await integration_token_status(session))["configured"] is False
    first = await rotate_integration_token(session)
    assert first["token"].startswith("tb_")
    assert first["token"] not in str((await integration_token_status(session)))
    stored_token = await session.get(AppSetting, "external_import_token")
    assert first["token"] not in str(stored_token.value)
    assert len(stored_token.value["sha256"]) == 64
    await verify_integration_token(session, f"Bearer {first['token']}")

    with pytest.raises(IntegrationImportError) as missing:
        await verify_integration_token(session, None)
    assert missing.value.code == "authentication_required"

    second = await rotate_integration_token(session)
    with pytest.raises(IntegrationImportError) as old:
        await verify_integration_token(session, f"Bearer {first['token']}")
    assert old.value.code == "invalid_token"
    await verify_integration_token(session, f"Bearer {second['token']}")

    await revoke_integration_token(session)
    with pytest.raises(IntegrationImportError) as revoked:
        await verify_integration_token(session, f"Bearer {second['token']}")
    assert revoked.value.code == "token_not_configured"
    await session.close()
    await engine.dispose()


async def test_local_video_reuses_ingest_pipeline_without_f2(tmp_path, monkeypatch):
    engine, session = await import_session()
    monkeypatch.setattr(local_assets, "DATA_DIR", tmp_path)
    monkeypatch.setattr(local_assets, "_verify_video", lambda _path: 12.5)
    collection = Collection(key="local-tests", title="Local tests")
    session.add(collection)
    await session.commit()
    payload = fake_mp4(b"local-asset")

    request = LocalImportBatchCreate.model_validate(
        {
            "rights_attested": True,
            "items": [
                {
                    "client_item_id": "local-1",
                    "filename": r"C:\Downloads\演示视频.mp4",
                    "size_bytes": len(payload),
                    "target_collection_id": collection.id,
                }
            ],
        }
    )
    view = await create_local_import_batch(
        session,
        rights_attested=request.rights_attested,
        items=request.items,
    )
    assert view["source_type"] == "local_upload"
    assert view["items"][0]["title"] == "演示视频"
    item_id = view["items"][0]["id"]

    upload = Upload(payload, "../../not-used.mp4")
    item, stored = await store_import_video(
        session, item_id, upload, batch_id=view["id"]
    )
    expected_sha = hashlib.sha256(payload).hexdigest()
    assert upload.closed
    assert item.status == "ready"
    assert item.platform == "local"
    assert item.platform_work_id == f"local-{expected_sha}"
    assert stored["duration_seconds"] == 12.5
    assert stored["budget_estimate"]["media_minutes"] == pytest.approx(0.2083)
    assert stored["budget_estimate"]["llm_tokens"] > 0
    assert item.raw_metadata["import_provenance"]["declared_size_bytes"] == len(
        payload
    )
    assert (
        item.raw_metadata["import_provenance"]["budget_estimate"]
        == stored["budget_estimate"]
    )

    confirmed = await confirm_import_items(session, view["id"], [item.id])
    work = await session.get(Work, confirmed["work_ids"][0])
    source_asset = await session.scalar(
        select(WorkSourceAsset).where(WorkSourceAsset.work_id == work.id)
    )
    membership = await session.scalar(
        select(CollectionMembership).where(
            CollectionMembership.collection_id == collection.id,
            CollectionMembership.work_id == work.id,
        )
    )
    assert work.platform == "local"
    assert work.import_source == "local_upload"
    assert work.refresh_policy == "never"
    assert work.source_url is None
    assert source_asset is not None
    assert membership is not None

    class MustNotCallF2:
        async def work_from_url(self, _url):
            raise AssertionError("F2 must not be called")

    await _refresh_f2_media(session, work, MustNotCallF2())
    policy_only_work = Work(
        platform="douyin",
        platform_work_id="no-local-asset",
        title="Policy guard",
        source_url="https://www.douyin.com/video/999999999",
        import_source="external_batch",
        refresh_policy="never",
    )
    session.add(policy_only_work)
    await session.commit()
    await _refresh_f2_media(session, policy_only_work, MustNotCallF2())
    assert await session.scalar(select(func.count(DailyLinkQuota.day))) == 0

    duplicate_request = LocalImportBatchCreate.model_validate(
        {
            "rights_attested": True,
            "items": [
                {
                    "client_item_id": "local-2",
                    "filename": "same-video.mp4",
                    "size_bytes": len(payload),
                }
            ],
        }
    )
    duplicate_batch = await create_local_import_batch(
        session,
        rights_attested=True,
        items=duplicate_request.items,
    )
    duplicate_item, _ = await store_import_video(
        session,
        duplicate_batch["items"][0]["id"],
        Upload(payload),
        batch_id=duplicate_batch["id"],
    )
    assert duplicate_item.status == "duplicate"
    assert duplicate_item.existing_work_id == work.id
    assert await session.scalar(select(func.count(Work.id))) == 2
    await session.close()
    await engine.dispose()


async def test_local_manifest_rejects_video_with_different_declared_size(
    tmp_path, monkeypatch
):
    engine, session = await import_session()
    monkeypatch.setattr(local_assets, "DATA_DIR", tmp_path)
    monkeypatch.setattr(local_assets, "_verify_video", lambda _path: 4.0)
    payload = fake_mp4(b"actual")
    request = LocalImportBatchCreate.model_validate(
        {
            "rights_attested": True,
            "items": [
                {
                    "client_item_id": "size-check",
                    "filename": "video.mp4",
                    "size_bytes": len(payload) + 1,
                }
            ],
        }
    )
    batch = await create_local_import_batch(
        session,
        rights_attested=True,
        items=request.items,
    )
    item_id = batch["items"][0]["id"]

    with pytest.raises(LocalAssetError) as captured:
        await store_import_video(
            session,
            item_id,
            Upload(payload),
            batch_id=batch["id"],
        )

    assert captured.value.code == "size_mismatch"
    item = await session.get(ImportItem, item_id)
    assert item.status == "needs_local_file"
    assert not list(tmp_path.rglob("*.mp4"))
    await session.close()
    await engine.dispose()


async def test_external_manifest_is_idempotent_and_commit_is_repeatable(
    tmp_path, monkeypatch
):
    engine, session = await import_session()
    monkeypatch.setattr(local_assets, "DATA_DIR", tmp_path)
    monkeypatch.setattr(local_assets, "_verify_video", lambda _path: 33.0)
    collection = Collection(key="external-tests", title="External tests")
    session.add(collection)
    await session.commit()
    video = fake_mp4(b"external-asset")
    expected_sha = hashlib.sha256(video).hexdigest()
    request = ExternalImportBatchCreate.model_validate(
        {
            "rights_attested": True,
            "items": [
                {
                    "client_item_id": "export-1",
                    "platform_work_id": "7531234567890123456",
                    "video_pending": True,
                    "title": "External title",
                    "author_name": "Author",
                    "target_collection_id": collection.id,
                    "expected_sha256": expected_sha,
                    "extra_metadata": {"source_tool": "test"},
                }
            ],
        }
    )
    created = await create_external_import_batch(
        session,
        idempotency_key="stable-key-001",
        rights_attested=request.rights_attested,
        items=request.items,
    )
    replayed = await create_external_import_batch(
        session,
        idempotency_key="stable-key-001",
        rights_attested=request.rights_attested,
        items=request.items,
    )
    assert created["replayed"] is False
    assert replayed["replayed"] is True
    assert replayed["batch_id"] == created["batch_id"]
    assert created["items"][0]["status"] == "needs_local_file"

    different_request = ExternalImportBatchCreate.model_validate(
        {
            "rights_attested": True,
            "items": [
                {
                    "client_item_id": "export-1",
                    "platform_work_id": "7531234567890123456",
                    "video_pending": True,
                    "title": "Changed title",
                }
            ],
        }
    )
    with pytest.raises(IntegrationImportError) as conflict:
        await create_external_import_batch(
            session,
            idempotency_key="stable-key-001",
            rights_attested=True,
            items=different_request.items,
        )
    assert conflict.value.code == "idempotency_conflict"

    item = await session.get(ImportItem, created["items"][0]["item_id"])
    first_upload = Upload(video)
    item, stored = await store_import_video(
        session,
        item.id,
        first_upload,
        batch_id=created["batch_id"],
        expected_sha256=expected_sha,
    )
    assert item.status == "ready"
    assert stored["duration_seconds"] == 33.0

    # An identical upload retry succeeds without replacing the stored asset.
    retry = Upload(video)
    _, retry_result = await store_import_video(
        session,
        item.id,
        retry,
        batch_id=created["batch_id"],
        expected_sha256=expected_sha,
    )
    assert retry_result["idempotent"] is True

    committed = await commit_external_batch(
        session, batch_id=created["batch_id"], start_processing=False
    )
    assert committed["results"][0]["status"] == "imported"
    assert committed["job"] is None
    work = await session.get(Work, committed["work_ids"][0])
    assert work.platform == "douyin"
    assert work.import_source == "external_batch"
    assert work.refresh_policy == "never"
    assert work.author_name == "Author"

    started = await commit_external_batch(
        session, batch_id=created["batch_id"], start_processing=True
    )
    repeated = await commit_external_batch(
        session, batch_id=created["batch_id"], start_processing=True
    )
    assert started["results"][0]["status"] == "imported"
    assert started["job"] is not None
    assert repeated["job"] is None
    assert (
        await session.scalar(select(func.count(Job.id)).where(Job.job_type == "ingest"))
        == 1
    )
    assert await session.scalar(select(func.count(DailyLinkQuota.day))) == 0
    await session.close()
    await engine.dispose()


async def test_external_upload_rejects_sha_mismatch_and_keeps_item_pending(
    tmp_path, monkeypatch
):
    engine, session = await import_session()
    monkeypatch.setattr(local_assets, "DATA_DIR", tmp_path)
    monkeypatch.setattr(local_assets, "_verify_video", lambda _path: 9.0)
    request = ExternalImportBatchCreate.model_validate(
        {
            "rights_attested": True,
            "items": [
                {
                    "client_item_id": "sha-check",
                    "platform_work_id": "123456789",
                    "video_pending": True,
                    "title": "SHA check",
                    "expected_sha256": "0" * 64,
                }
            ],
        }
    )
    batch = await create_external_import_batch(
        session,
        idempotency_key="sha-key-0001",
        rights_attested=True,
        items=request.items,
    )
    item = await session.get(ImportItem, batch["items"][0]["item_id"])
    with pytest.raises(LocalAssetError) as mismatch:
        await store_import_video(
            session,
            item.id,
            Upload(fake_mp4(b"different")),
            batch_id=batch["batch_id"],
            expected_sha256="0" * 64,
        )
    assert mismatch.value.code == "sha256_mismatch"
    await session.refresh(item)
    assert item.status == "needs_local_file"
    assert not list(tmp_path.rglob("*.mp4"))
    await session.close()
    await engine.dispose()


async def test_interrupted_upload_and_asset_replacement_are_safe(tmp_path, monkeypatch):
    engine, session = await import_session()
    monkeypatch.setattr(local_assets, "DATA_DIR", tmp_path)
    monkeypatch.setattr(local_assets, "_verify_video", lambda _path: 5.0)
    request = ExternalImportBatchCreate.model_validate(
        {
            "rights_attested": True,
            "items": [
                {
                    "client_item_id": "replace-me",
                    "platform_work_id": "987654321",
                    "video_pending": True,
                    "title": "Replace me",
                }
            ],
        }
    )
    batch = await create_external_import_batch(
        session,
        idempotency_key="replace-key-001",
        rights_attested=True,
        items=request.items,
    )
    item = await session.get(ImportItem, batch["items"][0]["item_id"])
    interrupted = InterruptedUpload(fake_mp4(b"interrupted"))
    with pytest.raises(OSError):
        await store_import_video(
            session,
            item.id,
            interrupted,
            batch_id=batch["batch_id"],
        )
    assert interrupted.closed
    assert not list(tmp_path.rglob("*.uploading"))

    first_bytes = fake_mp4(b"first")
    _, first = await store_import_video(
        session,
        item.id,
        Upload(first_bytes),
        batch_id=batch["batch_id"],
    )
    first_asset = await session.scalar(
        select(WorkSourceAsset).where(WorkSourceAsset.import_item_id == item.id)
    )
    first_path = first_asset.path
    second_bytes = fake_mp4(b"second")
    with pytest.raises(LocalAssetError) as conflict:
        await store_import_video(
            session,
            item.id,
            Upload(second_bytes),
            batch_id=batch["batch_id"],
        )
    assert conflict.value.code == "asset_conflict"
    assert first_asset.sha256 == first["sha256"]

    _, replaced = await store_import_video(
        session,
        item.id,
        Upload(second_bytes),
        batch_id=batch["batch_id"],
        replace=True,
    )
    replacement = await session.scalar(
        select(WorkSourceAsset).where(WorkSourceAsset.import_item_id == item.id)
    )
    assert replacement.sha256 == replaced["sha256"]
    assert replacement.path != first_path
    assert not Path(first_path).exists()
    await session.close()
    await engine.dispose()


async def test_external_commit_reports_partial_results_and_cross_batch_duplicate(
    tmp_path, monkeypatch
):
    engine, session = await import_session()
    monkeypatch.setattr(local_assets, "DATA_DIR", tmp_path)
    monkeypatch.setattr(local_assets, "_verify_video", lambda _path: 7.0)
    first_collection = Collection(key="partial-a", title="Partial A")
    second_collection = Collection(key="partial-b", title="Partial B")
    session.add_all([first_collection, second_collection])
    await session.commit()
    request = ExternalImportBatchCreate.model_validate(
        {
            "rights_attested": True,
            "items": [
                {
                    "client_item_id": "primary",
                    "platform_work_id": "111111111",
                    "video_pending": True,
                    "title": "Primary",
                    "target_collection_id": first_collection.id,
                },
                {
                    "client_item_id": "same-id",
                    "platform_work_id": "111111111",
                    "video_pending": True,
                    "title": "Duplicate in manifest",
                },
                {
                    "client_item_id": "missing",
                    "platform_work_id": "222222222",
                    "video_pending": True,
                    "title": "Missing video",
                },
            ],
        }
    )
    batch = await create_external_import_batch(
        session,
        idempotency_key="partial-key-0001",
        rights_attested=True,
        items=request.items,
    )
    statuses = {item["client_item_id"]: item["status"] for item in batch["items"]}
    assert statuses == {
        "primary": "needs_local_file",
        "same-id": "duplicate",
        "missing": "needs_local_file",
    }
    primary = next(
        item for item in batch["items"] if item["client_item_id"] == "primary"
    )
    await store_import_video(
        session,
        primary["item_id"],
        Upload(fake_mp4(b"partial")),
        batch_id=batch["batch_id"],
    )
    committed = await commit_external_batch(
        session, batch_id=batch["batch_id"], start_processing=False
    )
    results = {row["client_item_id"]: row["status"] for row in committed["results"]}
    assert results == {
        "primary": "imported",
        "same-id": "duplicate",
        "missing": "missing_video",
    }
    assert await session.scalar(select(func.count(Work.id))) == 1

    duplicate_request = ExternalImportBatchCreate.model_validate(
        {
            "rights_attested": True,
            "items": [
                {
                    "client_item_id": "later-export",
                    "platform_work_id": "111111111",
                    "video_pending": True,
                    "title": "Already imported",
                    "target_collection_id": second_collection.id,
                }
            ],
        }
    )
    duplicate_batch = await create_external_import_batch(
        session,
        idempotency_key="partial-key-0002",
        rights_attested=True,
        items=duplicate_request.items,
    )
    assert duplicate_batch["items"][0]["status"] == "duplicate"
    assert duplicate_batch["items"][0]["upload_url"] is None
    duplicate_commit = await commit_external_batch(
        session, batch_id=duplicate_batch["batch_id"], start_processing=False
    )
    assert duplicate_commit["results"][0]["status"] == "duplicate"
    assert (
        await session.scalar(
            select(func.count(CollectionMembership.id)).where(
                CollectionMembership.collection_id == second_collection.id
            )
        )
        == 1
    )
    await session.close()
    await engine.dispose()


def test_openapi_exposes_contract_v7_import_routes():
    schema = app.openapi()
    paths = schema["paths"]
    assert API_CONTRACT_VERSION == 7
    assert "/api/package-import-batches" in paths
    assert "/api/local-import-batches" in paths
    assert "/api/integrations/v1/import-batches" in paths
    assert "/api/settings/integration-token" in paths
    bearer = schema["components"]["securitySchemes"]["ExternalImportToken"]
    assert bearer["type"] == "http"
    assert bearer["scheme"] == "bearer"
    create_response = paths["/api/integrations/v1/import-batches"]["post"]["responses"][
        "201"
    ]["content"]["application/json"]["schema"]
    assert create_response["$ref"].endswith("/ExternalImportBatchCreated")


def test_external_manifest_rejects_paths_remote_media_and_policy_overrides():
    base = {
        "rights_attested": True,
        "items": [
            {
                "client_item_id": "strict-item",
                "platform_work_id": "123456789",
                "video_pending": True,
                "title": "Strict",
            }
        ],
    }
    for forbidden_field, value in (
        ("video_path", r"C:\private\video.mp4"),
        ("media_url", "https://example.test/video.mp4"),
        ("media_policy", {"download_permission": "allowed"}),
    ):
        payload = json.loads(json.dumps(base))
        payload["items"][0][forbidden_field] = value
        with pytest.raises(ValueError):
            ExternalImportBatchCreate.model_validate(payload)
    invalid_client = json.loads(json.dumps(base))
    invalid_client["items"][0]["client_item_id"] = "../../escape"
    with pytest.raises(ValueError):
        ExternalImportBatchCreate.model_validate(invalid_client)


async def test_external_http_api_requires_current_bearer_token():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override_db():
        async with factory() as session:
            yield session

    application = create_application()
    application.dependency_overrides[get_db] = override_db
    transport = httpx.ASGITransport(app=application)
    manifest = {
        "rights_attested": True,
        "items": [
            {
                "client_item_id": "http-item",
                "platform_work_id": "1234567890",
                "video_pending": True,
                "title": "HTTP item",
            }
        ],
    }
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        unconfigured = await client.post(
            "/api/integrations/v1/import-batches",
            headers={"Idempotency-Key": "http-key-0001"},
            json=manifest,
        )
        assert unconfigured.status_code == 503
        assert unconfigured.json()["detail"]["code"] == "token_not_configured"

        generated = (await client.post("/api/settings/integration-token")).json()
        token = generated["token"]
        oversized = await client.post(
            "/api/integrations/v1/import-batches",
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": "oversized-key-0001",
                "Content-Type": "application/json",
            },
            content=(b" " * (2 * 1024 * 1024) + json.dumps(manifest).encode()),
        )
        assert oversized.status_code == 413
        assert oversized.json()["detail"]["code"] == "request_too_large"
        wrong = await client.post(
            "/api/integrations/v1/import-batches",
            headers={
                "Authorization": "Bearer wrong",
                "Idempotency-Key": "http-key-0001",
            },
            json=manifest,
        )
        assert wrong.status_code == 401
        assert wrong.headers["www-authenticate"] == "Bearer"

        valid_headers = {
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": "http-key-0001",
        }
        created = await client.post(
            "/api/integrations/v1/import-batches",
            headers=valid_headers,
            json=manifest,
        )
        assert created.status_code == 201
        assert created.json()["items"][0]["status"] == "needs_local_file"

        concurrent_manifest = {
            "rights_attested": True,
            "items": [
                {
                    "client_item_id": "concurrent-item",
                    "platform_work_id": "1234567891",
                    "video_pending": True,
                    "title": "Concurrent replay",
                }
            ],
        }
        concurrent_headers = {
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": "concurrent-key-001",
        }
        concurrent = await asyncio.gather(
            client.post(
                "/api/integrations/v1/import-batches",
                headers=concurrent_headers,
                json=concurrent_manifest,
            ),
            client.post(
                "/api/integrations/v1/import-batches",
                headers=concurrent_headers,
                json=concurrent_manifest,
            ),
        )
        assert {response.status_code for response in concurrent} == {201}
        assert len({response.json()["batch_id"] for response in concurrent}) == 1
        assert {response.json()["replayed"] for response in concurrent} == {
            False,
            True,
        }

        rotated = (await client.post("/api/settings/integration-token")).json()
        stale = await client.get(
            f"/api/integrations/v1/import-batches/{created.json()['batch_id']}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert stale.status_code == 401
        assert stale.json()["detail"]["code"] == "invalid_token"

        invalid = await client.post(
            "/api/integrations/v1/import-batches",
            headers={
                "Authorization": f"Bearer {rotated['token']}",
                "Idempotency-Key": "http-key-0002",
            },
            json={
                "rights_attested": True,
                "items": [
                    {
                        "client_item_id": "http-item-2",
                        "platform_work_id": "not-numeric",
                        "video_pending": True,
                        "title": "Invalid",
                    }
                ],
            },
        )
        assert invalid.status_code == 422
        assert invalid.json()["detail"]["code"] == "invalid_request"
        assert invalid.json()["detail"]["field"].endswith("platform_work_id")

        await client.delete("/api/settings/integration-token")
        revoked = await client.get(
            f"/api/integrations/v1/import-batches/{created.json()['batch_id']}",
            headers={"Authorization": f"Bearer {rotated['token']}"},
        )
        assert revoked.status_code == 503
    await engine.dispose()


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="FFmpeg smoke test requires ffmpeg and ffprobe",
)
async def test_real_ffmpeg_local_video_upload_smoke(monkeypatch):
    engine, session = await import_session()
    with tempfile.TemporaryDirectory(prefix="tokbrain-video-smoke-") as directory:
        root = Path(directory)
        video_path = root / "smoke.mp4"
        subprocess.run(
            [
                shutil.which("ffmpeg"),
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:s=320x180:d=1",
                "-c:v",
                "mpeg4",
                "-q:v",
                "5",
                str(video_path),
            ],
            check=True,
            timeout=30,
        )
        monkeypatch.setattr(local_assets, "DATA_DIR", root / "data")
        request = LocalImportBatchCreate.model_validate(
            {
                "rights_attested": True,
                "items": [
                    {
                        "client_item_id": "ffmpeg-smoke",
                        "filename": video_path.name,
                        "size_bytes": video_path.stat().st_size,
                    }
                ],
            }
        )
        batch = await create_local_import_batch(
            session,
            rights_attested=True,
            items=request.items,
        )
        item, stored = await store_import_video(
            session,
            batch["items"][0]["id"],
            Upload(video_path.read_bytes(), video_path.name),
            batch_id=batch["id"],
        )
        assert item.status == "ready"
        assert stored["duration_seconds"] == pytest.approx(1.0, abs=0.1)
        assert Path(
            (
                await session.scalar(
                    select(WorkSourceAsset).where(
                        WorkSourceAsset.import_item_id == item.id
                    )
                )
            ).path
        ).is_file()
    await session.close()
    await engine.dispose()


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="FFmpeg smoke test requires ffmpeg and ffprobe",
)
def test_real_ffmpeg_rejects_truncated_faststart_video(tmp_path):
    complete = tmp_path / "complete.mp4"
    truncated = tmp_path / "truncated.mp4"
    subprocess.run(
        [
            shutil.which("ffmpeg"),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=320x180:d=2",
            "-c:v",
            "mpeg4",
            "-q:v",
            "5",
            "-movflags",
            "+faststart",
            str(complete),
        ],
        check=True,
        timeout=30,
    )
    payload = complete.read_bytes()
    truncated.write_bytes(payload[: len(payload) // 2])

    with pytest.raises(LocalAssetError, match="未下载完整|损坏"):
        local_assets._verify_video(truncated)
