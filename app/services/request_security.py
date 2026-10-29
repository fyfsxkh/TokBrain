"""Shared protection for browser-only local mutation endpoints."""

from fastapi import HTTPException, Request

from app.config import settings


def require_same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin not in {
        *settings.allowed_origins,
        "http://testserver",
        "https://testserver",
    }:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "same_origin_required",
                "message": "该接口只能由 TokBrain 本机网页调用",
                "retryable": False,
                "field": "Origin",
            },
        )
