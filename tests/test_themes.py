from io import BytesIO
import json

import httpx
import pytest
from fastapi import HTTPException
from PIL import Image
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from app.database import get_db
from app.main import create_application
from app.models import Base
from app.services import themes
from app.services.request_security import require_same_origin


PALETTE = {
    "page": "#101214",
    "sidebar": "#15191D",
    "surface": "#1A1F24",
    "surfaceAlt": "#22282E",
    "border": "#343C44",
    "text": "#F4F1EA",
    "muted": "#9BA3AA",
    "primary": "#36F0E0",
    "secondary": "#FF315F",
    "success": "#44D17A",
    "warning": "#FFBD59",
    "danger": "#FF665F",
}


class Upload:
    def __init__(self, content: bytes, filename: str):
        self.content = content
        self.filename = filename
        self.offset = 0
        self.closed = False

    async def read(self, size: int) -> bytes:
        chunk = self.content[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk

    async def close(self) -> None:
        self.closed = True


async def theme_factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'themes.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (32, 18), (220, 120, 170)).save(output, format="PNG")
    return output.getvalue()


def test_palette_requires_all_semantic_hex_colors():
    assert themes.normalize_palette(PALETTE)["primary"] == "#36F0E0"
    with pytest.raises(themes.ThemeAssetError, match="12"):
        themes.normalize_palette({"page": "#000000"})
    with pytest.raises(themes.ThemeAssetError, match="#RRGGBB"):
        themes.normalize_palette({**PALETTE, "page": "red"})


@pytest.mark.asyncio
async def test_inherited_custom_theme_can_be_created_updated_and_deleted(tmp_path):
    engine, factory = await theme_factory(tmp_path)
    async with factory() as session:
        created = await themes.create_custom_theme(
            session,
            name="My palette",
            source_theme_id="classic-night",
            palette=PALETTE,
            upload=None,
        )
        assert created.media_kind == "inherited"
        assert themes.theme_view(created)["content_url"] is None
        updated = await themes.update_custom_theme(
            session,
            created.id,
            name="Renamed",
            palette={**PALETTE, "primary": "#ABCDEF"},
        )
        assert updated.name == "Renamed"
        assert updated.palette["primary"] == "#ABCDEF"
        assert await themes.delete_custom_theme(session, created.id)
        assert await themes.list_custom_themes(session) == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_uploaded_image_uses_uuid_path_and_real_content(tmp_path, monkeypatch):
    asset_root = tmp_path / "theme-assets"
    temp_root = tmp_path / "tmp"
    monkeypatch.setattr(themes, "THEME_ASSET_ROOT", asset_root)
    monkeypatch.setattr(themes, "THEME_TEMP_ROOT", temp_root)
    engine, factory = await theme_factory(tmp_path)
    upload = Upload(png_bytes(), "../../page.html")
    async with factory() as session:
        created = await themes.create_custom_theme(
            session,
            name="Local image",
            source_theme_id="sakura-valley",
            palette=PALETTE,
            upload=upload,
        )
        assert upload.closed
        assert created.media_kind == "static"
        assert created.mime_type == "image/png"
        assert created.width == 32 and created.height == 18
        stored = themes.resolve_theme_path(created.stored_path)
        assert stored and stored.parent == asset_root
        assert stored.name == f"{created.id}.png"
        assert "page" not in stored.name
        await themes.delete_custom_theme(session, created.id)
        assert not stored.exists()
    await engine.dispose()


def request(origin: str | None) -> Request:
    headers = [] if origin is None else [(b"origin", origin.encode())]
    return Request({"type": "http", "method": "POST", "path": "/api/themes", "headers": headers})


def test_theme_mutations_require_tokbrain_origin():
    with pytest.raises(HTTPException) as missing:
        require_same_origin(request(None))
    assert missing.value.status_code == 403
    require_same_origin(request("http://testserver"))


@pytest.mark.asyncio
async def test_theme_http_crud_and_private_media_delivery(tmp_path, monkeypatch):
    asset_root = tmp_path / "theme-assets"
    monkeypatch.setattr(themes, "THEME_ASSET_ROOT", asset_root)
    monkeypatch.setattr(themes, "THEME_TEMP_ROOT", tmp_path / "tmp")
    engine, factory = await theme_factory(tmp_path)

    async def override_db():
        async with factory() as session:
            yield session

    application = create_application()
    application.dependency_overrides[get_db] = override_db
    transport = httpx.ASGITransport(app=application)
    form = {
        "name": "HTTP image",
        "source_theme_id": "sakura-valley",
        "palette_json": json.dumps(PALETTE),
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        forbidden = await client.post("/api/themes", data=form)
        assert forbidden.status_code == 403

        created = await client.post(
            "/api/themes",
            data=form,
            files={"file": ("wallpaper.png", png_bytes(), "image/png")},
            headers={"Origin": "http://127.0.0.1:3000"},
        )
        assert created.status_code == 201
        payload = created.json()
        assert payload["media_kind"] == "static"
        assert payload["content_url"].endswith("/content")

        listing = await client.get("/api/themes")
        assert [item["id"] for item in listing.json()["items"]] == [payload["id"]]

        content = await client.get(payload["content_url"])
        assert content.status_code == 200
        assert content.headers["content-type"].startswith("image/png")
        assert content.headers["cache-control"] == "private, max-age=31536000, immutable"
        assert content.content == png_bytes()

        patched = await client.patch(
            f"/api/themes/{payload['id']}",
            json={"name": "HTTP renamed", "palette": {**PALETTE, "primary": "#ABCDEF"}},
            headers={"Origin": "http://127.0.0.1:3000"},
        )
        assert patched.status_code == 200
        assert patched.json()["name"] == "HTTP renamed"

        deleted = await client.delete(
            f"/api/themes/{payload['id']}",
            headers={"Origin": "http://127.0.0.1:3000"},
        )
        assert deleted.status_code == 200
        assert deleted.json()["files_removed"] is True
        assert (await client.get(payload["content_url"])).status_code == 404
    await engine.dispose()
