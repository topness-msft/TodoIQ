"""Schema coverage for durable Cowork unread state."""

import sqlite3

from src import db


class TestSeenAtMigration:
    def test_fresh_db_has_nullable_seen_at(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.init_db(conn)

        columns = {
            row["name"]: row for row in conn.execute("PRAGMA table_info(task_actions)")
        }
        assert "seen_at" in columns
        assert columns["seen_at"]["notnull"] == 0

    def test_migration_adds_seen_at_to_legacy_db_and_preserves_rows(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        legacy_sql = "\n".join(
            line for line in db.SCHEMA_SQL.splitlines() if "seen_at" not in line
        )
        conn.executescript(legacy_sql)
        conn.execute("INSERT INTO tasks (title) VALUES ('Legacy task')")
        conn.execute(
            "INSERT INTO task_actions (task_id, state) VALUES (1, 'ready')"
        )

        db._migrate(conn)
        db._migrate(conn)

        row = conn.execute(
            "SELECT seen_at FROM task_actions WHERE task_id = 1"
        ).fetchone()
        assert row["seen_at"] is None
