"""Tests for database schema and initialization."""

import unittest
import sqlite3
import tempfile
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from src.db import init_db, SCHEMA_SQL


class TestDatabaseSchema(unittest.TestCase):
    """Test init_db creates correct schema and constraints."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        self.conn = sqlite3.connect(self.tmp.name)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def _get_tables(self):
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        return {r["name"] for r in rows}

    def test_init_db_creates_all_tables(self):
        tables = self._get_tables()
        expected = {
            "tasks", "task_context", "refresh_schedule", "sync_log",
            "briefing_cache",
            "person", "person_alias", "person_merge_history", "task_person",
        }
        self.assertEqual(tables, expected)

    def test_tasks_table_columns(self):
        rows = self.conn.execute("PRAGMA table_info(tasks)").fetchall()
        cols = {r["name"] for r in rows}
        expected = {
            "id", "title", "description", "status", "snoozed_until",
            "parse_status", "raw_input", "error_message", "is_quick_hit",
            "priority", "due_date", "committed_date",
            "source_type", "source_id", "source_url", "source_snippet",
            "source_date", "coaching_text", "action_type", "skill_output",
            "cowork_prompt", "key_people", "related_meeting", "user_notes",
            "waiting_activity", "suggestion_refreshed_at",
            "shadow_dup_of", "shadow_dup_reason", "shadow_checked_at",
            "created_at", "updated_at",
        }
        self.assertEqual(cols, expected)

    def test_task_context_table_columns(self):
        rows = self.conn.execute("PRAGMA table_info(task_context)").fetchall()
        cols = {r["name"] for r in rows}
        expected = {"id", "task_id", "context_type", "content", "query_used", "fetched_at"}
        self.assertEqual(cols, expected)

    def test_refresh_schedule_table_columns(self):
        rows = self.conn.execute("PRAGMA table_info(refresh_schedule)").fetchall()
        cols = {r["name"] for r in rows}
        expected = {
            "task_id", "interval_minutes", "next_refresh_at",
            "last_refresh_at", "consecutive_no_change",
        }
        self.assertEqual(cols, expected)

    def test_sync_log_table_columns(self):
        rows = self.conn.execute("PRAGMA table_info(sync_log)").fetchall()
        cols = {r["name"] for r in rows}
        expected = {
            "id", "sync_type", "result_summary",
            "tasks_created", "tasks_updated", "synced_at",
        }
        self.assertEqual(cols, expected)

    def test_invalid_status_raises_error(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO tasks (title, status) VALUES (?, ?)",
                ("test", "bogus_status"),
            )

    def test_invalid_priority_raises_error(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO tasks (title, priority) VALUES (?, ?)",
                ("test", 0),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO tasks (title, priority) VALUES (?, ?)",
                ("test", 6),
            )

    def test_foreign_key_constraint_task_context(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO task_context (task_id, context_type, content) VALUES (?, ?, ?)",
                (9999, "email_thread", "some content"),
            )

    def test_invalid_context_type_raises_error(self):
        # First create a valid task
        self.conn.execute("INSERT INTO tasks (title) VALUES (?)", ("test",))
        self.conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO task_context (task_id, context_type, content) VALUES (?, ?, ?)",
                (1, "invalid_type", "some content"),
            )

    def test_invalid_sync_type_raises_error(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO sync_log (sync_type) VALUES (?)",
                ("invalid_sync",),
            )

    def test_init_db_is_idempotent(self):
        # Running init_db a second time should not raise
        init_db(self.conn)
        tables = self._get_tables()
        expected = {
            "tasks", "task_context", "refresh_schedule", "sync_log",
            "briefing_cache",
            "person", "person_alias", "person_merge_history", "task_person",
        }
        self.assertEqual(tables, expected)


if __name__ == "__main__":
    unittest.main()
