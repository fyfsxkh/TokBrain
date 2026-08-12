"""Read-only consistency report for a TokBrain knowledge-library database."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import DATA_DIR  # noqa: E402


ORIGINAL_EVIDENCE_KINDS = ("subtitle", "transcript", "ocr", "visual")
RETRIEVABLE_SOURCE_KINDS = (*ORIGINAL_EVIDENCE_KINDS, "notes")


def _grouped_counts(
    connection: sqlite3.Connection, column: str
) -> dict[str, int]:
    rows = connection.execute(
        f'SELECT COALESCE("{column}", \'none\'), COUNT(*) '
        f'FROM works GROUP BY "{column}" ORDER BY "{column}"'
    ).fetchall()
    return {str(key): int(count) for key, count in rows}


def _schema_version(connection: sqlite3.Connection) -> int | None:
    row = connection.execute(
        "SELECT value FROM app_settings WHERE key = 'schema'"
    ).fetchone()
    if not row:
        return None
    try:
        value = json.loads(str(row[0]))
        return int(value["version"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def build_report(connection: sqlite3.Connection) -> dict[str, object]:
    placeholders_original = ",".join("?" for _ in ORIGINAL_EVIDENCE_KINDS)
    placeholders_retrievable = ",".join(
        "?" for _ in RETRIEVABLE_SOURCE_KINDS
    )
    searchable = connection.execute(
        f"""
        SELECT COUNT(DISTINCT w.id)
        FROM works w
        WHERE w.library_state = 'in_library'
          AND w.processing_state = 'processed'
          AND w.evidence_state IN ('sufficient', 'unverified')
          AND EXISTS (
              SELECT 1
              FROM knowledge_chunks original
              WHERE original.work_id = w.id
                AND lower(original.source_kind) IN ({placeholders_original})
          )
          AND EXISTS (
              SELECT 1
              FROM knowledge_chunks retrievable
              WHERE retrievable.work_id = w.id
                AND lower(retrievable.source_kind) IN ({placeholders_retrievable})
          )
        """,
        (*ORIGINAL_EVIDENCE_KINDS, *RETRIEVABLE_SOURCE_KINDS),
    ).fetchone()[0]
    grounded_chunk_works = connection.execute(
        f"""
        SELECT COUNT(DISTINCT work_id)
        FROM knowledge_chunks
        WHERE lower(source_kind) IN ({placeholders_original})
        """,
        ORIGINAL_EVIDENCE_KINDS,
    ).fetchone()[0]
    retrievable_chunk_works = connection.execute(
        f"""
        SELECT COUNT(DISTINCT work_id)
        FROM knowledge_chunks
        WHERE lower(source_kind) IN ({placeholders_retrievable})
        """,
        RETRIEVABLE_SOURCE_KINDS,
    ).fetchone()[0]
    orphan_chunks = connection.execute(
        """
        SELECT COUNT(*)
        FROM knowledge_chunks k
        LEFT JOIN works w ON w.id = k.work_id
        WHERE w.id IS NULL
        """
    ).fetchone()[0]
    grouped_distinct = connection.execute(
        "SELECT COUNT(DISTINCT work_id) FROM collection_memberships"
    ).fetchone()[0]
    errors = connection.execute(
        """
        SELECT COALESCE(last_error_code, 'none'), COUNT(*)
        FROM works
        WHERE library_state = 'issues'
        GROUP BY last_error_code
        ORDER BY COUNT(*) DESC, last_error_code
        """
    ).fetchall()
    return {
        "schema_version": _schema_version(connection),
        "library_states": _grouped_counts(connection, "library_state"),
        "processing_states": _grouped_counts(connection, "processing_state"),
        "evidence_states": _grouped_counts(connection, "evidence_state"),
        "supplement_states": _grouped_counts(connection, "supplement_state"),
        "searchable_works": int(searchable),
        "grounded_chunk_works": int(grounded_chunk_works),
        "retrievable_chunk_works": int(retrievable_chunk_works),
        "works_in_collections": int(grouped_distinct),
        "orphan_chunks": int(orphan_chunks),
        "issue_error_codes": {str(code): int(count) for code, count in errors},
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="只读检查 TokBrain 知识库状态与 schema v9 证据约束。"
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=DATA_DIR / "douyin_rag.db",
        help="要检查的 SQLite 文件；默认 data/douyin_rag.db",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 输出，便于脚本处理",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    database = args.database.expanduser().resolve()
    if not database.is_file():
        print(f"数据库不存在：{database}", file=sys.stderr)
        return 2

    uri = f"{database.as_uri()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=2)
        connection.execute("PRAGMA query_only = ON")
        try:
            report = build_report(connection)
        finally:
            connection.close()
    except sqlite3.Error as exc:
        print(f"只读检查失败：{exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        for key, value in report.items():
            print(f"{key}={value}")
    return 1 if report["orphan_chunks"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
