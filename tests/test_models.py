"""Tests for task CRUD and lifecycle operations."""

import unittest
import tempfile
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


class TestModels(unittest.TestCase):
    """Test CRUD, lifecycle, context, sync, and stats in src/models.py."""

    def setUp(self):
        import src.db as db_module
        self.db_module = db_module
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        db_module.DB_PATH = self.tmp.name
        conn = db_module.get_connection()
        db_module.init_db(conn)
        conn.close()

    def tearDown(self):
        os.unlink(self.tmp.name)

    # ── CRUD ──

    def test_create_task(self):
        from src.models import create_task
        task = create_task(
            title="Buy groceries",
            description="Milk, eggs, bread",
            priority=2,
            due_date="2026-02-20",
        )
        self.assertIsNotNone(task)
        self.assertEqual(task["title"], "Buy groceries")
        self.assertEqual(task["description"], "Milk, eggs, bread")
        self.assertEqual(task["status"], "active")
        self.assertEqual(task["parse_status"], "parsed")
        self.assertEqual(task["priority"], 2)
        self.assertEqual(task["due_date"], "2026-02-20")
        self.assertIsNotNone(task["created_at"])
        self.assertIsNotNone(task["updated_at"])
        self.assertIsNotNone(task["id"])

    def test_list_tasks(self):
        from src.models import create_task, list_tasks
        create_task(title="Task A", status="active")
        create_task(title="Task B", status="active")
        create_task(title="Task C", status="suggested")

        all_tasks = list_tasks()
        self.assertEqual(len(all_tasks), 3)

        active = list_tasks(status="active")
        self.assertEqual(len(active), 2)

        suggested = list_tasks(status="suggested")
        self.assertEqual(len(suggested), 1)
        self.assertEqual(suggested[0]["title"], "Task C")

    def test_update_task(self):
        from src.models import create_task, update_task
        task = create_task(title="Original")
        original_updated = task["updated_at"]

        # Small delay so updated_at can differ
        time.sleep(0.05)
        updated = update_task(task["id"], title="Modified", priority=1)
        self.assertEqual(updated["title"], "Modified")
        self.assertEqual(updated["priority"], 1)
        # updated_at should change (or at least not be None)
        self.assertIsNotNone(updated["updated_at"])

    def test_update_task_no_fields(self):
        from src.models import create_task, update_task
        task = create_task(title="No change")
        result = update_task(task["id"])
        self.assertEqual(result["title"], "No change")

    def test_delete_task(self):
        from src.models import create_task, delete_task, get_task
        task = create_task(title="To delete")
        self.assertTrue(delete_task(task["id"]))
        self.assertIsNone(get_task(task["id"]))

    def test_delete_nonexistent(self):
        from src.models import delete_task
        self.assertFalse(delete_task(99999))

    # ── Lifecycle ──

    def test_promote_task(self):
        from src.models import create_task, promote_task
        task = create_task(title="Suggestion", status="suggested")
        promoted = promote_task(task["id"])
        self.assertEqual(promoted["status"], "active")

    def test_dismiss_task_from_suggested(self):
        from src.models import create_task, dismiss_task
        task = create_task(title="Suggestion", status="suggested")
        dismissed = dismiss_task(task["id"])
        self.assertEqual(dismissed["status"], "dismissed")

    def test_dismiss_task_from_active(self):
        from src.models import create_task, dismiss_task
        task = create_task(title="Active task", status="active")
        dismissed = dismiss_task(task["id"])
        self.assertEqual(dismissed["status"], "dismissed")

    def test_complete_task_from_active(self):
        from src.models import create_task, complete_task
        task = create_task(title="Active", status="active")
        completed = complete_task(task["id"])
        self.assertEqual(completed["status"], "completed")

    def test_complete_task_from_in_progress(self):
        from src.models import create_task, start_task, complete_task
        task = create_task(title="Active", status="active")
        start_task(task["id"])
        completed = complete_task(task["id"])
        self.assertEqual(completed["status"], "completed")

    def test_start_task(self):
        from src.models import create_task, start_task
        task = create_task(title="Active", status="active")
        started = start_task(task["id"])
        self.assertEqual(started["status"], "in_progress")

    def test_invalid_transition_completed_to_dismissed(self):
        from src.models import create_task, complete_task, dismiss_task
        task = create_task(title="Active", status="active")
        complete_task(task["id"])
        with self.assertRaises(ValueError):
            dismiss_task(task["id"])

    def test_invalid_transition_suggested_to_completed(self):
        from src.models import create_task, complete_task
        task = create_task(title="Suggestion", status="suggested")
        with self.assertRaises(ValueError):
            complete_task(task["id"])

    def test_transition_nonexistent_task(self):
        from src.models import promote_task
        result = promote_task(99999)
        self.assertIsNone(result)

    # ── Stats ──

    def test_get_stats(self):
        from src.models import create_task, get_stats
        create_task(title="A", status="active")
        create_task(title="B", status="active")
        create_task(title="C", status="suggested")
        stats = get_stats()
        self.assertEqual(stats.get("active"), 2)
        self.assertEqual(stats.get("suggested"), 1)
        self.assertEqual(stats["total"], 3)

    def test_get_stats_empty(self):
        from src.models import get_stats
        stats = get_stats()
        self.assertEqual(stats["total"], 0)

    # ── Context ──

    def test_add_context(self):
        from src.models import create_task, add_context, get_contexts
        task = create_task(title="Task with context")
        ctx = add_context(
            task_id=task["id"],
            context_type="email_thread",
            content="Email body here",
            query_used="search query",
        )
        self.assertEqual(ctx["task_id"], task["id"])
        self.assertEqual(ctx["context_type"], "email_thread")
        self.assertEqual(ctx["content"], "Email body here")
        self.assertEqual(ctx["query_used"], "search query")

        contexts = get_contexts(task["id"])
        self.assertEqual(len(contexts), 1)

    def test_add_multiple_contexts(self):
        from src.models import create_task, add_context, get_contexts
        task = create_task(title="Multi context")
        add_context(task["id"], "email_thread", "First email")
        add_context(task["id"], "meeting", "Meeting notes")
        contexts = get_contexts(task["id"])
        self.assertEqual(len(contexts), 2)

    # ── Sync Log ──

    def test_log_sync(self):
        from src.models import log_sync
        entry = log_sync(
            sync_type="flagged_emails",
            result_summary="Found 3 emails",
            tasks_created=2,
            tasks_updated=1,
        )
        self.assertEqual(entry["sync_type"], "flagged_emails")
        self.assertEqual(entry["result_summary"], "Found 3 emails")
        self.assertEqual(entry["tasks_created"], 2)
        self.assertEqual(entry["tasks_updated"], 1)

    def test_get_last_sync(self):
        from src.models import log_sync, get_last_sync
        log_sync(sync_type="flagged_emails", result_summary="First")
        log_sync(sync_type="meetings", result_summary="Second")
        log_sync(sync_type="flagged_emails", result_summary="Third")

        last_any = get_last_sync()
        self.assertIsNotNone(last_any)

        last_email = get_last_sync(sync_type="flagged_emails")
        self.assertEqual(last_email["result_summary"], "Third")

        last_meeting = get_last_sync(sync_type="meetings")
        self.assertEqual(last_meeting["result_summary"], "Second")

    def test_get_last_sync_empty(self):
        from src.models import get_last_sync
        self.assertIsNone(get_last_sync())
        self.assertIsNone(get_last_sync("flagged_emails"))

    # ── Fuzzy Source-ID Dedup ──

    def test_normalize_source_id_basic(self):
        from src.models import normalize_source_id
        result = normalize_source_id("chat::spant@microsoft.com::power up okr and numbers")
        self.assertIsNotNone(result)
        src_type, person, tokens = result
        self.assertEqual(src_type, "chat")
        self.assertEqual(person, "spant")
        self.assertIn("power", tokens)
        self.assertIn("okr", tokens)
        self.assertIn("numbers", tokens)
        # stop words removed
        self.assertNotIn("and", tokens)
        self.assertNotIn("up", tokens)

    def test_normalize_source_id_no_domain(self):
        from src.models import normalize_source_id
        result = normalize_source_id("chat::spant::power up latest okr and numbers")
        self.assertIsNotNone(result)
        _, person, _ = result
        self.assertEqual(person, "spant")

    def test_normalize_source_id_bad_format(self):
        from src.models import normalize_source_id
        self.assertIsNone(normalize_source_id(""))
        self.assertIsNone(normalize_source_id(None))
        self.assertIsNone(normalize_source_id("just-a-string"))
        self.assertIsNone(normalize_source_id("only::two"))

    def test_user_examples_all_match(self):
        """The 4 source_ids from the bug report should all be considered duplicates."""
        from src.models import normalize_source_id, _jaccard

        ids = [
            "chat::spant@microsoft.com::power up okr and numbers",
            "chat::spant::power up latest okr and numbers",
            "chat::saurabh.pant@microsoft.com::power up slides okrs metrics",
            "chat::spant@microsoft.com::power up latest okr and numbers",
        ]
        parsed = [normalize_source_id(sid) for sid in ids]
        for p in parsed:
            self.assertIsNotNone(p)

        # IDs 0, 1, 3 share person alias "spant"
        self.assertEqual(parsed[0][1], parsed[1][1])
        self.assertEqual(parsed[0][1], parsed[3][1])

        # ID 2 has different alias "saurabh.pant" (different person alias)
        self.assertNotEqual(parsed[0][1], parsed[2][1])

        # Token overlap between 0 and 1 should exceed 0.5
        self.assertGreaterEqual(_jaccard(parsed[0][2], parsed[1][2]), 0.5)
        # Token overlap between 0 and 3
        self.assertGreaterEqual(_jaccard(parsed[0][2], parsed[3][2]), 0.5)

    def test_find_similar_source_matches(self):
        from src.models import create_task, find_similar_source
        from src.db import get_connection
        # Create an existing task with a source_id
        create_task(
            title="Power up OKR",
            source_type="chat",
            source_id="chat::spant@microsoft.com::power up okr and numbers",
            status="suggested",
        )
        conn = get_connection()
        try:
            # Slightly different source_id should fuzzy-match
            match = find_similar_source(
                conn,
                "chat::spant::power up latest okr and numbers",
                "chat",
            )
            self.assertIsNotNone(match)
            self.assertEqual(match["title"], "Power up OKR")
        finally:
            conn.close()

    def test_find_similar_source_no_match_different_person(self):
        from src.models import create_task, find_similar_source
        from src.db import get_connection
        create_task(
            title="Power up OKR",
            source_type="chat",
            source_id="chat::spant@microsoft.com::power up okr and numbers",
            status="suggested",
        )
        conn = get_connection()
        try:
            # Different person alias should NOT match
            match = find_similar_source(
                conn,
                "chat::johndoe@microsoft.com::power up okr and numbers",
                "chat",
            )
            self.assertIsNone(match)
        finally:
            conn.close()

    def test_find_similar_source_no_match_different_topic(self):
        from src.models import create_task, find_similar_source
        from src.db import get_connection
        create_task(
            title="Power up OKR",
            source_type="chat",
            source_id="chat::spant@microsoft.com::power up okr and numbers",
            status="suggested",
        )
        conn = get_connection()
        try:
            # Completely different topic should NOT match
            match = find_similar_source(
                conn,
                "chat::spant@microsoft.com::budget review quarterly finance",
                "chat",
            )
            self.assertIsNone(match)
        finally:
            conn.close()

    def test_create_task_exact_dedup(self):
        from src.models import create_task
        t1 = create_task(
            title="Task A",
            source_type="chat",
            source_id="chat::alice@example.com::project update",
            status="suggested",
        )
        t2 = create_task(
            title="Task A duplicate",
            source_type="chat",
            source_id="chat::alice@example.com::project update",
            status="suggested",
        )
        # Should return the same task
        self.assertEqual(t1["id"], t2["id"])

    def test_create_task_fuzzy_dedup(self):
        from src.models import create_task
        t1 = create_task(
            title="Power up OKR",
            source_type="chat",
            source_id="chat::spant@microsoft.com::power up okr and numbers",
            status="suggested",
        )
        t2 = create_task(
            title="Power up latest OKR",
            source_type="chat",
            source_id="chat::spant::power up latest okr and numbers",
            status="suggested",
        )
        # Fuzzy match should return original task
        self.assertEqual(t1["id"], t2["id"])

    def test_create_task_no_source_id_skips_dedup(self):
        from src.models import create_task
        t1 = create_task(title="Manual task A")
        t2 = create_task(title="Manual task B")
        # No source_id → no dedup, separate tasks created
        self.assertNotEqual(t1["id"], t2["id"])


if __name__ == "__main__":
    unittest.main()
