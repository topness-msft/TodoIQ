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


    # ── Staleness Guard ──

    def test_staleness_guard_downgrades_old_suggestion(self):
        from src.models import create_task
        from src.db import get_connection
        from datetime import datetime, timedelta

        conn = get_connection()
        today = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO sync_log (sync_type, synced_at, result_summary, tasks_created, tasks_updated) VALUES (?,?,?,?,?)",
            ("full_scan", today, "{}", 0, 0),
        )
        conn.commit()
        conn.close()

        old_date = (datetime.utcnow() - timedelta(days=20)).strftime("%Y-%m-%d")
        task = create_task(
            title="Old chat suggestion",
            status="suggested",
            source_type="chat",
            source_id="test::staleness::downgrades_old",
            source_date=old_date,
            priority=2,
        )
        self.assertEqual(task["priority"], 5)
        self.assertIn("Auto-downgraded", task["coaching_text"])

    def test_staleness_guard_keeps_recent_suggestion(self):
        from src.models import create_task
        from src.db import get_connection
        from datetime import datetime, timedelta

        conn = get_connection()
        sync_date = (datetime.utcnow() - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO sync_log (sync_type, synced_at, result_summary, tasks_created, tasks_updated) VALUES (?,?,?,?,?)",
            ("full_scan", sync_date, "{}", 0, 0),
        )
        conn.commit()
        conn.close()

        recent_date = (datetime.utcnow() - timedelta(days=2)).strftime("%Y-%m-%d")
        task = create_task(
            title="Recent meeting suggestion",
            status="suggested",
            source_type="meeting",
            source_id="test::staleness::keeps_recent",
            source_date=recent_date,
            priority=2,
        )
        self.assertEqual(task["priority"], 2)

    def test_staleness_guard_exempts_email(self):
        from src.models import create_task
        from src.db import get_connection
        from datetime import datetime, timedelta

        conn = get_connection()
        today = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO sync_log (sync_type, synced_at, result_summary, tasks_created, tasks_updated) VALUES (?,?,?,?,?)",
            ("full_scan", today, "{}", 0, 0),
        )
        conn.commit()
        conn.close()

        old_date = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")
        task = create_task(
            title="Old flagged email",
            status="suggested",
            source_type="email",
            source_id="test::staleness::exempts_email",
            source_date=old_date,
            priority=2,
        )
        self.assertEqual(task["priority"], 2)

    def test_staleness_guard_skips_active_tasks(self):
        from src.models import create_task
        from src.db import get_connection
        from datetime import datetime, timedelta

        conn = get_connection()
        today = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO sync_log (sync_type, synced_at, result_summary, tasks_created, tasks_updated) VALUES (?,?,?,?,?)",
            ("full_scan", today, "{}", 0, 0),
        )
        conn.commit()
        conn.close()

        old_date = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")
        task = create_task(
            title="Active old task",
            status="active",
            source_type="chat",
            source_id="test::staleness::skips_active",
            source_date=old_date,
            priority=2,
        )
        self.assertEqual(task["priority"], 2)

    def test_staleness_guard_fallback_14_days(self):
        from src.models import create_task
        from datetime import datetime, timedelta

        old_date = (datetime.utcnow() - timedelta(days=20)).strftime("%Y-%m-%d")
        task = create_task(
            title="No sync history suggestion",
            status="suggested",
            source_type="chat",
            source_id="test::staleness::fallback_14d",
            source_date=old_date,
            priority=3,
        )
        self.assertEqual(task["priority"], 5)
        self.assertIn("Auto-downgraded", task["coaching_text"])

    # ── Title-Based Dedup ──

    def test_title_dedup_catches_paraphrased_duplicates(self):
        """Real-world case: same person, same topic, different phrasing across chats."""
        from src.models import create_task
        people = '[{"name": "Ramakrishnan Raman", "email": "ramakrishnan.raman@microsoft.com"}]'
        t1 = create_task(
            title="Follow up with Ramakrishnan Raman on preferred question-collection process",
            source_type="chat",
            source_id="chat::ramakrishnan.raman@microsoft.com::question collection process",
            key_people=people,
            status="suggested",
        )
        t2 = create_task(
            title="Follow up with Ramakrishnan Raman on question intake method",
            source_type="chat",
            source_id="chat::ramakrishnan.raman@microsoft.com::question intake method chat",
            key_people=people,
            status="suggested",
        )
        self.assertEqual(t1["id"], t2["id"])

    def test_title_dedup_catches_webinar_repo_variants(self):
        """Real-world case: webinar trimming repo task surfaced 3 times."""
        from src.models import create_task
        people = '[{"name": "Vasavi Bhaviri Setty", "email": "vasavi.bhaviri@microsoft.com"}]'
        t1 = create_task(
            title="Try Vasavi's webinar-trimming GitHub repo and provide feedback",
            source_type="chat",
            source_id="chat::vasavi.bhaviri@microsoft.com::webinar trimming repo",
            key_people=people,
            status="suggested",
        )
        t2 = create_task(
            title="Test webinar trimming GitHub repo for Vasavi",
            source_type="meeting",
            source_id="meeting::vasavi.bhaviri@microsoft.com::webinar repo review",
            key_people=people,
            status="suggested",
        )
        self.assertEqual(t1["id"], t2["id"])

    def test_title_dedup_different_topics_same_person_no_merge(self):
        """Same person but genuinely different tasks should NOT dedup."""
        from src.models import create_task
        people = '[{"name": "Vasavi Bhaviri Setty", "email": "vasavi.bhaviri@microsoft.com"}]'
        t1 = create_task(
            title="Meet with Vasavi to identify webinar automation pilot areas",
            source_type="meeting",
            source_id="meeting::vasavi.bhaviri@microsoft.com::webinar automation pilot",
            key_people=people,
            status="suggested",
        )
        t2 = create_task(
            title="Confirm CAPE Scale EBC 1-slider with Vasavi",
            source_type="chat",
            source_id="chat::vasavi.bhaviri@microsoft.com::cape scale ebc slider",
            key_people=people,
            status="suggested",
        )
        self.assertNotEqual(t1["id"], t2["id"])

    def test_title_dedup_completed_task_not_matched(self):
        """Title dedup should NOT match against completed tasks."""
        from src.models import create_task, transition_task
        people = '[{"name": "Bill Spencer", "email": "bill.spencer@microsoft.com"}]'
        t1 = create_task(
            title="Follow up with Bill Spencer on speaker timing confirmation",
            source_type="chat",
            source_id="chat::bill.spencer@microsoft.com::speaker timing old",
            key_people=people,
            status="active",
        )
        transition_task(t1["id"], "completed")
        t2 = create_task(
            title="Follow up with Bill Spencer on allotted speaking time",
            source_type="chat",
            source_id="chat::bill.spencer@microsoft.com::speaking time new",
            key_people=people,
            status="suggested",
        )
        # Should be a new task since t1 is completed
        self.assertNotEqual(t1["id"], t2["id"])

    def test_title_dedup_no_people_no_match(self):
        """Without people info, title dedup should not fire."""
        from src.models import create_task
        t1 = create_task(
            title="Review the quarterly deck and finalize updates",
            status="suggested",
        )
        t2 = create_task(
            title="Review the quarterly deck and finalize changes",
            status="suggested",
        )
        self.assertNotEqual(t1["id"], t2["id"])

    def test_title_dedup_augments_context(self):
        """When title dedup matches, provenance should be saved as context."""
        from src.models import create_task, get_contexts
        people = '[{"name": "Mudit Agarwal", "email": "mudit.agarwal@microsoft.com"}]'
        t1 = create_task(
            title="Complete and submit Mudit's Connect feedback",
            source_type="chat",
            source_id="chat::mudit.agarwal@microsoft.com::connect feedback submit",
            key_people=people,
            source_snippet="Mudit asked for Connect feedback",
            status="suggested",
        )
        t2 = create_task(
            title="Complete Mudit Agarwal's Connect request",
            source_type="chat",
            source_id="chat::mudit.agarwal@microsoft.com::connect request completion",
            key_people=people,
            source_snippet="Mudit's Connect request needs completion",
            status="suggested",
        )
        self.assertEqual(t1["id"], t2["id"])
        contexts = get_contexts(t1["id"])
        dedup_contexts = [c for c in contexts if c["context_type"] == "dedup"]
        self.assertEqual(len(dedup_contexts), 1)
        self.assertIn("title similarity", dedup_contexts[0]["content"])

    def test_title_dedup_falls_back_to_source_id_person(self):
        """Title dedup should work via source_id person even without key_people."""
        from src.models import create_task
        t1 = create_task(
            title="Follow up with Bill Spencer on speaker timing confirmation",
            source_type="chat",
            source_id="chat::bill.spencer@microsoft.com::speaker timing old",
            key_people='[{"name": "Bill Spencer", "email": "bill.spencer@microsoft.com"}]',
            status="suggested",
        )
        # Second task has no key_people but same person in source_id
        t2 = create_task(
            title="Follow up with Bill Spencer on allotted speaking time",
            source_type="chat",
            source_id="chat::bill.spencer@microsoft.com::allotted time new",
            status="suggested",
        )
        self.assertEqual(t1["id"], t2["id"])


if __name__ == "__main__":
    unittest.main()
