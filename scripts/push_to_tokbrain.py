#!/usr/bin/env python3
"""Push an authorized local-video manifest to TokBrain's loopback API.

This helper uses only the Python standard library. Local ``video_path`` values
are consumed by this script and are never included in the API manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import mimetypes
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlsplit


ALLOWED_ITEM_FIELDS = {
    "client_item_id",
    "platform_work_id",
    "title",
    "description",
    "author_id",
    "author_name",
    "published_at",
    "source_url",
    "duration_seconds",
    "target_collection_id",
    "expected_sha256",
    "extra_metadata",
}
CHUNK_SIZE = 1024 * 1024
MAX_VIDEO_BYTES = 1024 * 1024 * 1024
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_EXTRA_METADATA_BYTES = 64 * 1024
CLIENT_ITEM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


class PushError(RuntimeError):
    """A safe, user-facing integration failure."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _load_manifest(path: Path) -> tuple[dict[str, Any], dict[str, Path]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PushError(f"无法读取清单 {path}: {exc}") from exc

    items = raw.get("items") if isinstance(raw, dict) else None
    if not isinstance(items, list) or not 1 <= len(items) <= 100:
        raise PushError("清单 items 必须包含 1–100 条记录")

    api_items: list[dict[str, Any]] = []
    files: dict[str, Path] = {}
    seen: set[str] = set()
    for position, raw_item in enumerate(items, start=1):
        if not isinstance(raw_item, dict):
            raise PushError(f"第 {position} 条记录必须是 JSON 对象")
        client_item_id = str(raw_item.get("client_item_id", "")).strip()
        platform_work_id = str(raw_item.get("platform_work_id", "")).strip()
        title = str(raw_item.get("title", "")).strip()
        video_path = raw_item.get("video_path")
        if not CLIENT_ITEM_RE.fullmatch(client_item_id) or client_item_id in seen:
            raise PushError(f"第 {position} 条 client_item_id 格式无效或在批次内重复")
        if not platform_work_id.isdigit() or len(platform_work_id) > 64:
            raise PushError(
                f"第 {position} 条 platform_work_id 必须是纯数字抖音作品 ID"
            )
        if not title or len(title) > 500:
            raise PushError(f"第 {position} 条 title 必须为 1–500 个字符")
        if not isinstance(video_path, str) or not video_path.strip():
            raise PushError(f"第 {position} 条必须提供本地 video_path")

        local_path = Path(video_path).expanduser().resolve()
        if not local_path.is_file():
            raise PushError(f"第 {position} 条视频不存在或不是文件: {local_path}")
        if local_path.stat().st_size > MAX_VIDEO_BYTES:
            raise PushError(f"第 {position} 条视频超过 1 GB: {local_path}")
        extra_metadata = raw_item.get("extra_metadata", {})
        if not isinstance(extra_metadata, dict):
            raise PushError(f"第 {position} 条 extra_metadata 必须是 JSON 对象")
        if len(_canonical_json(extra_metadata)) > MAX_EXTRA_METADATA_BYTES:
            raise PushError(f"第 {position} 条 extra_metadata 超过 64 KB")
        actual_sha256 = _sha256(local_path)
        declared_sha256 = str(raw_item.get("expected_sha256", "")).lower().strip()
        if declared_sha256 and declared_sha256 != actual_sha256:
            raise PushError(
                f"第 {position} 条 expected_sha256 与本地文件不符: {client_item_id}"
            )

        api_item = {
            key: raw_item[key] for key in ALLOWED_ITEM_FIELDS if key in raw_item
        }
        api_item.update(
            client_item_id=client_item_id,
            platform_work_id=platform_work_id,
            video_pending=True,
            title=title,
            expected_sha256=actual_sha256,
        )
        api_items.append(api_item)
        files[client_item_id] = local_path
        seen.add(client_item_id)

    manifest = {"rights_attested": True, "items": api_items}
    if len(_canonical_json(manifest)) > MAX_MANIFEST_BYTES:
        raise PushError("发送清单超过 2 MB")
    return manifest, files


class TokBrainClient:
    def __init__(self, base_url: str, token: str, timeout: float) -> None:
        parsed = urlsplit(base_url.rstrip("/"))
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise PushError(
                "--base-url 必须是本机 http://127.0.0.1 或 http://localhost"
            )
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise PushError("--base-url 不能包含凭据、查询参数或片段")
        self.host = parsed.hostname
        self.port = parsed.port or 80
        self.prefix = parsed.path.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _connection(self) -> http.client.HTTPConnection:
        return http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)

    def _decode_response(self, response: http.client.HTTPResponse) -> Any:
        body = response.read()
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {"detail": body.decode("utf-8", errors="replace")[:1000]}
        if not 200 <= response.status < 300:
            detail = (
                payload.get("detail", payload) if isinstance(payload, dict) else payload
            )
            raise PushError(f"TokBrain 返回 HTTP {response.status}: {detail}")
        return payload

    def json_request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        body = _canonical_json(payload) if payload is not None else None
        request_headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
        }
        if body is not None:
            request_headers["Content-Type"] = "application/json; charset=utf-8"
        request_headers.update(headers or {})
        connection = self._connection()
        try:
            connection.request(
                method, self.prefix + path, body=body, headers=request_headers
            )
            return self._decode_response(connection.getresponse())
        except (OSError, http.client.HTTPException) as exc:
            raise PushError(f"无法连接 TokBrain: {exc}") from exc
        finally:
            connection.close()

    def upload_file(
        self, batch_id: str, client_item_id: str, path: Path, replace: bool
    ) -> Any:
        boundary = f"----TokBrain{uuid.uuid4().hex}"
        safe_name = path.name.replace('"', "_")
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        preamble = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8")
        trailer = f"\r\n--{boundary}--\r\n".encode("ascii")
        query = urlencode({"replace": str(replace).lower()})
        request_path = (
            f"/api/integrations/v1/import-batches/{quote(batch_id, safe='')}"
            f"/items/{quote(client_item_id, safe='')}/asset?{query}"
        )
        connection = self._connection()
        try:
            connection.putrequest("PUT", self.prefix + request_path)
            connection.putheader("Accept", "application/json")
            connection.putheader("Authorization", f"Bearer {self.token}")
            connection.putheader(
                "Content-Type", f"multipart/form-data; boundary={boundary}"
            )
            connection.putheader(
                "Content-Length",
                str(len(preamble) + path.stat().st_size + len(trailer)),
            )
            connection.endheaders()
            connection.send(preamble)
            with path.open("rb") as source:
                while chunk := source.read(CHUNK_SIZE):
                    connection.send(chunk)
            connection.send(trailer)
            return self._decode_response(connection.getresponse())
        except (OSError, http.client.HTTPException) as exc:
            raise PushError(f"上传 {path.name} 失败: {exc}") from exc
        finally:
            connection.close()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="将有权处理的本地视频批量推送到本机 TokBrain"
    )
    parser.add_argument(
        "manifest", type=Path, help="包含 items 与 video_path 的 JSON 清单"
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("TOKBRAIN_IMPORT_TOKEN", ""),
        help="外部导入令牌；建议使用 TOKBRAIN_IMPORT_TOKEN 环境变量",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("TOKBRAIN_API_URL", "http://127.0.0.1:8000"),
        help="TokBrain 本机 API；可用 TOKBRAIN_API_URL 对齐自定义 APP_PORT",
    )
    parser.add_argument(
        "--idempotency-key", help="重试时复用的幂等键；默认按清单内容生成"
    )
    parser.add_argument(
        "--start-processing", action="store_true", help="提交后立即创建 AI 入库任务"
    )
    parser.add_argument(
        "--replace", action="store_true", help="替换尚未提交且内容不同的已上传资产"
    )
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument(
        "--attest-rights",
        action="store_true",
        help="确认已获得处理清单中全部文件与元数据的必要权利",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if not args.attest_rights:
        raise PushError("必须显式添加 --attest-rights 才会提交文件")
    token = args.token.strip()
    if not token:
        raise PushError("缺少令牌：请设置 TOKBRAIN_IMPORT_TOKEN 或传入 --token")

    manifest, files = _load_manifest(args.manifest.resolve())
    idempotency_key = args.idempotency_key or (
        "tokbrain-manifest-" + hashlib.sha256(_canonical_json(manifest)).hexdigest()
    )
    client = TokBrainClient(args.base_url, token, args.timeout)
    created = client.json_request(
        "POST",
        "/api/integrations/v1/import-batches",
        manifest,
        {"Idempotency-Key": idempotency_key},
    )
    if not isinstance(created, dict) or not created.get("batch_id"):
        raise PushError("TokBrain 创建批次响应缺少 batch_id")
    batch_id = str(created["batch_id"])

    upload_errors: list[dict[str, str]] = []
    for item in created.get("items", []):
        client_item_id = str(item.get("client_item_id", ""))
        # Existing/committed items deliberately have no upload URL. This makes
        # replaying a fully or partially committed manifest safe.
        if not client_item_id or not item.get("upload_url"):
            continue
        try:
            client.upload_file(
                batch_id, client_item_id, files[client_item_id], args.replace
            )
        except (KeyError, PushError) as exc:
            upload_errors.append({"client_item_id": client_item_id, "error": str(exc)})

    if upload_errors:
        print(
            json.dumps(
                {"batch_id": batch_id, "upload_errors": upload_errors},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2

    committed = client.json_request(
        "POST",
        f"/api/integrations/v1/import-batches/{quote(batch_id, safe='')}/commit",
        {"start_processing": bool(args.start_processing)},
    )
    print(json.dumps(committed, ensure_ascii=False, indent=2))
    failed = any(
        result.get("status") in {"invalid", "missing_video"}
        for result in committed.get("results", [])
    )
    return 3 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PushError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc
