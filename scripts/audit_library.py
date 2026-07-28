"""Read-only consistency report for the local knowledge library."""

from __future__ import annotations

import sqlite3

from app.config import DATA_DIR


def main() -> None:
    connection = sqlite3.connect(DATA_DIR / "douyin_rag.db")
    try:
        states = connection.execute(
            "SELECT library_state, COUNT(*) FROM works GROUP BY library_state ORDER BY library_state"
        ).fetchall()
        searchable = connection.execute(
            """
            SELECT COUNT(DISTINCT w.id)
            FROM works w
            WHERE w.library_state = 'in_library'
              AND w.processing_state = 'processed'
              AND EXISTS (
                  SELECT 1 FROM knowledge_chunks k WHERE k.work_id = w.id
              )
            """
        ).fetchone()[0]
        chunk_works = connection.execute(
            "SELECT COUNT(DISTINCT work_id) FROM knowledge_chunks"
        ).fetchone()[0]
        valid_chunk_works = connection.execute(
            """
            SELECT COUNT(DISTINCT k.work_id)
            FROM knowledge_chunks k
            JOIN works w ON w.id = k.work_id
            WHERE w.processing_state = 'processed'
            """
        ).fetchone()[0]
        grouped_distinct = connection.execute(
            """
            SELECT COUNT(DISTINCT cm.work_id)
            FROM collection_memberships cm
            """
        ).fetchone()[0]
        errors = connection.execute(
            """
            SELECT COALESCE(last_error_code, 'none'), COUNT(*)
            FROM works
            WHERE library_state = 'issues'
            GROUP BY last_error_code
            ORDER BY COUNT(*) DESC
            """
        ).fetchall()
        print(f"states={states}")
        print(f"searchable={searchable}")
        print(f"chunk_works={chunk_works}")
        print(f"valid_processed_chunk_works={valid_chunk_works}")
        print(f"searchable_matches_valid={searchable == valid_chunk_works}")
        print(f"grouped_distinct={grouped_distinct}")
        print(f"errors={errors}")
    finally:
        connection.close()


if __name__ == "__main__":
    main()
