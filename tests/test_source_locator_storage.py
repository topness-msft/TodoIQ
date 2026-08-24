"""The source_locator column, and recovering it for tasks that predate it.

1,270 of the 2,371 live tasks already carry a re-openable Teams conversation
inside `source_url`; nothing had ever read it out and stored it. The column is
added by the existing ALTER TABLE loop (no CHECK, so no table rebuild), and a
one-time pass at startup fills it in from the links already on disk.

The pass runs at startup rather than on read on purpose: deriving per request
would put ~1,900 regexes on every dashboard load of `list_tasks`, which fetches
the whole non-deleted set (src/models.py:241). Derive-on-read survives only as a
fallback for a task created after the pass ran.
"""

import json
import os
import sqlite3
import tempfile
import unittest

import src.db as db_module
from src.services import source_locator as sl


ONE_TO_ONE = (
    "https://teams.microsoft.com/l/message/"
    "19:aaa_bbb@unq.gbl.spaces/1756000000000"
)
GROUP = "https://teams.microsoft.com/l/message/19:ccc@thread.v2/1756000000001"
OUTLOOK = "https://outlook.office365.com/mail/inbox/id/AAQkAG"


class TestSourceLocatorColumn(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.original = db_module.DB_PATH
        db_module.DB_PATH = self.tmp.name

    def tearDown(self):
        db_module.DB_PATH = self.original
        os.unlink(self.tmp.name)

    def _fresh(self):
        conn = db_module.get_connection()
        db_module.init_db(conn)
        return conn

    def _columns(self, conn):
        return [r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()]

    def test_the_column_exists_after_init(self):
        conn = self._fresh()
        self.assertIn("source_locator", self._columns(conn))
        conn.close()

    def test_an_older_database_gains_the_column(self):
        conn = self._fresh()
        # Simulate a database written before the column existed.
        db_module._rebuild_tasks_constraints  # referenced so the intent is clear
        conn.execute("ALTER TABLE tasks DROP COLUMN source_locator")
        conn.commit()
        self.assertNotIn("source_locator", self._columns(conn))
        conn.close()

        conn = db_module.get_connection()
        db_module.init_db(conn)
        self.assertIn("source_locator", self._columns(conn))
        conn.close()

    def test_adding_the_column_does_not_disturb_existing_rows(self):
        conn = self._fresh()
        conn.execute(
            "INSERT INTO tasks (title, status, source_url) VALUES (?,?,?)",
            ("Existing", "waiting", ONE_TO_ONE),
        )
        conn.commit()
        before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        conn.execute("ALTER TABLE tasks DROP COLUMN source_locator")
        conn.commit()
        conn.close()

        conn = db_module.get_connection()
        db_module.init_db(conn)
        after = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        self.assertEqual(before, after)
        conn.close()


class TestBackfillFromStoredLinks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.original = db_module.DB_PATH
        db_module.DB_PATH = self.tmp.name
        conn = db_module.get_connection()
        db_module.init_db(conn)
        conn.close()

    def tearDown(self):
        db_module.DB_PATH = self.original
        os.unlink(self.tmp.name)

    def _add(self, title, url, locator=None):
        conn = db_module.get_connection()
        conn.execute(
            "INSERT INTO tasks (title, status, source_url, source_locator) "
            "VALUES (?,?,?,?)",
            (title, "waiting", url, locator),
        )
        conn.commit()
        task_id = conn.execute("SELECT MAX(id) FROM tasks").fetchone()[0]
        conn.close()
        return task_id

    def _locator(self, task_id):
        conn = db_module.get_connection()
        row = conn.execute(
            "SELECT source_locator FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        conn.close()
        return row[0]

    def _run(self):
        conn = db_module.get_connection()
        filled = db_module.backfill_source_locators(conn)
        conn.close()
        return filled

    def test_a_teams_link_is_recovered(self):
        task_id = self._add("Chat task", ONE_TO_ONE)
        self.assertEqual(self._run(), 1)
        stored = sl.normalise(self._locator(task_id))
        self.assertEqual(stored["kind"], sl.KIND_TEAMS_CHAT)
        self.assertTrue(sl.is_thread_readable(stored))

    def test_a_recovered_locator_is_marked_derived_not_captured(self):
        task_id = self._add("Chat task", GROUP)
        self._run()
        self.assertEqual(sl.normalise(self._locator(task_id))["source"],
                         sl.SOURCE_DERIVED)

    def test_an_unparseable_link_is_left_alone(self):
        task_id = self._add("Mail task", OUTLOOK)
        self._run()
        self.assertIsNone(self._locator(task_id))

    def test_a_task_with_no_link_is_left_alone(self):
        task_id = self._add("Manual task", None)
        self._run()
        self.assertIsNone(self._locator(task_id))

    def test_a_captured_locator_is_never_overwritten(self):
        # Something recorded at creation is better evidence than anything we
        # can reconstruct from a link afterwards.
        captured = json.dumps({
            "version": 1, "kind": "teams_chat",
            "conversation_id": "19:captured@thread.v2",
            "message_id": None, "team_id": None, "channel_id": None,
            "internet_message_id": None, "event_id": None,
            "source": "captured",
        })
        task_id = self._add("Already known", ONE_TO_ONE, locator=captured)
        self._run()
        stored = sl.normalise(self._locator(task_id))
        self.assertEqual(stored["conversation_id"], "19:captured@thread.v2")
        self.assertEqual(stored["source"], sl.SOURCE_CAPTURED)

    def test_running_twice_changes_nothing_the_second_time(self):
        self._add("Chat task", ONE_TO_ONE)
        self.assertEqual(self._run(), 1)
        self.assertEqual(self._run(), 0)

    def test_it_reports_how_many_it_filled(self):
        self._add("a", ONE_TO_ONE)
        self._add("b", GROUP)
        self._add("c", OUTLOOK)
        self._add("d", None)
        self.assertEqual(self._run(), 2)

    def test_init_db_runs_the_backfill_itself(self):
        task_id = self._add("Chat task", ONE_TO_ONE)
        conn = db_module.get_connection()
        db_module.init_db(conn)
        conn.close()
        self.assertIsNotNone(self._locator(task_id))


class TestReadFallback(unittest.TestCase):
    """A task created after the startup pass still resolves."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.original = db_module.DB_PATH
        db_module.DB_PATH = self.tmp.name
        conn = db_module.get_connection()
        db_module.init_db(conn)
        conn.close()

    def tearDown(self):
        db_module.DB_PATH = self.original
        os.unlink(self.tmp.name)

    def test_a_task_read_back_exposes_its_locator(self):
        from src.models import create_task, get_task

        task = create_task(title="Chat task", source_url=ONE_TO_ONE)
        got = get_task(task["id"])
        self.assertTrue(got["source_locator_resolved"]["thread_readable"])
        self.assertEqual(
            got["source_locator_resolved"]["locator"]["kind"], sl.KIND_TEAMS_CHAT)

    def test_a_task_with_no_usable_source_says_so(self):
        from src.models import create_task, get_task

        task = create_task(title="Manual", source_url=None)
        got = get_task(task["id"])
        self.assertFalse(got["source_locator_resolved"]["thread_readable"])
        self.assertIsNone(got["source_locator_resolved"]["locator"])

    def test_the_stored_column_wins_over_the_link(self):
        from src.models import create_task, get_task, update_task

        task = create_task(title="Chat task", source_url=ONE_TO_ONE)
        update_task(task["id"], source_locator=json.dumps({
            "version": 1, "kind": "teams_chat",
            "conversation_id": "19:stored@thread.v2",
            "source": "captured",
        }))
        got = get_task(task["id"])
        self.assertEqual(
            got["source_locator_resolved"]["locator"]["conversation_id"],
            "19:stored@thread.v2")


if __name__ == "__main__":
    unittest.main()
