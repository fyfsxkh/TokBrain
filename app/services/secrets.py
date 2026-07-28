"""DPAPI-backed secret persistence.

The encrypted bytes are safe to keep in SQLite but intentionally cannot be
restored under another Windows account.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SecretRecord


class SecretUnavailableError(RuntimeError):
    pass


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def protect_text(value: str) -> bytes:
    if os.name != "nt":
        raise SecretUnavailableError("DPAPI 仅支持 Windows")
    raw, keepalive = _blob(value.encode("utf-8"))
    output = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    ok = crypt32.CryptProtectData(
        ctypes.byref(raw),
        "TokBrain local secret",
        None,
        None,
        None,
        0x01,
        ctypes.byref(output),
    )
    del keepalive
    if not ok:
        raise SecretUnavailableError(str(ctypes.WinError()))
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)


def unprotect_text(value: bytes) -> str:
    if os.name != "nt":
        raise SecretUnavailableError("DPAPI 仅支持 Windows")
    raw, keepalive = _blob(value)
    output = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(raw), None, None, None, None, 0x01, ctypes.byref(output)
    )
    del keepalive
    if not ok:
        raise SecretUnavailableError(
            "当前 Windows 用户无法解密此密钥；请重新输入模型或账单 API Key"
        )
    try:
        return ctypes.string_at(output.pbData, output.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)


async def set_secret(session: AsyncSession, name: str, value: str | None) -> None:
    record = await session.get(SecretRecord, name)
    if not value:
        if record:
            await session.delete(record)
        return
    encrypted = protect_text(value.strip())
    if record:
        record.encrypted_value = encrypted
    else:
        session.add(SecretRecord(name=name, encrypted_value=encrypted))


async def get_secret(session: AsyncSession, name: str) -> str | None:
    record = await session.get(SecretRecord, name)
    if not record:
        return None
    return unprotect_text(record.encrypted_value)


async def has_secret(session: AsyncSession, name: str) -> bool:
    return await session.get(SecretRecord, name) is not None


async def has_readable_secret(session: AsyncSession, name: str) -> bool:
    """Report only secrets that the current Windows security context can decrypt."""

    record = await session.get(SecretRecord, name)
    if not record:
        return False
    try:
        unprotect_text(record.encrypted_value)
    except SecretUnavailableError:
        return False
    return True
