"""Schema coverage for persisted Cowork island routing."""

import sqlite3

from src import db


class TestIslandSchema:
    def test_fresh_db_has_nullable_island_url(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.init_db(conn)
        columns = {
            row["name"]: row for row in conn.execute("PRAGMA table_info(task_actions)")
        }
        assert "island_url" in columns
        assert columns["island_url"]["notnull"] == 0

    def test_legacy_migration_is_idempotent_and_backfills_null(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        legacy = "\n".join(
            line for line in db.SCHEMA_SQL.splitlines() if "island_url" not in line
        )
        conn.executescript(legacy)
        conn.execute("INSERT INTO tasks (title) VALUES ('Legacy island task')")
        conn.execute(
            "INSERT INTO task_actions (task_id, state) VALUES (1, 'ready')"
        )

        db._migrate(conn)
        db._migrate(conn)

        row = conn.execute(
            "SELECT island_url FROM task_actions WHERE task_id = 1"
        ).fetchone()
        assert row["island_url"] is None
