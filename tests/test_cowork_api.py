"""Tests for the Cowork preview API.

The real `cowork` binary is never invoked: `cowork_runner.start_preview` takes a
`spawn` injection point, and these tests patch the handler's spawn hook.

Phase 1 is PREVIEW ONLY. The last class here asserts that structurally — no
route may exist that could write to M365.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tornado.testing  # noqa: E402

from src.app import make_app  # noqa: E402
from src.services import cowork_runner as cr  # noqa: E402


class FakeProc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self._out, self._err, self.returncode = stdout, stderr, returncode

    def communicate(self, timeout=None):
        return self._out, self._err

    def kill(self):
        self.returncode = -9

    def poll(self):
        return self.returncode


# A minimal payload in the real CLI's shape. The prose key is "text" — verified
# against tests/fixtures/spike-2076-stdout.json, not guessed.
GOOD_STDOUT = json.dumps(
    {
        "terminal_status": "ok",
        "conversation_id": "conv-abc",
        "tool_trace": [{"name": "m365_teams-GetMessages", "ok": True}],
        "text": (
            "Sarah asked for the deck on Tuesday and has not had a reply.\n\n"
            "## Step 2 - Draft nudge (not sent)\n\n"
            "> Hi Sarah - sorry for the delay, sending the deck today.\n\n"
            "Want me to send it?"
        ),
    }
)


class CoworkAPITestBase(tornado.testing.AsyncHTTPTestCase):
    def setUp(self):
        import src.db as db_module

        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        db_module.DB_PATH = self.tmp.name
        conn = db_module.get_connection()
        db_module.init_db(conn)
        conn.close()
        cr.reset_registry()
        self.original_auth_login = cr._auth_login_fn
        cr._auth_login_fn = lambda *args, **kwargs: type(
            "Login", (), {"returncode": 1}
        )()
        self.spawned = []
        self.log_tmp = tempfile.mkdtemp(prefix="cowork-api-")
        super().setUp()

    def tearDown(self):
        super().tearDown()
        cr._auth_login_fn = self.original_auth_login
        cr.reset_registry()
        os.unlink(self.tmp.name)

    def get_app(self):
        return make_app()

    # ── helpers ──

    def make_task(self, **extra):
        """Create a task. coaching_text is not accepted by POST /api/tasks, so
        it is applied with a follow-up PUT rather than widening that route."""
        coaching = extra.pop("coaching_text", None)
        body = {"title": "Send Sarah the deck", **extra}
        resp = self.fetch(
            "/api/tasks",
            method="POST",
            body=json.dumps(body),
            headers={"Content-Type": "application/json"},
        )
        tid = json.loads(resp.body)["task"]["id"]
        if coaching is not None:
            self.fetch(
                f"/api/tasks/{tid}",
                method="PUT",
                body=json.dumps({"coaching_text": coaching}),
                headers={"Content-Type": "application/json"},
            )
        return tid

    def make_action(self, task_id, state="ready", seen_at=None):
        from src.models import create_task_action
        from src.db import get_connection

        action = create_task_action(task_id, action_type="follow-up")
        conn = get_connection()
        conn.execute(
            "UPDATE task_actions SET state=?, seen_at=? WHERE id=?",
            (state, seen_at, action["id"]),
        )
        conn.commit()
        conn.close()
        return action["id"]

    def spawner(self, proc):
        def _spawn(argv, **kwargs):
            self.spawned.append({"argv": argv, "kwargs": kwargs})
            return proc

        return _spawn

    def start(self, task_id, proc=None, body=None):
        """POST a preview with a fake process, then wait for the worker."""
        from src.handlers import cowork as cowork_handler

        proc = proc if proc is not None else FakeProc(stdout=GOOD_STDOUT)
        cowork_handler.SPAWN = self.spawner(proc)
        cowork_handler.LOG_DIR_OVERRIDE = self.log_tmp
        try:
            resp = self.fetch(
                f"/api/tasks/{task_id}/cowork",
                method="POST",
                body=json.dumps(body or {}),
                headers={"Content-Type": "application/json"},
            )
        finally:
            pass
        cr.wait_for(cr.preview_label(task_id), timeout=10)
        return resp

    def get_preview(self, task_id):
        resp = self.fetch(f"/api/tasks/{task_id}/cowork")
        return resp, (json.loads(resp.body) if resp.body else {})


# ------------------------------------------------------------------ POST


class TestStartPreview(CoworkAPITestBase):
    def test_returns_202(self):
        tid = self.make_task()
        self.assertEqual(self.start(tid).code, 202)

    def test_unknown_task_is_404(self):
        resp = self.fetch(
            "/api/tasks/999999/cowork",
            method="POST",
            body="{}",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.code, 404)

    def test_creates_previewing_row(self):
        tid = self.make_task()
        self.start(tid, proc=FakeProc(stdout=GOOD_STDOUT))
        _, data = self.get_preview(tid)
        self.assertIn(data["action"]["state"], ("previewing", "ready"))

    def test_persists_cached_island_url_at_action_creation(self):
        original = cr._ISLAND_PROBE_FN
        try:
            cr._ISLAND_PROBE_FN = lambda: "https://ia302.example"
            cr.resolve_cowork_island()
            tid = self.make_task()
            self.start(tid)
            _, data = self.get_preview(tid)
            self.assertEqual(
                data["action"]["island_url"], "https://ia302.example"
            )
        finally:
            cr._ISLAND_PROBE_FN = original
            cr.reset_registry()

    def test_snapshots_intent_and_notes(self):
        tid = self.make_task(
            coaching_text="Nudge Sarah about the deck",
            user_notes="Keep it short; she is travelling",
        )
        self.start(tid)
        _, data = self.get_preview(tid)
        self.assertEqual(data["action"]["intent"], "Nudge Sarah about the deck")
        self.assertEqual(
            data["action"]["notes_snapshot"], "Keep it short; she is travelling"
        )

    def test_composed_prompt_persisted(self):
        tid = self.make_task(coaching_text="Nudge Sarah")
        self.start(tid)
        _, data = self.get_preview(tid)
        self.assertIn("Nudge Sarah", data["action"]["composed_prompt"])

    def test_destination_parsed_from_source_url(self):
        tid = self.make_task(
            source_url=(
                "https://teams.microsoft.com/l/message/"
                "19:aaaa_bbbb@unq.gbl.spaces/1772052810655"
            )
        )
        self.start(tid)
        _, data = self.get_preview(tid)
        self.assertEqual(data["action"]["destination_kind"], "one_to_one")

    def test_linked_teams_thread_is_destination_not_cowork_conversation(self):
        tid = self.make_task(
            source_type="chat",
            source_url=(
                "https://teams.microsoft.com/l/message/"
                "19:aaaa_bbbb@unq.gbl.spaces/1772052810655"
            ),
            key_people=json.dumps(
                [{"name": "Sarah Goodwin", "email": "sarah@microsoft.com"}]
            ),
        )
        response = self.start(tid, proc=FakeProc(stdout=GOOD_STDOUT))
        action = json.loads(response.body)["action"]

        self.assertIsNone(action["conversation_id"])
        self.assertEqual(
            action["destination_ref"], "19:aaaa_bbbb@unq.gbl.spaces"
        )
        self.assertEqual(action["delivery_channel"], "teams")
        self.assertIn("Sarah Goodwin", action["destination_display"])

    def test_manual_unique_person_prefills_without_choosing_channel(self):
        tid = self.make_task(
            source_type="manual",
            key_people=json.dumps(
                [{"name": "Sarah Goodwin", "email": "sarah@microsoft.com"}]
            ),
        )
        response = self.start(tid)
        action = json.loads(response.body)["action"]

        self.assertIsNone(action["delivery_channel"])
        self.assertEqual(action["destination_ref"], "sarah@microsoft.com")
        self.assertEqual(action["destination_display"], "Sarah Goodwin")
        self.assertEqual(action["destination_source"], "auto_key_people")

    def test_broadcast_destination_recorded(self):
        tid = self.make_task(
            source_url=(
                "https://teams.microsoft.com/l/message/"
                "19:ccccc@thread.v2/1772052810655"
            )
        )
        self.start(tid)
        _, data = self.get_preview(tid)
        self.assertEqual(data["action"]["destination_kind"], "group")

    def test_redirect_text_stored_on_new_row(self):
        tid = self.make_task()
        self.start(tid)
        self.start(tid, body={"redirect_text": "no, look for times next week"})
        _, data = self.get_preview(tid)
        self.assertEqual(
            data["action"]["redirect_text"], "no, look for times next week"
        )

    def test_redo_creates_a_second_row_not_an_update(self):
        tid = self.make_task()
        self.start(tid)
        self.start(tid, body={"redirect_text": "try again"})
        resp = self.fetch(f"/api/tasks/{tid}/cowork?history=1")
        self.assertEqual(len(json.loads(resp.body)["actions"]), 2)

    def test_conflict_while_running(self):
        """409 must be gated on the in-memory registry, not the DB row."""
        tid = self.make_task()
        from src.handlers import cowork as cowork_handler

        never_returns = FakeProc(stdout=GOOD_STDOUT)
        cowork_handler.SPAWN = self.spawner(never_returns)
        cowork_handler.LOG_DIR_OVERRIDE = self.log_tmp

        # Seed a run and keep it "in flight" by not draining it.
        cr.reset_registry()
        cr._runs[cr.preview_label(tid)] = {
            "proc": None,
            "thread": None,
            "result": None,
        }
        resp = self.fetch(
            f"/api/tasks/{tid}/cowork",
            method="POST",
            body="{}",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.code, 409)


# ------------------------------------------------------------------- GET


class TestGetPreview(CoworkAPITestBase):
    def test_404_when_never_run(self):
        tid = self.make_task()
        resp, _ = self.get_preview(tid)
        self.assertEqual(resp.code, 404)

    def test_finalises_to_ready(self):
        tid = self.make_task()
        self.start(tid)
        _, data = self.get_preview(tid)
        self.assertEqual(data["action"]["state"], "ready")

    def test_draft_extracted(self):
        tid = self.make_task()
        self.start(tid)
        _, data = self.get_preview(tid)
        self.assertIn("sending the deck today", data["action"]["draft"])

    def test_finding_extracted(self):
        tid = self.make_task()
        self.start(tid)
        _, data = self.get_preview(tid)
        self.assertIn("Sarah asked", data["action"]["finding"])

    def test_sse_events_never_persisted(self):
        """82 entries and the bulk of a 21KB payload."""
        noisy = json.dumps(
            {
                "terminal_status": "ok",
                "text": "> draft here",
                "sse_events": [{"x": i} for i in range(82)],
            }
        )
        tid = self.make_task()
        self.start(tid, proc=FakeProc(stdout=noisy))
        _, data = self.get_preview(tid)
        self.assertNotIn("sse_events", json.dumps(data["action"]))

    def test_latest_row_wins_by_id_not_timestamp(self):
        """TEXT timestamps are second-precision and tie — the live get_last_sync
        bug. Two redos inside one second must still order correctly."""
        tid = self.make_task()
        self.start(tid)
        self.start(tid, body={"redirect_text": "second"})
        self.start(tid, body={"redirect_text": "third"})
        _, data = self.get_preview(tid)
        self.assertEqual(data["action"]["redirect_text"], "third")

    def test_failure_recorded_as_failed(self):
        tid = self.make_task()
        self.start(tid, proc=FakeProc(stdout="", stderr="boom", returncode=1))
        _, data = self.get_preview(tid)
        self.assertEqual(data["action"]["state"], "failed")
        self.assertTrue(data["action"]["error"])

    def test_auth_failure_surfaces_actionable_error(self):
        proc = FakeProc(
            stdout="", stderr="Not authenticated. Run: cowork auth login", returncode=1
        )
        tid = self.make_task()
        self.start(tid, proc=proc)
        _, data = self.get_preview(tid)
        self.assertEqual(data["action"]["state"], "failed")
        self.assertIn("cowork auth login", data["action"]["error"])

    def test_finalise_auto_confirms_safe_teams_one_to_one(self):
        tid = self.make_task(
            source_type="chat",
            source_url=(
                "https://teams.microsoft.com/l/message/"
                "19:aaaa_bbbb@unq.gbl.spaces/1772052810655"
            ),
            key_people=json.dumps(
                [{"name": "Sarah Goodwin", "email": "sarah@microsoft.com"}]
            ),
        )
        self.start(tid)
        _, data = self.get_preview(tid)

        self.assertEqual(data["action"]["state"], "ready")
        self.assertIsNotNone(data["action"]["destination_confirmed_at"])
        self.assertEqual(data["action"]["destination_source"], "auto_source_url")

    def test_finalise_does_not_auto_confirm_broadcast(self):
        tid = self.make_task(
            source_type="chat",
            source_url=(
                "https://teams.microsoft.com/l/message/"
                "19:group@thread.v2/1772052810655"
            ),
        )
        self.start(tid)
        _, data = self.get_preview(tid)

        self.assertEqual(data["action"]["state"], "ready")
        self.assertIsNone(data["action"]["destination_confirmed_at"])
        self.assertTrue(data["action"]["is_broadcast"])


# ------------------------------------------------------------- mark seen


class TestMarkSeen(CoworkAPITestBase):
    def test_mark_seen_sets_timestamp_on_already_ready_action(self):
        tid = self.make_task()
        self.make_action(tid, state="ready")

        response = self.fetch(f"/api/tasks/{tid}/cowork?mark_seen=1")
        action = json.loads(response.body)["action"]

        self.assertIsNotNone(action["seen_at"])

    def test_mark_seen_does_not_mark_action_that_finalises_in_same_get(self):
        tid = self.make_task()
        self.start(tid)

        response = self.fetch(f"/api/tasks/{tid}/cowork?mark_seen=1")
        action = json.loads(response.body)["action"]

        self.assertEqual(action["state"], "ready")
        self.assertIsNone(action["seen_at"])

    def test_plain_get_does_not_mark_ready_action(self):
        tid = self.make_task()
        self.make_action(tid, state="ready")

        _, data = self.get_preview(tid)

        self.assertIsNone(data["action"]["seen_at"])

    def test_client_cannot_set_seen_timestamp_through_put(self):
        tid = self.make_task()
        self.make_action(tid, state="ready")
        response = self.fetch(
            f"/api/tasks/{tid}/cowork",
            method="PUT",
            body=json.dumps({"seen_at": "1999-01-01T00:00:00Z"}),
            headers={"Content-Type": "application/json"},
        )

        self.assertEqual(response.code, 400)


# ------------------------------------------------------ confirm destination


class TestConfirmDestination(CoworkAPITestBase):
    def _confirm(self, tid, body):
        return self.fetch(
            f"/api/tasks/{tid}/cowork/destination",
            method="POST",
            body=json.dumps(body),
            headers={"Content-Type": "application/json"},
        )

    def test_user_picker_confirms_exact_validated_bundle(self):
        tid = self.make_task()
        self.make_action(tid, state="ready")
        response = self._confirm(
            tid,
            {
                "delivery_channel": "email",
                "destination_ref": "sarah@microsoft.com",
                "destination_display": "Sarah Goodwin",
                "source": "auto_source_url",
            },
        )
        action = json.loads(response.body)["action"]

        self.assertEqual(response.code, 200)
        self.assertEqual(action["delivery_channel"], "email")
        self.assertEqual(action["destination_ref"], "sarah@microsoft.com")
        self.assertEqual(action["destination_display"], "Sarah Goodwin")
        self.assertEqual(action["destination_source"], "user_picker")
        self.assertIsNotNone(action["destination_confirmed_at"])
        self.assertIn("is_broadcast", action)

    def test_rejects_unknown_channel_and_blank_destination(self):
        tid = self.make_task()
        self.make_action(tid, state="ready")
        self.assertEqual(
            self._confirm(
                tid,
                {
                    "delivery_channel": "sms",
                    "destination_ref": "x",
                    "destination_display": "X",
                },
            ).code,
            400,
        )
        self.assertEqual(
            self._confirm(
                tid,
                {
                    "delivery_channel": "teams",
                    "destination_ref": " ",
                    "destination_display": " ",
                },
            ).code,
            400,
        )

    def test_previewing_action_cannot_be_confirmed(self):
        tid = self.make_task()
        self.make_action(tid, state="previewing")
        response = self._confirm(
            tid,
            {
                "delivery_channel": "teams",
                "destination_ref": "sarah@microsoft.com",
                "destination_display": "Sarah Goodwin",
            },
        )
        self.assertEqual(response.code, 409)

    def test_history_includes_broadcast_enrichment(self):
        tid = self.make_task()
        action_id = self.make_action(tid, state="ready")
        import src.db as db_module

        conn = db_module.get_connection()
        conn.execute(
            "UPDATE task_actions SET destination_kind='group' WHERE id=?",
            (action_id,),
        )
        conn.commit()
        conn.close()

        response = self.fetch(f"/api/tasks/{tid}/cowork?history=1")
        action = json.loads(response.body)["actions"][0]
        self.assertTrue(action["is_broadcast"])


# ------------------------------------------------------------------- PUT


class TestEditDraft(CoworkAPITestBase):
    def _put(self, tid, body):
        return self.fetch(
            f"/api/tasks/{tid}/cowork",
            method="PUT",
            body=json.dumps(body),
            headers={"Content-Type": "application/json"},
        )

    def test_saves_draft_edited(self):
        tid = self.make_task()
        self.start(tid)
        self.assertEqual(self._put(tid, {"draft_edited": "My own words"}).code, 200)
        _, data = self.get_preview(tid)
        self.assertEqual(data["action"]["draft_edited"], "My own words")

    def test_original_draft_preserved(self):
        tid = self.make_task()
        self.start(tid)
        self._put(tid, {"draft_edited": "My own words"})
        _, data = self.get_preview(tid)
        self.assertIn("sending the deck today", data["action"]["draft"])

    def test_404_when_no_action(self):
        tid = self.make_task()
        self.assertEqual(self._put(tid, {"draft_edited": "x"}).code, 404)

    def test_rejects_unknown_fields(self):
        """Only draft_edited is editable. state/draft/tool_trace are not."""
        tid = self.make_task()
        self.start(tid)
        self._put(
            tid,
            {
                "state": "ready",
                "draft": "spoofed",
                "island_url": "https://evil.example",
            },
        )
        _, data = self.get_preview(tid)
        self.assertNotEqual(data["action"]["draft"], "spoofed")
        self.assertNotEqual(data["action"]["island_url"], "https://evil.example")


# ------------------------------------------------------- Phase 1 safety


class TestNoExecutePath(CoworkAPITestBase):
    def test_no_execute_route(self):
        tid = self.make_task()
        resp = self.fetch(
            f"/api/tasks/{tid}/cowork/execute",
            method="POST",
            body="{}",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.code, 404)

    def test_execute_states_rejected_by_schema(self):
        import src.db as db_module

        conn = db_module.get_connection()
        try:
            tid = self.make_task()
            with self.assertRaises(Exception):
                conn.execute(
                    "INSERT INTO task_actions (task_id, state) VALUES (?, 'executed')",
                    (tid,),
                )
                conn.commit()
        finally:
            conn.close()

    def test_denylist_passed_on_every_run(self):
        tid = self.make_task()
        self.start(tid)
        self.assertIn("--tool-callback-config", self.spawned[0]["argv"])

    def test_deny_tools_flag_never_used(self):
        tid = self.make_task()
        self.start(tid)
        self.assertNotIn("--deny-tools", self.spawned[0]["argv"])


# ------------------------------------------------------------------ refs


class TestRefs(unittest.TestCase):
    """--ref values, built from the REAL key_people shape.

    A live sweep found key_people is JSON on 1942 of 1958 tasks, not a comma
    separated list. A naive split produced refs like
    `person:[{"name": "Sarah Goodwin"` on essentially every task.
    """

    def _refs(self, value):
        from src.handlers.cowork import _refs

        return _refs({"key_people": value})

    def test_json_array_uses_email(self):
        value = json.dumps(
            [{"name": "Sarah Goodwin", "email": "Sarah.Goodwin@microsoft.com"}]
        )
        self.assertEqual(self._refs(value), ["person:Sarah.Goodwin@microsoft.com"])

    def test_json_array_multiple_people(self):
        value = json.dumps(
            [
                {"name": "Sarah Goodwin", "email": "sarah.goodwin@microsoft.com"},
                {"name": "Sameer Bhangar", "email": "sameer@microsoft.com"},
            ]
        )
        self.assertEqual(
            self._refs(value),
            ["person:sarah.goodwin@microsoft.com", "person:sameer@microsoft.com"],
        )

    def test_extra_keys_ignored(self):
        value = json.dumps(
            [
                {
                    "name": "Sarah Goodwin",
                    "email": "sarah@microsoft.com",
                    "role": "Principal PM Manager, CAT",
                }
            ]
        )
        self.assertEqual(self._refs(value), ["person:sarah@microsoft.com"])

    def test_falls_back_to_name_without_email(self):
        self.assertEqual(
            self._refs(json.dumps([{"name": "Suzy Agi"}])), ["person:Suzy Agi"]
        )

    def test_plain_text_still_supported(self):
        self.assertEqual(
            self._refs("Sarah Goodwin, Sameer Bhangar"),
            ["person:Sarah Goodwin", "person:Sameer Bhangar"],
        )

    def test_empty_is_no_refs(self):
        self.assertEqual(self._refs(""), [])
        self.assertEqual(self._refs(None), [])

    def test_no_ref_ever_contains_json_punctuation(self):
        value = json.dumps([{"name": "A B", "email": "a@b.com"}])
        for ref in self._refs(value):
            for ch in '[]{}"':
                self.assertNotIn(ch, ref)

    def test_malformed_json_does_not_crash(self):
        self.assertEqual(self._refs('[{"name": '), [])

    def test_duplicates_collapsed(self):
        value = json.dumps(
            [{"email": "a@b.com"}, {"email": "a@b.com"}]
        )
        self.assertEqual(self._refs(value), ["person:a@b.com"])


if __name__ == "__main__":
    unittest.main()
