import json
import sqlite3
from pathlib import Path

from app.services.migrations import (
    backup_before_upgrade,
    migrate_to_v4,
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
        assert {"accounts", "account_works", "sync_jobs", "adapter_probes"}.isdisjoint(tables)
        states = {
            row["id"]: row["library_state"]
            for row in connection.execute("SELECT id,library_state FROM works")
        }
        assert states == {1: "in_library", 2: "issues", 3: "pending"}
        secrets = {
            row[0] for row in connection.execute("SELECT name FROM secret_records")
        }
        assert secrets == {"dashscope_api_key", "f2_cookie"}
        settings = {
            row[0] for row in connection.execute("SELECT key FROM app_settings")
        }
        assert "active_account" not in settings
        assert "runtime" in settings
        groups = {
            row[0] for row in connection.execute("SELECT title FROM collections")
        }
        assert groups == {"历史收藏", "手动导入"}
        assert connection.execute("SELECT COUNT(*) FROM knowledge_chunks").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM keyframes").fetchone()[0] == 1
        assert connection.execute("SELECT one_sentence FROM work_summaries").fetchone()[0] == "保留总结"
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        connection.close()
