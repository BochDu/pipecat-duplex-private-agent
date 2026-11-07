"""建表版本化与删库重建。"""

from app.storage import SCHEMA_VERSION, Storage, reset_database


def test_init_sets_user_version_and_is_idempotent(tmp_path):
    storage = Storage(tmp_path).init()
    version = storage.conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == SCHEMA_VERSION
    tables = {
        row[0]
        for row in storage.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert "conversations" in tables
    storage.close()

    again = Storage(tmp_path).init()
    assert again.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    again.close()


def test_reset_database_removes_db_and_wal(tmp_path):
    Storage(tmp_path).init().close()
    removed = reset_database(tmp_path)

    assert {path.name for path in removed} >= {"agent.db"}
    assert reset_database(tmp_path) == []
