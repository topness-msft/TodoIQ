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
        expected = {"tasks", "task_context", "refresh_schedule", "sync_log", "task_actions"}
        self.assertEqual(tables, expected)

    def test_tasks_table_columns(self):
        rows = self.conn.execute("PRAGMA table_info(tasks)").fetchall()
        cols = {r["name"] for r in rows}
        expected = {
            "id", "title", "description", "status", "parse_status",
            "raw_input", "priority", "due_date", "committed_date",
            "source_type", "source_id", "source_url", "source_snippet",
            "coaching_text", "key_people", "related_meeting", "user_notes",
            "suggestion_refreshed_at", "created_at", "updated_at",
            "action_type", "is_quick_hit", "error_message", "cowork_prompt",
            "snoozed_until", "skill_output", "waiting_activity",
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
        expected = {"tasks", "task_context", "refresh_schedule", "sync_log", "task_actions"}
        self.assertEqual(tables, expected)


class TestTaskActionsSchema(unittest.TestCase):
    """task_actions stores one Cowork preview attempt per row.

    The state CHECK constraint is the Phase 2 gate expressed as schema rather than
    UI discipline: no execute state may be reachable while Phase 1 ships.
    """

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        self.conn = sqlite3.connect(self.tmp.name)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        init_db(self.conn)
        self.conn.execute("INSERT INTO tasks (id, title) VALUES (1, 'Target task')")

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def _insert(self, **kwargs):
        fields = {"task_id": 1, "action_type": "follow-up", "state": "previewing"}
        fields.update(kwargs)
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        return self.conn.execute(
            f"INSERT INTO task_actions ({cols}) VALUES ({marks})", tuple(fields.values())
        )

    def test_task_actions_columns(self):
        rows = self.conn.execute("PRAGMA table_info(task_actions)").fetchall()
        cols = {r["name"] for r in rows}
        expected = {
            "id", "task_id", "action_type", "state",
            "intent", "notes_snapshot", "redirect_text", "composed_prompt",
            "finding", "draft", "draft_edited",
            "destination_kind", "destination_ref", "conversation_id",
            "terminal_status", "tool_trace", "cost_credits", "error",
            "seen_at", "island_url", "created_at", "updated_at",
            "delivery_channel", "destination_display",
            "destination_confirmed_at", "destination_source",
            "blocked_question",
            "answered_interaction",
            "interaction_mode",
            "completed_at",
            "had_interaction",
            # A refine turn continues an existing Cowork conversation. It is
            # still its own row so the correction chain stays auditable, and
            # this points back at the attempt it refines.
            "parent_action_id",
        }
        self.assertEqual(cols, expected)

    def test_interaction_mode_defaults_to_interaction(self):
        self.conn.execute("INSERT INTO task_actions (task_id) VALUES (1)")
        row = self.conn.execute(
            "SELECT interaction_mode FROM task_actions"
        ).fetchone()
        self.assertEqual(row["interaction_mode"], "interaction")

    def test_interaction_mode_rejects_unknown_values(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert(interaction_mode="surprise_me")

    def test_blocked_question_is_nullable(self):
        rows = self.conn.execute("PRAGMA table_info(task_actions)").fetchall()
        column = next(r for r in rows if r["name"] == "blocked_question")
        self.assertEqual(column["notnull"], 0)

    def test_phase1_states_are_accepted(self):
        for state in ("previewing", "ready", "failed"):
            with self.subTest(state=state):
                self._insert(state=state)

    def test_execute_states_are_rejected(self):
        # Phase 1 ships preview only. If any of these become insertable, a code path
        # can claim to have executed against M365 -- the gate must be in the DB.
        for state in ("executing", "executed", "approved", "running"):
            with self.subTest(state=state):
                with self.assertRaises(sqlite3.IntegrityError):
                    self._insert(state=state)

    def test_destination_kind_constrained(self):
        # Must accept every kind parse_source_url can emit -- a mismatch here would
        # only surface at preview time, as an IntegrityError on a real task.
        for kind in ("one_to_one", "group", "meeting", "channel", "unknown", "none"):
            with self.subTest(kind=kind):
                self._insert(destination_kind=kind)
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert(destination_kind="broadcast")

    def test_destination_kind_accepts_all_parser_outputs(self):
        from src.services.cowork_runner import parse_source_url
        urls = [
            None,
            "https://teams.microsoft.com/l/message/19:a_b@unq.gbl.spaces/1",
            "https://teams.microsoft.com/l/message/19:x@thread.v2/1",
            "https://teams.microsoft.com/l/message/19:meeting_x@thread.v2/1",
            "https://teams.microsoft.com/l/message/19:x@thread.skype/1",
            "https://teams.microsoft.com/l/message/19:x@thread.future/1",
            "https://outlook.office365.com/owa/?ItemID=abc",
        ]
        for u in urls:
            kind = parse_source_url(u)["kind"]
            with self.subTest(kind=kind):
                self._insert(destination_kind=kind)

    def test_defaults_state_to_previewing(self):
        self.conn.execute("INSERT INTO task_actions (task_id) VALUES (1)")
        row = self.conn.execute("SELECT state FROM task_actions").fetchone()
        self.assertEqual(row["state"], "previewing")

    def test_deleting_task_cascades_to_actions(self):
        self._insert()
        self.conn.execute("DELETE FROM tasks WHERE id = 1")
        remaining = self.conn.execute("SELECT COUNT(*) c FROM task_actions").fetchone()
        self.assertEqual(remaining["c"], 0)

    def test_orphan_action_rejected(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert(task_id=99999)

    def test_rows_survive_reinit(self):
        # init_db runs on every server start; an existing preview must not be dropped.
        self._insert(state="ready", draft="hello")
        init_db(self.conn)
        row = self.conn.execute("SELECT draft FROM task_actions").fetchone()
        self.assertEqual(row["draft"], "hello")

    def test_restart_recovery_fails_a_blocked_interaction_without_its_subscriber(self):
        import src.db as db_module
        from src.models import recover_stuck_previews

        self._insert(
            state="previewing",
            blocked_question="Which account should I use?",
        )
        self.conn.commit()
        original = db_module.DB_PATH
        db_module.DB_PATH = self.tmp.name
        try:
            self.assertEqual(recover_stuck_previews(), 1)
        finally:
            db_module.DB_PATH = original
        row = self.conn.execute("SELECT state FROM task_actions").fetchone()
        self.assertEqual(row["state"], "failed")

    def test_restart_recovery_still_fails_an_unblocked_preview(self):
        import src.db as db_module
        from src.models import recover_stuck_previews

        self._insert(state="previewing")
        self.conn.commit()
        original = db_module.DB_PATH
        db_module.DB_PATH = self.tmp.name
        try:
            self.assertEqual(recover_stuck_previews(), 1)
        finally:
            db_module.DB_PATH = original
        row = self.conn.execute("SELECT state FROM task_actions").fetchone()
        self.assertEqual(row["state"], "failed")

    def test_late_question_recovery_cannot_overwrite_answered_sentinel(self):
        import src.db as db_module
        from src.models import set_blocked_question_if_missing

        cursor = self._insert(
            state="previewing",
            blocked_question="",
            answered_interaction="Stale question?",
        )
        self.conn.commit()
        original = db_module.DB_PATH
        db_module.DB_PATH = self.tmp.name
        try:
            updated = set_blocked_question_if_missing(
                cursor.lastrowid, "Stale question?"
            )
        finally:
            db_module.DB_PATH = original
        self.assertIsNone(updated)
        row = self.conn.execute(
            "SELECT blocked_question, answered_interaction, had_interaction "
            "FROM task_actions WHERE id=?",
            (cursor.lastrowid,),
        ).fetchone()
        self.assertEqual(row["blocked_question"], "")
        self.assertEqual(row["answered_interaction"], "Stale question?")

    def test_only_one_answer_can_claim_a_pending_question(self):
        import src.db as db_module
        from src.models import claim_blocked_question_answer

        cursor = self._insert(
            state="previewing", blocked_question="Choose A or B?"
        )
        self.conn.commit()
        original = db_module.DB_PATH
        db_module.DB_PATH = self.tmp.name
        try:
            self.assertTrue(claim_blocked_question_answer(
                cursor.lastrowid, "Choose A or B?"
            ))
            self.assertFalse(claim_blocked_question_answer(
                cursor.lastrowid, "Choose A or B?"
            ))
        finally:
            db_module.DB_PATH = original
        row = self.conn.execute(
            "SELECT blocked_question, answered_interaction, had_interaction "
            "FROM task_actions WHERE id=?",
            (cursor.lastrowid,),
        ).fetchone()
        self.assertEqual(row["blocked_question"], "")
        self.assertEqual(row["answered_interaction"], "Choose A or B?")
        self.assertEqual(row["had_interaction"], 1)

    def test_resume_cleanup_does_not_erase_a_concurrent_answer_claim(self):
        import src.db as db_module
        from src.models import clear_blocked_question_if_unchanged

        cursor = self._insert(
            state="previewing",
            blocked_question="",
            answered_interaction="New question?",
        )
        self.conn.commit()
        original = db_module.DB_PATH
        db_module.DB_PATH = self.tmp.name
        try:
            self.assertFalse(clear_blocked_question_if_unchanged(
                cursor.lastrowid, "Old question?", None
            ))
        finally:
            db_module.DB_PATH = original
        row = self.conn.execute(
            "SELECT blocked_question, answered_interaction "
            "FROM task_actions WHERE id=?",
            (cursor.lastrowid,),
        ).fetchone()
        self.assertEqual(row["blocked_question"], "")
        self.assertEqual(row["answered_interaction"], "New question?")

    def test_stopped_action_cannot_claim_or_send_an_answer(self):
        import src.db as db_module
        from src.models import claim_blocked_question_answer

        cursor = self._insert(
            state="failed", blocked_question="Choose A or B?"
        )
        self.conn.commit()
        original = db_module.DB_PATH
        db_module.DB_PATH = self.tmp.name
        try:
            self.assertFalse(claim_blocked_question_answer(
                cursor.lastrowid, "Choose A or B?"
            ))
        finally:
            db_module.DB_PATH = original


if __name__ == "__main__":
    unittest.main()
