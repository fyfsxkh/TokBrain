import json
import sqlite3
from pathlib import Path

import pytest

from app.services.migrations import (
    backup_before_upgrade,
    migrate_to_v4,
    migrate_to_v5,
    migrate_to_v6,
    migrate_to_v7,
    migrate_to_v8,
    migrate_to_v9,
    schema_upgrade_needed,
)


def create_legacy_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    now = "2026-07-26T01:00:00+00:00"
    connection.executescript(
        """
        CREATE TABLE app_settings (
          key TEXT PRIMARY KEY, value JSON NOT NULL, updated_at DATETIME NOT NULL
        );
        CREATE TABLE secret_records (
          name TEXT PRIMARY KEY, encrypted_value BLOB NOT NULL, updated_at DATETIME NOT NULL
        );
        CREATE TABLE works (
          id INTEGER PRIMARY KEY,
          platform TEXT NOT NULL,
          platform_work_id TEXT NOT NULL,
          kind TEXT NOT NULL,
          title TEXT NOT NULL,
          description TEXT NOT NULL,
          author_id TEXT,
          author_name TEXT,
          duration_seconds FLOAT NOT NULL,
          cover_url TEXT,
          source_url TEXT,
          media_urls JSON NOT NULL,
          image_urls JSON NOT NULL,
          raw_metadata JSON NOT NULL,
          content_text TEXT NOT NULL,
          processing_state TEXT NOT NULL,
          process_error TEXT,
          process_attempts INTEGER NOT NULL,
          published_at DATETIME,
          last_seen_at DATETIME NOT NULL,
          created_at DATETIME NOT NULL,
          updated_at DATETIME NOT NULL
        );
        CREATE TABLE account_works (
          id INTEGER PRIMARY KEY,
          account_id INTEGER NOT NULL,
          work_id INTEGER NOT NULL,
          state TEXT NOT NULL,
          last_error_code TEXT
        );
        CREATE TABLE collections (
          id INTEGER PRIMARY KEY,
          title TEXT NOT NULL,
          cover_url TEXT,
          remote_order INTEGER NOT NULL
        );
        CREATE TABLE collection_memberships (
          id INTEGER PRIMARY KEY,
          collection_id INTEGER NOT NULL,
          work_id INTEGER NOT NULL,
          active BOOLEAN NOT NULL,
          first_seen_at DATETIME NOT NULL
        );
        CREATE TABLE knowledge_chunks (
          id INTEGER PRIMARY KEY,
          work_id INTEGER NOT NULL,
          chunk_index INTEGER NOT NULL,
          source_kind TEXT NOT NULL,
          text TEXT NOT NULL,
          start_seconds FLOAT,
          end_seconds FLOAT,
          embedding JSON,
          created_at DATETIME NOT NULL
        );
        CREATE TABLE keyframes (
          id INTEGER PRIMARY KEY,
          work_id INTEGER NOT NULL,
          timestamp_seconds FLOAT NOT NULL,
          scene_score FLOAT NOT NULL,
          path TEXT NOT NULL,
          perceptual_hash TEXT,
          created_at DATETIME NOT NULL
        );
        CREATE TABLE work_summaries (
          id INTEGER PRIMARY KEY,
          work_id INTEGER NOT NULL,
          status TEXT NOT NULL,
          one_sentence TEXT NOT NULL,
          content_json JSON NOT NULL,
          tags JSON NOT NULL,
          asset_ids JSON NOT NULL,
          model TEXT NOT NULL,
          prompt_version TEXT NOT NULL,
          source_digest TEXT NOT NULL,
          error TEXT,
          generated_at DATETIME,
          created_at DATETIME NOT NULL,
          updated_at DATETIME NOT NULL
        );
        CREATE TABLE accounts (id INTEGER PRIMARY KEY, nickname TEXT);
        CREATE TABLE sync_jobs (id TEXT PRIMARY KEY, state TEXT);
        CREATE TABLE adapter_probes (id INTEGER PRIMARY KEY, status TEXT);
        """
    )
    connection.executemany(
        "INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,?)",
        [
            ("schema", json.dumps({"version": 3}), now),
            ("active_account", json.dumps({"id": 1}), now),
            ("runtime", json.dumps({"max_keyframes": 12}), now),
        ],
    )
    connection.executemany(
        "INSERT INTO secret_records(name,encrypted_value,updated_at) VALUES(?,?,?)",
        [
            ("douyin_cookie", b"sensitive", now),
            ("dashscope_api_key", b"model-key", now),
        ],
    )
    for work_id, state in [(1, "processed"), (2, "processed"), (3, "discovered")]:
        connection.execute(
            """
            INSERT INTO works(
              id,platform,platform_work_id,kind,title,description,duration_seconds,
              media_urls,image_urls,raw_metadata,content_text,processing_state,
              process_attempts,last_seen_at,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                work_id,
                "douyin",
                f"legacy-{work_id}",
                "video",
                f"作品 {work_id}",
                "",
                0,
                "[]",
                "[]",
                "{}",
                "内容" if work_id == 1 else "",
                state,
                0,
                now,
                now,
                now,
            ),
        )
    connection.executemany(
        "INSERT INTO account_works(id,account_id,work_id,state,last_error_code) VALUES(?,?,?,?,?)",
        [
            (1, 1, 1, "in_library", None),
            (2, 2, 1, "archived", None),
            (3, 1, 2, "in_library", None),
            (4, 1, 3, "archived", None),
            (5, 2, 3, "candidate", None),
        ],
    )
    connection.execute(
        "INSERT INTO collections(id,title,cover_url,remote_order) VALUES(1,'历史收藏','cover',5)"
    )
    connection.execute(
        "INSERT INTO collection_memberships(id,collection_id,work_id,active,first_seen_at) VALUES(1,1,1,1,?)",
        (now,),
    )
    connection.execute(
        """
        INSERT INTO knowledge_chunks(
          id,work_id,chunk_index,source_kind,text,start_seconds,end_seconds,embedding,created_at
        ) VALUES(1,1,0,'metadata','保留知识',NULL,NULL,NULL,?)
        """,
        (now,),
    )
    connection.execute(
        "INSERT INTO keyframes(id,work_id,timestamp_seconds,scene_score,path,perceptual_hash,created_at) VALUES(1,1,2.5,0.8,'frame.jpg','hash',?)",
        (now,),
    )
    connection.execute(
        """
        INSERT INTO work_summaries(
          id,work_id,status,one_sentence,content_json,tags,asset_ids,model,
          prompt_version,source_digest,error,generated_at,created_at,updated_at
        ) VALUES(1,1,'ready','保留总结','{}','[]','[]','model','v1','digest',NULL,?,?,?)
        """,
        (now, now, now),
    )
    connection.commit()
    connection.close()


def test_v4_migration_backs_up_merges_accounts_and_drops_legacy_tables(tmp_path):
    path = tmp_path / "douyin_rag.db"
    create_legacy_database(path)
    assert schema_upgrade_needed(path)
    backup = backup_before_upgrade(path)
    assert backup and backup.is_file()
    assert backup.parent == tmp_path / "backups"
    assert "pre-v9" in backup.name

    migrate_to_v4(path)
    assert not schema_upgrade_needed(path)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"import_batches", "import_items", "jobs", "daily_link_quotas"} <= tables
        assert {"accounts", "account_works", "sync_jobs", "adapter_probes"}.isdisjoint(
            tables
        )
        states = {
            row["id"]: row["library_state"]
            for row in connection.execute("SELECT id,library_state FROM works")
        }
        assert states == {1: "in_library", 2: "issues", 3: "pending"}
        provenance = connection.execute(
            "SELECT DISTINCT import_source, refresh_policy FROM works"
        ).fetchall()
        assert [tuple(row) for row in provenance] == [("link", "f2")]
        secrets = {
            row[0] for row in connection.execute("SELECT name FROM secret_records")
        }
        assert secrets == {"dashscope_api_key", "f2_cookie"}
        settings = {
            row[0] for row in connection.execute("SELECT key FROM app_settings")
        }
        assert "active_account" not in settings
        assert "runtime" in settings
        groups = {row[0] for row in connection.execute("SELECT title FROM collections")}
        assert groups == {"历史收藏", "手动导入"}
        assert (
            connection.execute("SELECT COUNT(*) FROM knowledge_chunks").fetchone()[0]
            == 1
        )
        assert connection.execute("SELECT COUNT(*) FROM keyframes").fetchone()[0] == 1
        assert (
            connection.execute("SELECT one_sentence FROM work_summaries").fetchone()[0]
            == "保留总结"
        )
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        connection.close()


def test_v5_adds_collection_prompt_without_rebuilding_v4_data(tmp_path):
    path = tmp_path / "douyin_rag.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE app_settings (
          key TEXT PRIMARY KEY, value JSON NOT NULL, updated_at DATETIME NOT NULL
        );
        CREATE TABLE collections (
          id INTEGER PRIMARY KEY,
          key TEXT NOT NULL UNIQUE,
          title TEXT NOT NULL,
          cover_url TEXT,
          sort_order INTEGER NOT NULL,
          created_at DATETIME NOT NULL,
          updated_at DATETIME NOT NULL
        );
        CREATE TABLE import_items (
          id INTEGER PRIMARY KEY,
          status TEXT NOT NULL
        );
        INSERT INTO app_settings(key, value, updated_at)
        VALUES ('schema', '{"version": 4}', '2026-07-29T00:00:00+00:00');
        INSERT INTO collections(
          id, key, title, cover_url, sort_order, created_at, updated_at
        ) VALUES (
          7, 'manual-import', '手动导入', NULL, -1,
          '2026-07-29T00:00:00+00:00', '2026-07-29T00:00:00+00:00'
        );
        INSERT INTO import_items(id, status) VALUES (11, 'ready');
        """
    )
    connection.commit()
    connection.close()

    migrate_to_v5(path)

    connection = sqlite3.connect(path)
    try:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(collections)")
        }
        version = json.loads(
            connection.execute(
                "SELECT value FROM app_settings WHERE key='schema'"
            ).fetchone()[0]
        )["version"]
        assert "summary_prompt" in columns
        assert version == 5
        assert (
            connection.execute(
                "SELECT status FROM import_items WHERE id=11"
            ).fetchone()[0]
            == "ready"
        )
    finally:
        connection.close()


def test_v6_adds_explainable_keyframe_fields_without_losing_rows(tmp_path):
    path = tmp_path / "douyin_rag.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE app_settings (
          key TEXT PRIMARY KEY, value JSON NOT NULL, updated_at DATETIME NOT NULL
        );
        CREATE TABLE keyframes (
          id INTEGER PRIMARY KEY,
          work_id INTEGER NOT NULL,
          timestamp_seconds FLOAT NOT NULL,
          scene_score FLOAT NOT NULL,
          path TEXT NOT NULL,
          perceptual_hash TEXT,
          created_at DATETIME NOT NULL
        );
        INSERT INTO app_settings(key, value, updated_at)
        VALUES ('schema', '{"version": 5}', '2026-08-06T00:00:00+00:00');
        INSERT INTO keyframes(
          id, work_id, timestamp_seconds, scene_score, path, perceptual_hash, created_at
        ) VALUES (3, 1, 4.2, 0.8, 'frame.jpg', 'hash', '2026-08-06T00:00:00+00:00');
        """
    )
    connection.commit()
    connection.close()

    migrate_to_v6(path)

    connection = sqlite3.connect(path)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(keyframes)")}
        assert {
            "candidate_source",
            "selection_score",
            "selection_reason",
            "ocr_text",
            "visual_description",
        } <= columns
        assert connection.execute("SELECT COUNT(*) FROM keyframes").fetchone()[0] == 1
        assert (
            json.loads(
                connection.execute(
                    "SELECT value FROM app_settings WHERE key='schema'"
                ).fetchone()[0]
            )["version"]
            == 6
        )
    finally:
        connection.close()


def test_v7_adds_import_provenance_and_unique_indexes_without_losing_rows(tmp_path):
    path = tmp_path / "douyin_rag.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE app_settings (
          key TEXT PRIMARY KEY, value JSON NOT NULL, updated_at DATETIME NOT NULL
        );
        CREATE TABLE collections (id INTEGER PRIMARY KEY, title TEXT NOT NULL);
        CREATE TABLE works (id INTEGER PRIMARY KEY, title TEXT NOT NULL);
        CREATE TABLE import_batches (id TEXT PRIMARY KEY, raw_input TEXT NOT NULL);
        CREATE TABLE import_items (
          id INTEGER PRIMARY KEY,
          batch_id TEXT NOT NULL,
          ordinal INTEGER NOT NULL,
          input_url TEXT NOT NULL,
          normalized_url TEXT NOT NULL
        );
        INSERT INTO app_settings(key, value, updated_at)
        VALUES ('schema', '{"version": 6}', '2026-08-07T00:00:00+00:00');
        INSERT INTO collections(id, title) VALUES (7, 'Local imports');
        INSERT INTO works(id, title) VALUES (1, 'Existing work');
        INSERT INTO import_batches(id, raw_input) VALUES ('batch-1', 'existing');
        INSERT INTO import_items(
          id, batch_id, ordinal, input_url, normalized_url
        ) VALUES (3, 'batch-1', 0, 'https://example.test/1', 'https://example.test/1');
        """
    )
    connection.commit()
    connection.close()

    migrate_to_v7(path)
    migrate_to_v7(path)

    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        assert connection.execute(
            "SELECT import_source, refresh_policy FROM works WHERE id=1"
        ).fetchone() == ("link", "f2")
        assert connection.execute(
            "SELECT source_type, idempotency_key_hash, request_digest "
            "FROM import_batches WHERE id='batch-1'"
        ).fetchone() == ("link", None, None)
        assert connection.execute(
            "SELECT platform, client_item_id, target_collection_id "
            "FROM import_items WHERE id=3"
        ).fetchone() == ("douyin", None, None)
        assert (
            json.loads(
                connection.execute(
                    "SELECT value FROM app_settings WHERE key='schema'"
                ).fetchone()[0]
            )["version"]
            == 7
        )

        unique_indexes = {
            row[1]
            for table in ("import_batches", "import_items")
            for row in connection.execute(f"PRAGMA index_list({table})")
            if row[2]
        }
        assert "uq_import_batch_idempotency_key_hash" in unique_indexes
        assert "uq_import_item_batch_client_id" in unique_indexes

        foreign_keys = connection.execute(
            "PRAGMA foreign_key_list(import_items)"
        ).fetchall()
        assert any(
            row[2] == "collections"
            and row[3] == "target_collection_id"
            and row[4] == "id"
            and row[6] == "SET NULL"
            for row in foreign_keys
        )

        connection.execute(
            "UPDATE import_batches SET idempotency_key_hash='digest' WHERE id='batch-1'"
        )
        connection.execute(
            "INSERT INTO import_batches(id, raw_input) VALUES ('batch-2', 'new')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE import_batches SET idempotency_key_hash='digest' "
                "WHERE id='batch-2'"
            )

        connection.execute(
            "UPDATE import_items SET client_item_id='item-1', target_collection_id=7 "
            "WHERE id=3"
        )
        connection.execute(
            "INSERT INTO import_items(id, batch_id, ordinal, input_url, normalized_url) "
            "VALUES (4, 'batch-1', 1, 'https://example.test/2', "
            "'https://example.test/2')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE import_items SET client_item_id='item-1' WHERE id=4"
            )
        connection.execute("DELETE FROM collections WHERE id=7")
        assert (
            connection.execute(
                "SELECT target_collection_id FROM import_items WHERE id=3"
            ).fetchone()[0]
            is None
        )
        assert connection.execute("SELECT COUNT(*) FROM works").fetchone()[0] == 1
        assert (
            connection.execute("SELECT COUNT(*) FROM import_items").fetchone()[0] == 2
        )
    finally:
        connection.close()


def test_v8_adds_durable_package_file_staging_and_preserves_v7_batch(tmp_path):
    path = tmp_path / "douyin_rag.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE app_settings (
          key TEXT PRIMARY KEY, value JSON NOT NULL, updated_at DATETIME NOT NULL
        );
        CREATE TABLE import_batches (id TEXT PRIMARY KEY, raw_input TEXT NOT NULL);
        INSERT INTO app_settings(key, value, updated_at)
        VALUES ('schema', '{"version": 7}', '2026-08-08T00:00:00+00:00');
        INSERT INTO import_batches(id, raw_input) VALUES ('old-batch', 'existing');
        """
    )
    connection.commit()
    connection.close()

    migrate_to_v8(path)
    migrate_to_v8(path)

    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        assert connection.execute(
            "SELECT raw_input FROM import_batches WHERE id='old-batch'"
        ).fetchone() == ("existing",)
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(package_import_files)")
        }
        assert {
            "batch_id",
            "client_file_id",
            "relative_path",
            "path_hash",
            "stored_path",
            "sha256",
            "status",
        } <= columns
        assert (
            json.loads(
                connection.execute(
                    "SELECT value FROM app_settings WHERE key='schema'"
                ).fetchone()[0]
            )["version"]
            == 8
        )
        values = (
            "file-1",
            "old-batch",
            "client-1",
            "video.mp4",
            "hash",
            "video",
            "pending",
            10,
            0,
            "2026-08-08T00:00:00+00:00",
            "2026-08-08T00:00:00+00:00",
        )
        connection.execute(
            "INSERT INTO package_import_files("
            "id,batch_id,client_file_id,relative_path,path_hash,role,status,"
            "declared_size,size_bytes,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            values,
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO package_import_files("
                "id,batch_id,client_file_id,relative_path,path_hash,role,status,"
                "declared_size,size_bytes,created_at,updated_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                ("file-2", *values[1:]),
            )
    finally:
        connection.close()


def test_v9_adds_evidence_state_and_quarantines_only_restricted_title_notes(
    tmp_path,
):
    path = tmp_path / "douyin_rag.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE app_settings (
          key TEXT PRIMARY KEY, value JSON NOT NULL, updated_at DATETIME NOT NULL
        );
        CREATE TABLE works (
          id INTEGER PRIMARY KEY,
          kind TEXT NOT NULL,
          library_state TEXT NOT NULL,
          raw_metadata JSON NOT NULL,
          content_text TEXT NOT NULL
        );
        CREATE TABLE work_source_assets (
          id INTEGER PRIMARY KEY, work_id INTEGER, kind TEXT NOT NULL
        );
        CREATE TABLE knowledge_chunks (
          id INTEGER PRIMARY KEY, work_id INTEGER NOT NULL, source_kind TEXT NOT NULL
        );
        CREATE TABLE keyframes (
          id INTEGER PRIMARY KEY, work_id INTEGER NOT NULL,
          ocr_text TEXT, visual_description TEXT
        );
        CREATE TABLE work_summaries (
          id INTEGER PRIMARY KEY, work_id INTEGER NOT NULL
        );
        INSERT INTO app_settings(key, value, updated_at)
        VALUES ('schema', '{"version": 8}', '2026-08-08T00:00:00+00:00');
        INSERT INTO works(id,kind,library_state,raw_metadata,content_text) VALUES
          (1,'video','in_library','{"media_policy":{"download_permission":"denied"}}','title notes'),
          (2,'video','in_library','{"media_policy":{"download_permission":"unknown"}}','real transcript'),
          (3,'video','in_library','{"media_policy":{"download_permission":"denied"}}','local source'),
          (4,'video','in_library','{"media_policy":{"download_permission":"allowed"}}','allowed title');
        INSERT INTO work_source_assets(id,work_id,kind) VALUES (1,3,'video');
        INSERT INTO knowledge_chunks(id,work_id,source_kind) VALUES
          (1,1,'metadata'), (2,1,'notes'), (3,2,'transcript'), (4,4,'metadata');
        INSERT INTO keyframes(id,work_id,ocr_text,visual_description)
        VALUES (1,1,'','');
        INSERT INTO work_summaries(id,work_id) VALUES (1,1), (2,2), (3,4);
        """
    )
    connection.commit()
    connection.close()

    migrate_to_v9(path)
    migrate_to_v9(path)

    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(works)")}
        assert {
            "supplement_state",
            "supplement_reason",
            "evidence_state",
            "track_report",
        } <= columns
        rows = {
            row["id"]: row
            for row in connection.execute(
                "SELECT id,library_state,supplement_state,supplement_reason,"
                "evidence_state,content_text,track_report FROM works"
            )
        }
        assert rows[1]["library_state"] == "in_library"
        assert rows[1]["supplement_state"] == "required"
        assert rows[1]["supplement_reason"] == "full_video_unavailable"
        assert rows[1]["evidence_state"] == "insufficient"
        assert rows[1]["content_text"] == ""
        assert json.loads(rows[1]["track_report"])["video"]["available"] is False
        assert rows[2]["supplement_state"] == "required"
        assert rows[2]["evidence_state"] == "sufficient"
        assert rows[2]["content_text"] == "real transcript"
        assert rows[3]["supplement_state"] == "none"
        assert rows[3]["evidence_state"] == "unverified"
        assert rows[4]["supplement_state"] == "none"
        assert rows[4]["content_text"] == "allowed title"
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM knowledge_chunks WHERE work_id=1"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM work_summaries WHERE work_id=1"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM keyframes WHERE work_id=1"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM knowledge_chunks WHERE work_id=2"
            ).fetchone()[0]
            == 1
        )
        assert (
            json.loads(
                connection.execute(
                    "SELECT value FROM app_settings WHERE key='schema'"
                ).fetchone()[0]
            )["version"]
            == 9
        )
    finally:
        connection.close()
