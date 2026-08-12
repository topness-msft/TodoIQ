"""Schema coverage for preview-only destination binding."""

import sqlite3

from src import db


EXPECTED = {
    "delivery_channel",
    "destination_display",
    "destination_confirmed_at",
    "destination_source",
}


class TestDestinationSchema:
    def test_fresh_db_has_destination_columns(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.init_db(conn)
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(task_actions)")
        }
        assert EXPECTED <= columns

    def test_legacy_migration_is_idempotent_and_backfills_null(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        legacy = "\n".join(
            line
            for line in db.SCHEMA_SQL.splitlines()
            if not any(column in line for column in EXPECTED)
        )
        conn.executescript(legacy)
        conn.execute("INSERT INTO tasks (title) VALUES ('Legacy destination')")
        conn.execute(
            "INSERT INTO task_actions (task_id, state) VALUES (1, 'ready')"
        )

        db._migrate(conn)
        db._migrate(conn)

        row = conn.execute("SELECT * FROM task_actions WHERE id=1").fetchone()
        assert all(row[column] is None for column in EXPECTED)
