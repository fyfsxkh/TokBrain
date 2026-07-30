"""One-way migration from the legacy account scraper to the local import schema."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine

from app.config import DATA_DIR
from app.models import Base


DB_PATH = DATA_DIR / "douyin_rag.db"
SCHEMA_KEY = "schema"
SCHEMA_VERSION = 5
REMOVED_SETTING_KEYS = {
    "active_account",
    "adapter_health_state",
    "browser_runtime",
    "schedule",
}


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(connection, table):
        return set()
    return {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')}


def _schema_version(path: Path) -> int:
    if not path.exists():
        return SCHEMA_VERSION
    connection = sqlite3.connect(path)
    try:
        if not _table_exists(connection, "app_settings"):
            return 0
        row = connection.execute(
            "SELECT value FROM app_settings WHERE key=?", (SCHEMA_KEY,)
        ).fetchone()
        if not row:
            return 0
        value = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        return int((value or {}).get("version", 0))
    except (ValueError, TypeError, json.JSONDecodeError, sqlite3.DatabaseError):
        return 0
    finally:
        connection.close()


def schema_upgrade_needed(path: Path = DB_PATH) -> bool:
    return path.exists() and _schema_version(path) < SCHEMA_VERSION


def backup_before_upgrade(path: Path = DB_PATH) -> Path | None:
    if not schema_upgrade_needed(path):
        return None
    backup_dir = path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"douyin_rag-pre-v5-{datetime.now():%Y%m%d-%H%M%S}.db"
    source = sqlite3.connect(path)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return target


def _rows(connection: sqlite3.Connection, table: str) -> list[dict]:
    if not _table_exists(connection, table):
        return []
    cursor = connection.execute(f'SELECT * FROM "{table}"')
    names = [item[0] for item in cursor.description or []]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _insert(
    connection: sqlite3.Connection, table: str, row: dict, *, replace: bool = False
) -> None:
    allowed = _columns(connection, table)
    values = {key: value for key, value in row.items() if key in allowed}
    if not values:
        return
    keys = list(values)
    verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
    placeholders = ",".join("?" for _ in keys)
    columns = ",".join(f'"{key}"' for key in keys)
    connection.execute(
        f'{verb} INTO "{table}" ({columns}) VALUES ({placeholders})',
        [values[key] for key in keys],
    )


def _derived_library_state(
    work: dict,
    relation_states: list[str],
    *,
    has_knowledge: bool,
) -> str:
    states = set(relation_states)
    processed = work.get("processing_state") == "processed" and has_knowledge
    if "in_library" in states and processed:
        return "in_library"
    if (
        states.intersection({"failed", "ingesting"})
        or ("in_library" in states and not processed)
        or str(work.get("processing_state") or "").startswith(("failed", "waiting"))
    ):
        return "issues"
    if states.intersection({"candidate", "selected", "deferred"}):
        return "pending"
    if states and states == {"archived"}:
        return "archived"
    if processed:
        return "in_library"
    if work.get("status") == "archived":
        return "archived"
    return "pending"


def _copy_legacy_data(source: sqlite3.Connection, target: sqlite3.Connection) -> None:
    now = datetime.now(timezone.utc).isoformat()

    for row in _rows(source, "app_settings"):
        if row.get("key") in REMOVED_SETTING_KEYS or str(row.get("key", "")).startswith(
            ("library_state_repair_", "work_duration_repair_")
        ):
            continue
        _insert(target, "app_settings", row)
    _insert(
        target,
        "app_settings",
        {"key": SCHEMA_KEY, "value": json.dumps({"version": SCHEMA_VERSION}), "updated_at": now},
        replace=True,
    )

    for row in _rows(source, "secret_records"):
        copied = dict(row)
        if copied.get("name") == "douyin_cookie":
            copied["name"] = "f2_cookie"
        _insert(target, "secret_records", copied)

    relation_states: dict[int, list[str]] = defaultdict(list)
    relation_errors: dict[int, str] = {}
    for row in _rows(source, "account_works"):
        work_id = int(row.get("work_id") or 0)
        if not work_id:
            continue
        relation_states[work_id].append(str(row.get("state") or "candidate"))
        if row.get("last_error_code"):
            relation_errors.setdefault(work_id, str(row["last_error_code"]))
    knowledge_work_ids = {
        int(row["work_id"])
        for row in _rows(source, "knowledge_chunks")
        if row.get("work_id") is not None
    }

    for row in _rows(source, "works"):
        work_id = int(row["id"])
        copied = dict(row)
        copied["library_state"] = _derived_library_state(
            row,
            relation_states.get(work_id, []),
            has_knowledge=work_id in knowledge_work_ids,
        )
        copied["last_error_code"] = relation_errors.get(work_id)
        if copied["library_state"] == "archived" and not copied.get("archived_at"):
            copied["archived_at"] = now
        _insert(target, "works", copied)

    for index, row in enumerate(_rows(source, "collections")):
        _insert(
            target,
            "collections",
            {
                "id": row.get("id"),
                "key": f"legacy-{row.get('id')}",
                "title": row.get("title") or f"历史分组 {row.get('id')}",
                "cover_url": row.get("cover_url"),
                "sort_order": row.get("remote_order", index),
                "created_at": row.get("created_at", now),
                "updated_at": row.get("updated_at", now),
            },
        )
    _insert(
        target,
        "collections",
        {
            "key": "manual-import",
            "title": "手动导入",
            "sort_order": -1,
            "created_at": now,
            "updated_at": now,
        },
    )

    for row in _rows(source, "collection_memberships"):
        if "active" in row and not bool(row.get("active")):
            continue
        _insert(
            target,
            "collection_memberships",
            {
                "id": row.get("id"),
                "collection_id": row.get("collection_id"),
                "work_id": row.get("work_id"),
                "created_at": row.get("first_seen_at", now),
            },
        )

    for table in (
        "usage_events",
        "daily_budgets",
        "keyframes",
        "knowledge_chunks",
        "work_summaries",
    ):
        for row in _rows(source, table):
            _insert(target, table, row)


def migrate_to_v4(path: Path = DB_PATH) -> None:
    if not path.exists() or _schema_version(path) >= 4:
        return
    temp = path.with_name(f"{path.stem}.v4-migrating{path.suffix}")
    temp.unlink(missing_ok=True)
    engine = create_engine(f"sqlite:///{temp.as_posix()}")
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()

    source = sqlite3.connect(path)
    target = sqlite3.connect(temp)
    try:
        target.execute("PRAGMA foreign_keys=OFF")
        target.execute("BEGIN")
        _copy_legacy_data(source, target)
        target.commit()
        target.execute("PRAGMA foreign_keys=ON")
        violations = target.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError("本地知识库迁移后的外键检查失败")
    except Exception:
        target.rollback()
        raise
    finally:
        source.close()
        target.close()

    for suffix in ("-wal", "-shm"):
        path.with_name(path.name + suffix).unlink(missing_ok=True)
    os.replace(temp, path)


def migrate_to_v5(path: Path = DB_PATH) -> None:
    """Add collection prompts without rebuilding or dropping v4 queue data."""

    if not path.exists() or _schema_version(path) >= 5:
        return
    connection = sqlite3.connect(path)
    try:
        if (
            _table_exists(connection, "collections")
            and "summary_prompt" not in _columns(connection, "collections")
        ):
            connection.execute("ALTER TABLE collections ADD COLUMN summary_prompt TEXT")
        connection.execute(
            """
            INSERT INTO app_settings(key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (
                SCHEMA_KEY,
                json.dumps({"version": SCHEMA_VERSION}),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        connection.commit()
    finally:
        connection.close()


def cleanup_legacy_browser_profile() -> bool:
    profile = DATA_DIR / "browser-profile"
    if not profile.exists():
        return True
    try:
        shutil.rmtree(profile)
    except OSError:
        return False
    return not profile.exists()


def persist_security_cleanup_state(path: Path = DB_PATH) -> bool:
    clean = cleanup_legacy_browser_profile()
    if not path.exists():
        return clean
    connection = sqlite3.connect(path)
    try:
        now = datetime.now(timezone.utc).isoformat()
        value = json.dumps(
            {
                "required": not clean,
                "message": (
                    "旧专用浏览器数据尚未清理，请关闭旧登录窗口后重启 TokBrain"
                    if not clean
                    else "旧抖音登录数据已清理"
                ),
            },
            ensure_ascii=False,
        )
        connection.execute(
            """
            INSERT INTO app_settings(key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            ("security_cleanup", value, now),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO secret_records(name, encrypted_value, updated_at)
            SELECT 'f2_cookie', encrypted_value, updated_at
            FROM secret_records
            WHERE name='douyin_cookie'
            """
        )
        connection.execute("DELETE FROM secret_records WHERE name='douyin_cookie'")
        if _table_exists(connection, "collections"):
            connection.execute(
                """
                INSERT OR IGNORE INTO collections(
                  key, title, cover_url, summary_prompt, sort_order, created_at, updated_at
                ) VALUES ('manual-import', '手动导入', NULL, NULL, -1, ?, ?)
                """,
                (now, now),
            )
        connection.execute(
            """
            INSERT INTO app_settings(key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (
                SCHEMA_KEY,
                json.dumps({"version": SCHEMA_VERSION}),
                now,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return clean


def prepare_database(path: Path = DB_PATH) -> Path | None:
    backup = backup_before_upgrade(path)
    migrate_to_v4(path)
    migrate_to_v5(path)
    persist_security_cleanup_state(path)
    return backup


def finalize_database(path: Path = DB_PATH) -> bool:
    return persist_security_cleanup_state(path)
