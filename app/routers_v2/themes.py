"""Local custom-theme CRUD and validated wallpaper delivery."""

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import CustomTheme
from app.schemas import CustomThemeList, CustomThemeUpdate, CustomThemeView
from app.services.request_security import require_same_origin
from app.services.themes import (
    ThemeAssetError,
    create_custom_theme,
    delete_custom_theme,
    list_custom_themes,
    parse_palette_json,
    resolve_theme_path,
    theme_view,
    update_custom_theme,
)


router = APIRouter(prefix="/api/themes", tags=["themes"])


def _raise(exc: ThemeAssetError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.detail()) from exc


@router.get("", response_model=CustomThemeList)
async def get_themes(session: AsyncSession = Depends(get_db)):
    items = await list_custom_themes(session)
    return {"items": [theme_view(item) for item in items]}


@router.post("", status_code=201, response_model=CustomThemeView)
async def post_theme(
    request: Request,
    name: str = Form(..., min_length=1, max_length=80),
    source_theme_id: str = Form(...),
    palette_json: str = Form(..., max_length=4096),
    file: UploadFile | None = File(default=None),
    session: AsyncSession = Depends(get_db),
):
    require_same_origin(request)
    try:
        theme = await create_custom_theme(
            session,
            name=name,
            source_theme_id=source_theme_id,
            palette=parse_palette_json(palette_json),
            upload=file,
        )
    except ThemeAssetError as exc:
        _raise(exc)
    return theme_view(theme)


@router.patch("/{theme_id}", response_model=CustomThemeView)
async def patch_theme(
    theme_id: str,
    payload: CustomThemeUpdate,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    require_same_origin(request)
    try:
        theme = await update_custom_theme(
            session,
            theme_id,
            name=payload.name,
            palette=payload.palette,
        )
    except ThemeAssetError as exc:
        _raise(exc)
    return theme_view(theme)


@router.delete("/{theme_id}")
async def delete_theme(
    theme_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    require_same_origin(request)
    try:
        files_removed = await delete_custom_theme(session, theme_id)
    except ThemeAssetError as exc:
        _raise(exc)
    return {"id": theme_id, "deleted": True, "files_removed": files_removed}


def _asset_headers() -> dict[str, str]:
    return {
        "Cache-Control": "private, max-age=31536000, immutable",
        "X-Content-Type-Options": "nosniff",
        "Cross-Origin-Resource-Policy": "same-site",
    }


@router.get("/{theme_id}/content")
async def theme_content(theme_id: str, session: AsyncSession = Depends(get_db)):
    theme = await session.get(CustomTheme, theme_id)
    path = resolve_theme_path(theme.stored_path if theme else None)
    if not theme or not path:
        raise HTTPException(status_code=404, detail="主题媒体不存在")
    return FileResponse(path, media_type=theme.mime_type, headers=_asset_headers())


@router.get("/{theme_id}/poster")
async def theme_poster(theme_id: str, session: AsyncSession = Depends(get_db)):
    theme = await session.get(CustomTheme, theme_id)
    path = resolve_theme_path(theme.poster_path if theme else None)
    if not theme or not path:
        raise HTTPException(status_code=404, detail="主题封面不存在")
    media_type = theme.mime_type if theme.poster_path == theme.stored_path else "image/webp"
    return FileResponse(path, media_type=media_type, headers=_asset_headers())
