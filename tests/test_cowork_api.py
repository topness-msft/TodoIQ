"""Tests for the Cowork preview API.

The real `cowork` binary is never invoked: `cowork_runner.start_preview` takes a
`spawn` injection point, and these tests patch the handler's spawn hook.

Phase 1 is PREVIEW ONLY. The last class here asserts that structurally — no
route may exist that could write to M365.
"""

import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tornado.testing  # noqa: E402

from src.app import make_app  # noqa: E402
from src.services import cowork_runner as cr  # noqa: E402


class FakeProc:
    """Popen stand-in with real readable pipes.

    `_collect` drains stdout/stderr line by line so progress can surface while a
    run is live, so a fake must expose pipes or it takes a different path from
    production.
    """

    def __init__(self, stdout="", stderr="", returncode=0):
        self._out, self._err, self.returncode = stdout, stderr, returncode
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)

    def communicate(self, timeout=None):
        return self._out, self._err

    def wait(self, timeout=None):
        return self.returncode

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
        from src.handlers import cowork as cowork_handler

        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        db_module.DB_PATH = self.tmp.name
        conn = db_module.get_connection()
        db_module.init_db(conn)
        conn.close()
        cr.reset_registry()
        # A real cost snapshot is a ~1s network call and _collect runs in
        # hundreds of tests; unmocked it took the suite from 35s to 313s.
        self._base_cost_fn = cr._cost_snapshot_fn
        cr._cost_snapshot_fn = lambda: None
        self._base_handoff_fn = cowork_handler.HANDOFF_FN
        self._base_question_fn = cowork_handler.BLOCKED_QUESTION_FN
        # Same reasoning as the cost seam: GET /v1/tasks is a network call and
        # the card GET runs in many tests. Default to "no handoff info", which
        # is exactly the additive-degrades-to-today path.
        cowork_handler.HANDOFF_FN = lambda _cid: None
        cowork_handler.BLOCKED_QUESTION_FN = lambda _cid: None
        cr.reset_handoff_cache()
        # Tests must never read the user's real data/settings.json — with the
        # API transport flag on for dogfood, these tests made real network calls.
        self._base_api_flag = cr.api_transport_enabled
        cr.api_transport_enabled = lambda: False
        # tenant_barrier_precheck() calls the runtime to read the signed-in
        # tenant. start_preview runs it on every POST, so unstubbed it makes a
        # real network call per test — which fails Tornado's 5s fetch timeout
        # whenever the service is slow. Same isolation gap as the cost and
        # handoff seams. The precheck is advisory, so "ok" is the honest stub.
        self._base_precheck = cr.tenant_barrier_precheck
        cr.tenant_barrier_precheck = lambda **kw: {"status": "ok", "reason": ""}
        self.original_auth_login = cr._auth_login_fn
        cr._auth_login_fn = lambda *args, **kwargs: type(
            "Login", (), {"returncode": 1}
        )()
        self.spawned = []
        self.log_tmp = tempfile.mkdtemp(prefix="cowork-api-")
        super().setUp()

    def tearDown(self):
        from src.handlers import cowork as cowork_handler

        cr._cost_snapshot_fn = self._base_cost_fn
        cowork_handler.HANDOFF_FN = self._base_handoff_fn
        cowork_handler.BLOCKED_QUESTION_FN = self._base_question_fn
        cr.api_transport_enabled = self._base_api_flag
        cr.tenant_barrier_precheck = self._base_precheck
        cr.reset_handoff_cache()
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

    def test_ready_row_has_stable_completion_timestamp(self):
        tid = self.make_task()
        self.start(tid, proc=FakeProc(stdout=GOOD_STDOUT))
        _, data = self.get_preview(tid)
        completed_at = data["action"]["completed_at"]
        self.assertIsNotNone(completed_at)
        self.fetch(f"/api/tasks/{tid}/cowork?mark_seen=1")
        _, after = self.get_preview(tid)
        self.assertEqual(after["action"]["completed_at"], completed_at)

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

    def test_interaction_mode_defaults_to_interaction(self):
        tid = self.make_task()
        response = self.start(tid)
        action = json.loads(response.body)["action"]
        self.assertEqual(action["interaction_mode"], "interaction")
        self.assertNotIn("[INTERACTION]", action["composed_prompt"])

    def test_no_interaction_mode_is_clamped_to_interaction(self):
        tid = self.make_task()
        response = self.start(tid, body={"interaction_mode": "no_interaction"})
        action = json.loads(response.body)["action"]
        self.assertEqual(action["interaction_mode"], "interaction")
        self.assertNotIn("[INTERACTION]", action["composed_prompt"])

    def test_unknown_interaction_mode_is_rejected(self):
        tid = self.make_task()
        response = self.fetch(
            f"/api/tasks/{tid}/cowork",
            method="POST",
            body=json.dumps({"interaction_mode": "surprise_me"}),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(response.code, 400)

    def test_non_string_interaction_mode_is_rejected(self):
        tid = self.make_task()
        response = self.fetch(
            f"/api/tasks/{tid}/cowork",
            method="POST",
            body=json.dumps({"interaction_mode": []}),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(response.code, 400)

    def test_new_run_does_not_inherit_previous_no_interaction_mode(self):
        tid = self.make_task()
        self.start(tid, body={"interaction_mode": "no_interaction"})
        response = self.start(tid, body={"redirect_text": "make it shorter"})
        action = json.loads(response.body)["action"]
        self.assertEqual(action["interaction_mode"], "interaction")

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

    def test_respond_email_action_selects_email_without_source_metadata(self):
        tid = self.make_task(
            title="Thank Phil for attending",
            description="Draft and send a concise thank-you email.",
            action_type="respond-email",
            source_type="manual",
            key_people=json.dumps([{
                "name": "Phil Topness",
                "email": "phil@topness.com",
                "role": "Principal Consultant",
            }]),
        )

        action = json.loads(self.start(tid).body)["action"]

        self.assertEqual(action["delivery_channel"], "email")
        self.assertEqual(action["destination_ref"], "phil@topness.com")
        voice = action["composed_prompt"].split("[VOICE]", 1)[1]
        self.assertIn("work-email-voice", voice)
        self.assertNotIn("work-teams-voice", voice)

    def test_email_redo_does_not_carry_forward_an_invalid_legacy_recipient(self):
        invalid_refs = (
            "alice@example.com bob@example.com",
            "alice@example.com,bob@example.com",
            "alice@example.com;bob@example.com",
            "19:meeting_Nm@thread.v2",
            '["alice@example.com"]',
            ".alice@example.com",
            "alice@example..com",
        )
        for invalid_ref in invalid_refs:
            with self.subTest(destination_ref=invalid_ref):
                tid = self.make_task(
                    action_type="respond-email",
                    source_type="manual",
                    key_people=json.dumps([{
                        "name": "Phil Topness",
                        "email": "phil@topness.com",
                    }]),
                )
                self.start(tid)
                from src.db import get_connection

                conn = get_connection()
                conn.execute(
                    """
                    UPDATE task_actions
                    SET delivery_channel='teams',
                        destination_ref=?,
                        destination_display='Legacy recipient',
                        destination_source='user_picker'
                    WHERE task_id=?
                    """,
                    (invalid_ref, tid),
                )
                conn.commit()
                conn.close()

                response = self.start(tid, body={"redirect_text": "try again"})
                action = json.loads(response.body)["action"]

                self.assertEqual(action["delivery_channel"], "email")
                self.assertEqual(action["destination_ref"], "phil@topness.com")
                self.assertEqual(
                    action["destination_source"], "auto_key_people"
                )

    def test_respond_email_does_not_reuse_a_teams_conversation(self):
        tid = self.make_task(
            title="Email Phil after the Teams discussion",
            description="Send Phil the follow-up by email.",
            action_type="respond-email",
            source_type="chat",
            source_url=(
                "https://teams.microsoft.com/l/message/"
                "19:teams-thread@thread.v2/1234567890"
            ),
            key_people=json.dumps([{
                "name": "Phil Topness",
                "email": "phil@topness.com",
            }]),
        )

        action = json.loads(self.start(tid).body)["action"]

        self.assertEqual(action["delivery_channel"], "email")
        self.assertEqual(action["destination_ref"], "phil@topness.com")
        self.assertNotIn("19:teams-thread", action["destination_ref"])

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

    def test_schedule_meeting_binds_selected_person_to_calendar(self):
        tid = self.make_task(
            action_type="schedule-meeting",
            source_type="manual",
            key_people=json.dumps(
                [{
                    "name": "Rima Reyes",
                    "email": "rima.reyes@microsoft.com",
                    "alternatives": [{
                        "name": "Rima Gooden",
                        "email": "rimagooden@microsoft.com",
                    }],
                }]
            ),
        )
        action = json.loads(self.start(tid).body)["action"]

        self.assertIsNone(action["delivery_channel"])
        self.assertEqual(action["destination_ref"], "rima.reyes@microsoft.com")
        self.assertEqual(action["destination_display"], "Rima Reyes")
        self.assertNotIn("work-teams-voice", action["composed_prompt"])
        self.assertNotIn("Rima Gooden", action["composed_prompt"])

    def test_schedule_meeting_binds_all_selected_people_to_confirmation(self):
        tid = self.make_task(
            action_type="schedule-meeting",
            source_type="manual",
            key_people=json.dumps([
                {"name": "Kanika Ramji", "email": "kanika@microsoft.com"},
                {"name": "Rima Reyes", "email": "rima@microsoft.com"},
                {"name": "Henry James", "email": "henry@microsoft.com"},
            ]),
        )

        action = json.loads(self.start(tid).body)["action"]

        self.assertEqual(
            action["destination_ref"],
            '["kanika@microsoft.com","rima@microsoft.com","henry@microsoft.com"]',
        )
        self.assertEqual(
            action["destination_display"], "Kanika Ramji, Rima Reyes, Henry James"
        )
        self.assertIn("Kanika Ramji", action["composed_prompt"])
        self.assertIn("Rima Reyes", action["composed_prompt"])
        self.assertIn("Henry James", action["composed_prompt"])

    def test_schedule_meeting_rejects_name_only_attendee(self):
        tid = self.make_task(
            action_type="schedule-meeting",
            source_type="manual",
            key_people=json.dumps([
                {"name": "Rima Reyes", "email": "rima@microsoft.com"},
                {"name": "Henry Jammes", "alternatives": []},
            ]),
        )

        response = self.start(tid)

        self.assertEqual(response.code, 400)
        self.assertEqual(
            json.loads(response.body)["error"],
            "Resolve the identity for Henry Jammes before scheduling.",
        )

    def test_schedule_meeting_rejects_empty_attendee_list(self):
        tid = self.make_task(
            action_type="schedule-meeting",
            source_type="manual",
            key_people="[]",
        )

        response = self.start(tid)

        self.assertEqual(response.code, 400)
        self.assertEqual(
            json.loads(response.body)["error"],
            "Add and confirm at least one attendee before scheduling.",
        )

    def test_schedule_meeting_rejects_unconfirmed_directory_match(self):
        tid = self.make_task(
            action_type="schedule-meeting",
            key_people=json.dumps([{
                "name": "Henry James",
                "email": "henry@microsoft.com",
                "unresolved": True,
                "alternatives": [],
            }]),
        )

        response = self.start(tid)

        self.assertEqual(response.code, 400)
        self.assertEqual(
            json.loads(response.body)["error"],
            "Resolve the identity for Henry James before scheduling.",
        )

    def test_schedule_meeting_rejects_uncertain_group_attendance(self):
        tid = self.make_task(
            action_type="schedule-meeting",
            key_people=json.dumps([{
                "name": "Exact Chat Member",
                "email": "member@microsoft.com",
                "aad_object_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "attendance_uncertain": True,
            }]),
        )

        response = self.start(tid)

        self.assertEqual(response.code, 400)
        self.assertEqual(
            json.loads(response.body)["error"],
            "Confirm whether Exact Chat Member should attend before scheduling.",
        )

    def test_schedule_meeting_rejects_plain_text_attendees(self):
        tid = self.make_task(
            action_type="schedule-meeting",
            source_type="manual",
            key_people="Rima Reyes, Henry James",
        )

        response = self.start(tid)

        self.assertEqual(response.code, 400)
        self.assertEqual(
            json.loads(response.body)["error"],
            "Resolve the identities for Rima Reyes, Henry James before scheduling.",
        )

    def test_schedule_meeting_rejects_duplicate_normalized_attendee_emails(self):
        tid = self.make_task(
            action_type="schedule-meeting",
            key_people=json.dumps([
                {"name": "Rima Reyes", "email": "RIMA@microsoft.com"},
                {"name": "Rima Reyes duplicate", "email": "rima@microsoft.com"},
            ]),
        )

        response = self.start(tid)

        self.assertEqual(response.code, 400)
        self.assertEqual(
            json.loads(response.body)["error"],
            "Resolve the identity for Rima Reyes duplicate before scheduling.",
        )

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

    def test_prompt_voice_follows_the_bound_channel(self):
        tid = self.make_task(
            source_type="email",
            key_people=json.dumps(
                [{"name": "Sarah Goodwin", "email": "sarah@microsoft.com"}]
            ),
        )
        self.start(tid)
        _, data = self.get_preview(tid)
        prompt = data["action"]["composed_prompt"]
        self.assertIn("[VOICE]", prompt)
        self.assertIn("work-email-voice", prompt.split("[VOICE]")[1])

    def test_redo_keeps_a_user_confirmed_destination(self):
        """A picker choice is explicit intent; re-deriving it would discard it."""
        tid = self.make_task(
            source_type="chat",
            key_people=json.dumps(
                [{"name": "Sarah Goodwin", "email": "sarah@microsoft.com"}]
            ),
        )
        self.start(tid)
        # Confirming requires a ready row, and finalisation happens on GET.
        self.get_preview(tid)
        confirm = self.fetch(
            f"/api/tasks/{tid}/cowork/destination",
            method="POST",
            body=json.dumps(
                {
                    "delivery_channel": "email",
                    "destination_ref": "sarah@microsoft.com",
                    "destination_display": "Sarah Goodwin",
                }
            ),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(confirm.code, 200)

        response = self.start(tid, body={"redirect_text": "try again"})
        action = json.loads(response.body)["action"]

        self.assertEqual(action["delivery_channel"], "email")
        self.assertEqual(action["destination_ref"], "sarah@microsoft.com")
        self.assertEqual(action["destination_source"], "user_picker")
        self.assertIn("work-email-voice", action["composed_prompt"].split("[VOICE]")[1])

    def test_email_redo_migrates_legacy_teams_picker_to_email_voice(self):
        tid = self.make_task(
            action_type="respond-email",
            source_type="manual",
            key_people=json.dumps([{
                "name": "Phil Topness",
                "email": "phil@topness.com",
                "unresolved": True,
            }]),
        )
        self.start(tid)
        from src.db import get_connection

        conn = get_connection()
        conn.execute(
            """
            UPDATE task_actions
            SET delivery_channel='teams',
                destination_ref='phil@topness.com',
                destination_display='Phil Topness',
                destination_source='user_picker'
            WHERE task_id=?
            """,
            (tid,),
        )
        conn.commit()
        conn.close()

        response = self.start(tid, body={"redirect_text": "try again"})
        action = json.loads(response.body)["action"]

        self.assertEqual(action["delivery_channel"], "email")
        self.assertEqual(action["destination_ref"], "phil@topness.com")
        self.assertEqual(action["destination_source"], "user_picker")
        voice = action["composed_prompt"].split("[VOICE]", 1)[1]
        self.assertIn("work-email-voice", voice)
        self.assertNotIn("work-teams-voice", voice)

    def test_redo_rederives_an_unconfirmed_destination(self):
        tid = self.make_task(
            source_type="chat",
            key_people=json.dumps(
                [{"name": "Sarah Goodwin", "email": "sarah@microsoft.com"}]
            ),
        )
        self.start(tid)
        response = self.start(tid, body={"redirect_text": "try again"})
        action = json.loads(response.body)["action"]

        self.assertEqual(action["delivery_channel"], "teams")
        self.assertEqual(action["destination_source"], "auto_key_people")

    def test_schedule_redo_rebinds_current_attendees(self):
        from src.db import get_connection

        tid = self.make_task(
            action_type="schedule-meeting",
            key_people=json.dumps([
                {"name": "Rima Reyes", "email": "rima@microsoft.com"},
                {"name": "Henry James", "email": "henry@microsoft.com"},
            ]),
        )
        self.start(tid)
        self.get_preview(tid)
        conn = get_connection()
        conn.execute(
            "UPDATE task_actions SET state='ready',error=NULL WHERE task_id=?",
            (tid,),
        )
        conn.commit()
        conn.close()
        confirm = self.fetch(
            f"/api/tasks/{tid}/cowork/destination",
            method="POST",
            body=json.dumps({
                "destination_ref": (
                    '["rima@microsoft.com","henry@microsoft.com"]'
                ),
                "destination_display": "Rima Reyes, Henry James",
            }),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(confirm.code, 200)
        conn = get_connection()
        conn.execute(
            "UPDATE tasks SET key_people=? WHERE id=?",
            (
                json.dumps([
                    {"name": "Rima Reyes", "email": "rima@microsoft.com"},
                    {"name": "Kanika Ramji", "email": "kanika@microsoft.com"},
                ]),
                tid,
            ),
        )
        conn.commit()
        conn.close()

        action = json.loads(
            self.start(tid, body={"redirect_text": "start over"}).body
        )["action"]

        self.assertEqual(
            action["destination_ref"],
            '["rima@microsoft.com","kanika@microsoft.com"]',
        )
        self.assertEqual(
            action["destination_display"], "Rima Reyes, Kanika Ramji"
        )
        self.assertEqual(action["destination_source"], "auto_key_people")

    def test_converting_to_schedule_does_not_carry_message_destination(self):
        from src.db import get_connection

        tid = self.make_task(
            action_type="follow-up",
            source_type="manual",
            key_people=json.dumps([
                {"name": "Sarah Goodwin", "email": "sarah@microsoft.com"},
            ]),
        )
        self.start(tid)
        self.get_preview(tid)
        confirm = self.fetch(
            f"/api/tasks/{tid}/cowork/destination",
            method="POST",
            body=json.dumps({
                "delivery_channel": "email",
                "destination_ref": "sarah@microsoft.com",
                "destination_display": "Sarah Goodwin",
            }),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(confirm.code, 200)
        conn = get_connection()
        conn.execute(
            "UPDATE tasks SET action_type=?, key_people=? WHERE id=?",
            (
                "schedule-meeting",
                json.dumps([
                    {"name": "Rima Reyes", "email": "rima@microsoft.com"},
                    {"name": "Kanika Ramji", "email": "kanika@microsoft.com"},
                ]),
                tid,
            ),
        )
        conn.commit()
        conn.close()

        action = json.loads(
            self.start(tid, body={"redirect_text": "schedule instead"}).body
        )["action"]

        self.assertIsNone(action["delivery_channel"])
        self.assertEqual(
            action["destination_ref"],
            '["rima@microsoft.com","kanika@microsoft.com"]',
        )
        self.assertEqual(action["destination_source"], "auto_key_people")


class TestApprovedEmailInput(unittest.TestCase):
    def test_rejects_invalid_or_multiple_recipient_refs(self):
        invalid_refs = (
            "alice@example.com bob@example.com",
            "alice@example.com,bob@example.com",
            "alice@example.com;bob@example.com",
            "19:meeting_Nm@thread.v2",
            '["alice@example.com"]',
            ".alice@example.com",
            "alice@example..com",
        )
        for invalid_ref in invalid_refs:
            with self.subTest(destination=invalid_ref):
                self.assertIsNone(
                    cr._approved_email_input(
                        "Subject: Hello\n\nApproved body", invalid_ref
                    )
                )


class TestChannelInferredFromTaskText(CoworkAPITestBase):
    """A manual task often states its own channel; reading it beats guessing.

    Derived from a sweep of all 1,967 live tasks. The Teams phrasing is explicit
    and scored 174 true / 0 false against source-derived labels. Email phrasing
    is NOT inferred: it conflates "email as background context" with "email as
    delivery target", and the failure is asymmetric - a wrong "email" puts a
    subject line and a sign-off on a Teams message, while falling back to the
    neutral voice is harmless.
    """

    def test_manual_task_stating_teams_binds_teams(self):
        tid = self.make_task(
            title="Ask Mehdi about the Copilot Kit",
            description="Send Mehdi a short Teams message asking about FinOps.",
            source_type="manual",
        )
        action = json.loads(self.start(tid).body)["action"]
        self.assertEqual(action["delivery_channel"], "teams")
        self.assertEqual(action["destination_source"], "auto_task_text")

    def test_ping_counts_as_teams(self):
        tid = self.make_task(
            title="Ping Kristina for a checkpoint",
            source_type="manual",
        )
        self.assertEqual(
            json.loads(self.start(tid).body)["action"]["delivery_channel"], "teams"
        )

    def test_mentioning_both_channels_is_ambiguous_and_infers_nothing(self):
        # "Send a Teams message or email" is a genuine choice, not a signal.
        tid = self.make_task(
            title="Reach out to Audrie Gordon",
            description="Send a Teams message or email outlining what she owns.",
            source_type="manual",
        )
        action = json.loads(self.start(tid).body)["action"]
        self.assertIsNone(action["delivery_channel"])
        self.assertNotEqual(action["destination_source"], "auto_task_text")

    def test_email_is_never_inferred_from_text(self):
        tid = self.make_task(
            title="Follow up with Iliyas",
            description="Reply to his email about the Power Up naming.",
            source_type="manual",
        )
        self.assertIsNone(
            json.loads(self.start(tid).body)["action"]["delivery_channel"]
        )

    def test_text_never_overrides_a_source_derived_channel(self):
        # An email-sourced task that happens to say "ping" stays email.
        tid = self.make_task(
            title="Follow up on the budget thread",
            description="Ping him about the numbers.",
            source_type="email",
        )
        self.assertEqual(
            json.loads(self.start(tid).body)["action"]["delivery_channel"], "email"
        )

    def test_inferred_channel_selects_the_teams_voice_skill(self):
        tid = self.make_task(
            title="Ping Brenda about scheduling",
            source_type="manual",
        )
        prompt = json.loads(self.start(tid).body)["action"]["composed_prompt"]
        self.assertIn("work-teams-voice", prompt)
        self.assertNotIn("work-email-voice", prompt)

    def test_text_inference_never_overrides_a_confirmed_destination(self):
        tid = self.make_task(
            title="Ping Sarah about the deck",
            source_type="manual",
        )
        self.start(tid)
        self.get_preview(tid)
        confirm = self.fetch(
            f"/api/tasks/{tid}/cowork/destination",
            method="POST",
            body=json.dumps(
                {
                    "delivery_channel": "email",
                    "destination_ref": "sarah@microsoft.com",
                    "destination_display": "Sarah Goodwin",
                }
            ),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(confirm.code, 200)
        action = json.loads(
            self.start(tid, body={"redirect_text": "try again"}).body
        )["action"]
        self.assertEqual(action["delivery_channel"], "email")
        self.assertEqual(action["destination_source"], "user_picker")

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

    def test_finalises_resumed_result_after_answered_interaction(self):
        from src.db import get_connection

        tid = self.make_task()
        action_id = self.make_action(tid)
        conn = get_connection()
        conn.execute(
            "UPDATE task_actions SET answered_interaction=?, blocked_question='' "
            "WHERE id=?",
            ('{"kind":"interaction_answer","answers":{"0":"4:00 PM"}}', action_id),
        )
        conn.commit()
        conn.close()
        cr._runs[cr.preview_label(tid)] = {
            "proc": None,
            "thread": None,
            "progress": [],
            "result": {
                "stdout": json.dumps({
                    "terminal_status": "ok",
                    "text": "Turn two completed after the user selected a slot.",
                    "tool_trace": [],
                }),
                "stderr": "",
                "error": None,
                "exit_code": 0,
                "cost_credits": 2,
            },
        }

        _, data = self.get_preview(tid)

        self.assertIn("Turn two completed", data["action"]["finding"])

    def test_ready_action_without_answer_does_not_refinalise(self):
        from src.handlers import cowork as cowork_handler

        tid = self.make_task()
        self.make_action(tid)

        with mock.patch.object(cowork_handler, "_finalise") as finalise:
            self.get_preview(tid)

        finalise.assert_not_called()

    def test_second_get_does_not_rewrite_settled_resumed_result(self):
        from src.db import get_connection
        from src.models import get_latest_task_action

        tid = self.make_task()
        action_id = self.make_action(tid)
        answer = '{"kind":"interaction_answer","answers":{"0":"4:00 PM"}}'
        result = {
            "stdout": json.dumps({
                "terminal_status": "ok",
                "text": "Turn two completed after the user selected a slot.",
                "tool_trace": [],
            }),
            "stderr": "",
            "error": None,
            "exit_code": 0,
            "cost_credits": 2,
        }
        conn = get_connection()
        conn.execute(
            "UPDATE task_actions SET answered_interaction=?, blocked_question='' "
            "WHERE id=?",
            (answer, action_id),
        )
        conn.commit()
        conn.close()
        cr._runs[cr.preview_label(tid)] = {
            "proc": None,
            "thread": None,
            "progress": [],
            "result": result,
        }
        self.get_preview(tid)
        conn = get_connection()
        conn.execute(
            "UPDATE task_actions SET updated_at='2001-01-01T00:00:00Z' WHERE id=?",
            (action_id,),
        )
        conn.commit()
        conn.close()

        self.get_preview(tid)

        self.assertEqual(
            get_latest_task_action(tid)["updated_at"],
            "2001-01-01T00:00:00Z",
        )

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

    def test_cumulative_credits_sum_completed_rows_and_skip_nulls(self):
        from src.db import get_connection

        tid = self.make_task()
        first_id = self.make_action(tid)
        second_id = self.make_action(tid)
        self.make_action(tid, state="previewing")
        conn = get_connection()
        conn.execute(
            "UPDATE task_actions SET cost_credits=? WHERE id=?",
            (30.2, first_id),
        )
        conn.execute(
            "UPDATE task_actions SET cost_credits=? WHERE id=?",
            (45.0, second_id),
        )
        conn.commit()
        conn.close()

        _, data = self.get_preview(tid)

        self.assertAlmostEqual(data["action"]["credits_cumulative"], 75.2)

    def test_cumulative_credits_are_null_when_no_run_is_attributable(self):
        tid = self.make_task()
        self.make_action(tid)

        _, data = self.get_preview(tid)

        self.assertIsNone(data["action"]["credits_cumulative"])

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

    def test_action_type_change_retires_current_card_but_preserves_history(self):
        from src.models import update_task_for_action_type

        tid = self.make_task(action_type="prepare")
        self.start(tid)
        update_task_for_action_type(tid, action_type="schedule-meeting")

        current, _ = self.get_preview(tid)
        history = self.fetch(f"/api/tasks/{tid}/cowork?history=1")

        self.assertEqual(current.code, 404)
        actions = json.loads(history.body)["actions"]
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["action_type"], "prepare")
        self.assertEqual(actions[0]["cowork_revision"], 0)

    def test_fresh_preview_snapshots_new_action_type_revision(self):
        from src.models import update_task_for_action_type

        tid = self.make_task(
            action_type="prepare",
            key_people=json.dumps([{
                "name": "Freada Sylvester",
                "email": "freadas@microsoft.com",
            }]),
        )
        self.start(tid)
        update_task_for_action_type(tid, action_type="schedule-meeting")

        response = self.start(tid)
        action = json.loads(response.body)["action"]

        self.assertEqual(action["action_type"], "schedule-meeting")
        self.assertEqual(action["cowork_revision"], 1)

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

    def test_ready_schedule_exposes_invite_only_calendar_preview(self):
        from src.db import get_connection
        from src.models import create_task_action

        tid = self.make_task(
            action_type="schedule-meeting",
            key_people=json.dumps([{
                "name": "Rima Reyes",
                "email": "rima.reyes@microsoft.com",
            }]),
        )
        reviewed = (
            "Timezone reasoning and availability notes.\n\n"
            "**Phil / Rima 1:1**\n"
            "- **When:** Monday, August 17, 3:05-3:30 PM ET\n"
            "- **Attendee:** Rima Reyes - shown free\n"
            "- **Format:** Teams meeting\n\n"
            "**Agenda**\n"
            "- Current priorities\n"
            "- Next steps\n\n"
            "Just say the word."
        )
        event = {
            "subject": "Phil / Rima 1:1",
            "start": "2026-08-17T15:05:00",
            "end": "2026-08-17T15:30:00",
            "time_zone": "America/New_York",
            "attendees": ["rima.reyes@microsoft.com"],
            "body": "model proposal",
            "is_online_meeting": True,
        }
        action = create_task_action(tid)
        selected = "fixture-selected-slot"
        answered = json.dumps({
            "kind": "interaction_answer",
            "question_raw": "{}",
            "answers": {"0": selected},
            "interaction": {
                "schedule_evidence": {
                    "valid": True,
                    "source": "FindMeetingTimes+interaction",
                    "query_backed": True,
                    "attendees": ["rima.reyes@microsoft.com"],
                    "duration_minutes": 25,
                    "slots": [{
                        "value": selected,
                        "start": "2026-08-17T15:05:00",
                        "end": "2026-08-17T15:30:00",
                        "timezone": "America/New_York",
                        "availability": {
                            "rima.reyes@microsoft.com": "free"
                        },
                    }],
                }
            },
        })
        conn = get_connection()
        conn.execute(
            "UPDATE task_actions SET state='ready', draft=?, tool_trace=?,"
            "answered_interaction=?,had_interaction=1 WHERE id=?",
            (
                reviewed,
                json.dumps([{
                    "tool_name": "mcp__outlook_calendar__CreateEvent",
                    "input": json.dumps(event),
                }]),
                answered,
                action["id"],
            ),
        )
        conn.commit()
        conn.close()

        _, data = self.get_preview(tid)
        preview = data["action"]["calendar_preview"]

        self.assertEqual(
            set(preview),
            {"attendees", "date_time", "format", "subject", "body_html"},
        )
        self.assertEqual(preview["attendees"], ["rima.reyes@microsoft.com"])
        self.assertEqual(
            preview["date_time"],
            "Monday, August 17, 2026 · 3:05 PM–3:30 PM · America/New_York",
        )
        self.assertIn("Current priorities", preview["body_html"])
        self.assertEqual(preview["subject"], "Phil / Rima 1:1")
        self.assertEqual(preview["format"], "Teams meeting")
        self.assertNotIn("When:", preview["body_html"])
        self.assertNotIn("Timezone reasoning", preview["body_html"])

    def test_ready_schedule_omits_incomplete_calendar_preview(self):
        from src.models import create_task_action
        from src.db import get_connection

        tid = self.make_task(
            action_type="schedule-meeting",
            key_people=json.dumps([{
                "name": "Rima Reyes",
                "email": "rima.reyes@microsoft.com",
            }]),
        )
        action = create_task_action(tid)
        conn = get_connection()
        conn.execute(
            "UPDATE task_actions SET state='ready', draft='No structured event' "
            "WHERE id=?",
            (action["id"],),
        )
        conn.commit()
        conn.close()

        _, data = self.get_preview(tid)
        self.assertNotIn("calendar_preview", data["action"])

    def test_calendar_preview_is_bound_to_the_selected_certified_slot(self):
        from src.handlers.cowork import _preview_calendar_event

        event = {
            "subject": "Phil / Rima 1:1",
            "start": "2026-08-20T13:05:00",
            "end": "2026-08-20T13:30:00",
            "time_zone": "Eastern Standard Time",
            "attendees": ["rima@microsoft.com"],
            "is_online_meeting": True,
            "body": "model proposal",
        }
        selected = "Thu 8/20, 1:05 PM ET"
        record = {
            "kind": "interaction_answer",
            "question_raw": "{}",
            "answers": {"0": selected},
            "interaction": {
                "schedule_evidence": {
                    "valid": True,
                    "source": "FindMeetingTimes+interaction",
                    "query_backed": True,
                    "attendees": ["rima@microsoft.com"],
                    "duration_minutes": 25,
                    "slots": [{
                        "value": selected,
                        "start": "2026-08-20T13:05:00-04:00",
                        "end": "2026-08-20T13:30:00-04:00",
                        "timezone": "Eastern Standard Time",
                        "availability": {"rima@microsoft.com": "free"},
                    }],
                }
            },
        }
        action = {
            "action_type": "schedule-meeting",
            "draft": (
                "**Phil / Rima 1:1**\n\n**Agenda**\n"
                "- Current priorities"
            ),
            "answered_interaction": json.dumps(record),
            "tool_trace": json.dumps([{
                "tool_name": "mcp__outlook_calendar__CreateEvent",
                "input": json.dumps(event),
            }]),
        }

        self.assertIsNotNone(_preview_calendar_event(action))
        missing_binding = {
            **action,
            "answered_interaction": None,
            "had_interaction": 1,
        }
        self.assertIsNone(_preview_calendar_event(missing_binding))
        drifted = json.loads(action["tool_trace"])
        drifted[0]["input"] = json.dumps({
            **event,
            "start": "2026-08-20T14:05:00",
            "end": "2026-08-20T14:30:00",
        })
        action["tool_trace"] = json.dumps(drifted)
        self.assertIsNone(_preview_calendar_event(action))

    def test_calendar_event_must_exactly_match_selected_slot(self):
        from src.services.calendar_time import calendar_event_matches_slot

        slot = {
            "start": "2026-08-20T13:00:00-04:00",
            "end": "2026-08-20T13:30:00-04:00",
            "timezone": "America/New_York",
        }
        event = {
            "start": "2026-08-20T13:05:00-04:00",
            "end": "2026-08-20T13:30:00-04:00",
            "time_zone": "America/New_York",
        }

        self.assertFalse(calendar_event_matches_slot(event, slot))
        self.assertTrue(calendar_event_matches_slot(
            {**event, "start": slot["start"]},
            slot,
        ))
        self.assertFalse(calendar_event_matches_slot(
            {**event, "start": "2026-08-20T12:55:00-04:00"}, slot
        ))
        self.assertFalse(calendar_event_matches_slot(
            {**event, "end": "2026-08-20T13:35:00-04:00"}, slot
        ))
        self.assertFalse(calendar_event_matches_slot(
            {**event, "end": "2026-08-20T13:25:00-04:00"}, slot
        ))
        self.assertFalse(calendar_event_matches_slot(
            {
                **event,
                "start": "2026-08-20T13:30:00-04:00",
                "end": "2026-08-20T13:35:00-04:00",
            },
            slot,
        ))

    def test_finalise_rejects_shortened_event_shifted_inside_selected_slot(self):
        from src.db import get_connection
        from src.handlers import cowork as cowork_handler
        from src.models import create_task_action, get_latest_task_action

        tid = self.make_task(
            title="Schedule a 30-minute SpaceX handoff with Aamer Kaleem",
            description="Schedule a 30-minute handoff today.",
            action_type="schedule-meeting",
            key_people=json.dumps([{
                "name": "Aamer Kaleem",
                "email": "aamer.kaleem@microsoft.com",
            }]),
        )
        action = create_task_action(tid, action_type="schedule-meeting")
        selected = "4:00–4:30 PM ET"
        answered = json.dumps({
            "kind": "interaction_answer",
            "question_raw": "{}",
            "answers": {"0": selected},
            "interaction": {
                "schedule_evidence": {
                    "valid": True,
                    "source": "FindMeetingTimes+interaction",
                    "query_backed": True,
                    "attendees": ["aamer.kaleem@microsoft.com"],
                    "duration_minutes": 30,
                    "slots": [{
                        "value": selected,
                        "start": "2026-08-20T16:00:00-04:00",
                        "end": "2026-08-20T16:30:00-04:00",
                        "timezone": "Eastern Standard Time",
                        "availability": {
                            "aamer.kaleem@microsoft.com": "free",
                        },
                    }],
                },
            },
        })
        conn = get_connection()
        conn.execute(
            "UPDATE task_actions SET answered_interaction=?,had_interaction=1 "
            "WHERE id=?",
            (answered, action["id"]),
        )
        conn.commit()
        conn.close()
        action = get_latest_task_action(tid)
        event = {
            "subject": "SpaceX Account Handoff",
            "start": "2026-08-20T16:05:00",
            "end": "2026-08-20T16:30:00",
            "time_zone": "Eastern Standard Time",
            "attendees": ["aamer.kaleem@microsoft.com"],
            "body": "SpaceX account handoff",
            "is_online_meeting": True,
        }
        result = {
            "stdout": json.dumps({
                "terminal_status": "ok",
                "conversation_id": "conv-2524",
                "tool_trace": [{
                    "tool_name": "mcp__outlook_calendar__CreateEvent",
                    "ok": True,
                    "input": json.dumps(event),
                }],
                "text": (
                    "**SpaceX Account Handoff**\n\n"
                    "- **When:** 4:05–4:30 PM ET\n\n"
                    "**Agenda**\n- SpaceX transition"
                ),
            }),
            "stderr": "",
            "error": None,
            "exit_code": 0,
            "cost_credits": 1,
        }

        with mock.patch.object(cowork_handler, "get_result", return_value=result), \
                mock.patch.object(cowork_handler, "is_running", return_value=False):
            updated = cowork_handler._finalise(action)

        self.assertEqual(updated["state"], "failed")
        self.assertIn("selected calendar slot", updated["error"])
        self.assertIn("30 minutes", updated["error"])

        exact_result = {
            **result,
            "stdout": json.dumps({
                **json.loads(result["stdout"]),
                "tool_trace": [{
                    "tool_name": "mcp__outlook_calendar__CreateEvent",
                    "ok": True,
                    "input": json.dumps({
                        **event,
                        "start": "2026-08-20T16:00:00",
                    }),
                }],
            }),
        }
        conn = get_connection()
        conn.execute(
            "UPDATE task_actions SET state='previewing',error=NULL WHERE id=?",
            (action["id"],),
        )
        conn.commit()
        conn.close()
        action = get_latest_task_action(tid)
        with mock.patch.object(
            cowork_handler, "get_result", return_value=exact_result
        ), mock.patch.object(cowork_handler, "is_running", return_value=False):
            updated = cowork_handler._finalise(action)

        self.assertEqual(updated["state"], "ready")
        self.assertIsNone(updated["error"])

    def test_calendar_execution_requires_a_future_start(self):
        from src.services.calendar_time import calendar_event_is_future

        self.assertTrue(
            calendar_event_is_future(
                {
                    "start": "2026-08-20T10:05:00",
                    "time_zone": "Eastern Standard Time",
                },
                now="2026-08-19T12:00:00-04:00",
            )
        )
        self.assertFalse(
            calendar_event_is_future(
                {
                    "start": "2026-08-18T10:05:00",
                    "time_zone": "Eastern Standard Time",
                },
                now="2026-08-19T12:00:00-04:00",
            )
        )
        self.assertFalse(
            calendar_event_is_future(
                {
                    "start": "2026-08-19T10:00:00",
                    "time_zone": "India Standard Time",
                },
                now="2026-08-19T08:00:00+00:00",
            )
        )
        self.assertTrue(
            calendar_event_is_future(
                {
                    "start": "2026-12-15T10:00:00-05:00",
                    "time_zone": "Eastern Standard Time",
                },
                now="2026-12-14T12:00:00+00:00",
            )
        )
        self.assertFalse(
            calendar_event_is_future(
                {
                    "start": "2026-03-08T02:30:00",
                    "time_zone": "Eastern Standard Time",
                },
                now="2026-03-07T12:00:00+00:00",
            )
        )
        self.assertFalse(
            calendar_event_is_future(
                {
                    "start": "2026-11-01T01:30:00",
                    "time_zone": "Eastern Standard Time",
                },
                now="2026-10-31T12:00:00+00:00",
            )
        )
        self.assertFalse(
            calendar_event_is_future(
                {
                    "start": "2026-08-20T10:00:00-04:00",
                    "time_zone": "India Standard Time",
                },
                now="2026-08-19T08:00:00+00:00",
            )
        )
        self.assertFalse(
            calendar_event_is_future(
                {
                    "start": "2026-08-20T10:00:00-04:00",
                    "time_zone": "Mars/Olympus",
                },
                now="2026-08-19T08:00:00+00:00",
            )
        )


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

    def test_schedule_meeting_confirms_attendee_without_delivery_channel(self):
        tid = self.make_task(action_type="schedule-meeting")
        action_id = self.make_action(tid, state="ready")
        from src.db import get_connection

        conn = get_connection()
        conn.execute(
            "UPDATE task_actions SET action_type='schedule-meeting' WHERE id=?",
            (action_id,),
        )
        conn.commit()
        conn.close()

        response = self._confirm(
            tid,
            {
                "destination_ref": "rima.reyes@microsoft.com",
                "destination_display": "Rima Reyes",
            },
        )
        action = json.loads(response.body)["action"]

        self.assertEqual(response.code, 200)
        self.assertIsNone(action["delivery_channel"])
        self.assertEqual(action["destination_ref"], "rima.reyes@microsoft.com")
        self.assertIsNotNone(action["destination_confirmed_at"])

    def test_schedule_meeting_rejects_message_delivery_channel(self):
        tid = self.make_task(action_type="schedule-meeting")
        action_id = self.make_action(tid, state="ready")
        from src.db import get_connection

        conn = get_connection()
        conn.execute(
            "UPDATE task_actions SET action_type='schedule-meeting' WHERE id=?",
            (action_id,),
        )
        conn.commit()
        conn.close()

        response = self._confirm(
            tid,
            {
                "delivery_channel": "teams",
                "destination_ref": "rima.reyes@microsoft.com",
                "destination_display": "Rima Reyes",
            },
        )

        self.assertEqual(response.code, 400)

    def test_respond_email_rejects_teams_channel_or_conversation_ref(self):
        tid = self.make_task(action_type="respond-email")
        action_id = self.make_action(tid, state="ready")
        from src.db import get_connection

        conn = get_connection()
        conn.execute(
            "UPDATE task_actions SET action_type='respond-email' WHERE id=?",
            (action_id,),
        )
        conn.commit()
        conn.close()

        teams_response = self._confirm(
            tid,
            {
                "delivery_channel": "teams",
                "destination_ref": "19:teams-thread@thread.v2",
                "destination_display": "Phil Topness",
            },
        )
        conversation_as_email = self._confirm(
            tid,
            {
                "delivery_channel": "email",
                "destination_ref": "19:teams-thread@thread.v2",
                "destination_display": "Phil Topness",
            },
        )

        self.assertEqual(teams_response.code, 400)
        self.assertEqual(conversation_as_email.code, 400)

    def test_respond_email_rejects_invalid_or_multiple_recipient_refs(self):
        invalid_refs = (
            "alice@example.com bob@example.com",
            "alice@example.com,bob@example.com",
            "alice@example.com;bob@example.com",
            "19:meeting_Nm@thread.v2",
            '["alice@example.com"]',
            ".alice@example.com",
            "alice@example..com",
        )
        for invalid_ref in invalid_refs:
            with self.subTest(destination_ref=invalid_ref):
                tid = self.make_task(action_type="respond-email")
                action_id = self.make_action(tid, state="ready")
                from src.db import get_connection

                conn = get_connection()
                conn.execute(
                    "UPDATE task_actions SET action_type='respond-email' "
                    "WHERE id=?",
                    (action_id,),
                )
                conn.commit()
                conn.close()

                response = self._confirm(
                    tid,
                    {
                        "delivery_channel": "email",
                        "destination_ref": invalid_ref,
                        "destination_display": "Recipient",
                    },
                )

                self.assertEqual(response.code, 400)

    def test_email_delivery_channel_rejects_invalid_recipient_for_general_action(self):
        tid = self.make_task(action_type="follow-up")
        action_id = self.make_action(tid, state="ready")
        from src.db import get_connection

        conn = get_connection()
        conn.execute(
            "UPDATE task_actions SET delivery_channel='email' WHERE id=?",
            (action_id,),
        )
        conn.commit()
        conn.close()

        response = self._confirm(
            tid,
            {
                "delivery_channel": "email",
                "destination_ref": "Phil Topness",
                "destination_display": "Phil Topness",
            },
        )

        self.assertEqual(response.code, 400)

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


class TestExecuteApprovedAction(CoworkAPITestBase):
    def setUp(self):
        super().setUp()
        from src.handlers import cowork as cowork_handler

        self._execute_fn = getattr(cowork_handler, "EXECUTE_FN", None)
        self._execute_transport_fn = getattr(
            cowork_handler, "EXECUTE_TRANSPORT_ENABLED_FN", None
        )
        self._now_fn = getattr(cowork_handler, "NOW_FN", None)
        cowork_handler.EXECUTE_TRANSPORT_ENABLED_FN = lambda: True
        cowork_handler.NOW_FN = lambda: "2026-08-13T12:00:00-04:00"
        self.started = []
        cowork_handler.EXECUTE_FN = lambda task_id, prompt, conversation_id, **kw: (
            self.started.append((task_id, prompt, conversation_id, kw))
            or cr.execution_label(task_id)
        )

    def tearDown(self):
        from src.handlers import cowork as cowork_handler

        cowork_handler.EXECUTE_FN = self._execute_fn
        cowork_handler.EXECUTE_TRANSPORT_ENABLED_FN = self._execute_transport_fn
        cowork_handler.NOW_FN = self._now_fn
        super().tearDown()

    def _ready_action(self, tid, **overrides):
        from src.db import get_connection
        from src.models import get_task, update_task

        task = get_task(tid)
        if (
            overrides.get("action_type") == "schedule-meeting"
            and task
            and task.get("title") == "Send Sarah the deck"
        ):
            update_task(tid, title="Schedule a 25-minute meeting")

        action_id = self.make_action(tid, state="ready")
        values = {
            "draft": "Hi Sarah - the deck is attached.",
            "conversation_id": "tenant:user:conversation",
            "delivery_channel": "teams",
            "destination_ref": "sarah@microsoft.com",
            "destination_display": "Sarah Goodwin",
            "destination_confirmed_at": "2026-08-11T12:00:00Z",
        }
        values.update(overrides)
        if (
            values.get("action_type") == "schedule-meeting"
            and "answered_interaction" not in values
            and values.get("tool_trace")
        ):
            from src.services.calendar_time import calendar_event_duration_minutes

            trace = json.loads(values["tool_trace"])
            candidates = [
                item for item in trace
                if "createevent" in str(item.get("tool_name") or "").lower()
            ]
            if len(candidates) == 1:
                event = candidates[0].get("input")
                event = json.loads(event) if isinstance(event, str) else event
                if isinstance(event, dict):
                    selected = "fixture-selected-slot"
                    attendees = [
                        str(email).strip().lower()
                        for email in event.get("attendees") or []
                    ]
                    values["answered_interaction"] = json.dumps({
                        "kind": "interaction_answer",
                        "question_raw": "{}",
                        "answers": {"0": selected},
                        "interaction": {
                            "schedule_evidence": {
                                "valid": True,
                                "source": "FindMeetingTimes+interaction",
                                "query_backed": True,
                                "attendees": attendees,
                                "duration_minutes": calendar_event_duration_minutes(
                                    event
                                ),
                                "slots": [{
                                    "value": selected,
                                    "start": event.get("start"),
                                    "end": event.get("end"),
                                    "timezone": event.get("time_zone"),
                                    "availability": {
                                        email: "free" for email in attendees
                                    },
                                }],
                            }
                        },
                    })
                    values["had_interaction"] = 1
        conn = get_connection()
        conn.execute(
            "UPDATE task_actions SET "
            + ", ".join(f"{key}=?" for key in values)
            + " WHERE id=?",
            (*values.values(), action_id),
        )
        conn.commit()
        conn.close()
        return action_id

    def _execute(self, tid, snapshot=None, confirmed=True):
        from src.models import get_latest_task_action

        parent = get_latest_task_action(tid)
        if snapshot is None:
            draft = (
                parent.get("draft_edited")
                or parent.get("draft")
                or (
                    parent.get("finding")
                    if parent.get("action_type") == "schedule-meeting"
                    else ""
                )
                or ""
            ).strip()
            snapshot = {
                "parent_action_id": parent["id"],
                "draft": draft,
                "destination_ref": parent.get("destination_ref") or "",
                "destination_display": parent.get("destination_display") or "",
                "delivery_channel": parent.get("delivery_channel") or "",
                "destination_confirmed_at": parent.get("destination_confirmed_at") or "",
            }
        return self.fetch(
            f"/api/tasks/{tid}/cowork/execute",
            method="POST",
            body=json.dumps({"approved_snapshot": snapshot}),
            headers={
                "Content-Type": "application/json",
                **({"X-Riveter-Action": "confirm"} if confirmed else {}),
            },
        )

    def test_execute_response_keeps_completed_credit_total(self):
        from src.db import get_connection

        tid = self.make_task(action_type="respond-email")
        first_id = self._ready_action(
            tid,
            action_type="respond-email",
            delivery_channel="email",
            draft="Subject: Earlier draft\n\nEarlier body",
        )
        conn = get_connection()
        conn.execute(
            "UPDATE task_actions SET cost_credits=? WHERE id=?",
            (42.5, first_id),
        )
        conn.commit()
        conn.close()
        self._ready_action(
            tid,
            action_type="respond-email",
            delivery_channel="email",
            draft="Subject: Final draft\n\nApproved body",
        )

        response = self._execute(tid)
        data = json.loads(response.body)

        self.assertEqual(response.code, 202, response.body)
        self.assertAlmostEqual(data["action"]["credits_cumulative"], 42.5)

    def test_schedule_execution_rejects_attendee_drift_after_preview(self):
        from src.db import get_connection

        tid = self.make_task(
            action_type="schedule-meeting",
            key_people=json.dumps([
                {"name": "Rima Reyes", "email": "rima@microsoft.com"},
                {"name": "Henry James", "email": "henry@microsoft.com"},
            ]),
        )
        self._ready_action(
            tid,
            action_type="schedule-meeting",
            delivery_channel=None,
            destination_ref='["rima@microsoft.com","henry@microsoft.com"]',
            destination_display="Rima Reyes, Henry James",
        )
        conn = get_connection()
        conn.execute(
            "UPDATE tasks SET key_people=? WHERE id=?",
            (
                json.dumps([
                    {"name": "Rima Reyes", "email": "rima@microsoft.com"},
                    {"name": "Kanika Ramji", "email": "kanika@microsoft.com"},
                ]),
                tid,
            ),
        )
        conn.commit()
        conn.close()

        response = self._execute(tid)

        self.assertEqual(response.code, 409)
        self.assertEqual(
            json.loads(response.body)["error"],
            "The attendee list changed after this preview. Start over and "
            "review availability again before creating the meeting.",
        )

    def test_execution_claim_rejects_concurrent_attendee_change(self):
        import src.handlers.cowork as handler
        from src.models import create_execution_action, update_task

        tid = self.make_task(
            action_type="schedule-meeting",
            key_people=json.dumps([
                {"name": "Rima Reyes", "email": "rima@microsoft.com"},
            ]),
        )
        self._ready_action(
            tid,
            action_type="schedule-meeting",
            delivery_channel=None,
            destination_ref="rima@microsoft.com",
            destination_display="Rima Reyes",
            finding=(
                "**Phil / Rima 1:1**\n"
                "- **When:** Monday, August 17, 3:05-3:30 PM ET\n"
                "- **Teams meeting:** included\n\n"
                "**Agenda**\n- Quick 1:1 to sync up"
            ),
            draft=None,
            tool_trace=json.dumps([{
                "tool_name": "mcp__outlook_calendar__CreateEvent",
                "ok": True,
                "input": json.dumps({
                    "subject": "Phil / Rima 1:1",
                    "start": "2026-08-17T15:05:00",
                    "end": "2026-08-17T15:30:00",
                    "time_zone": "Eastern Standard Time",
                    "attendees": ["rima@microsoft.com"],
                    "is_online_meeting": True,
                    "body": "model proposal",
                }),
            }]),
        )
        original = handler.create_execution_action

        def change_then_claim(parent_action_id, snapshot):
            update_task(
                tid,
                key_people=json.dumps([{
                    "name": "Kanika Ramji",
                    "email": "kanika@microsoft.com",
                }]),
            )
            return create_execution_action(parent_action_id, snapshot)

        handler.create_execution_action = change_then_claim
        try:
            response = self._execute(tid)
        finally:
            handler.create_execution_action = original

        self.assertEqual(response.code, 409)
        self.assertIn(
            "changed after review",
            json.loads(response.body)["error"],
        )
        self.assertEqual(self.started, [])

    def test_delivery_evidence_must_match_the_approved_action(self):
        from src.handlers.cowork import (
            _delivery_evidence_matches,
            _delivery_tool_matches_action,
        )

        self.assertFalse(
            _delivery_tool_matches_action(
                {"action_type": "respond-email", "delivery_channel": "email"},
                "mcp__m365_teams__PostMessage",
            )
        )
        self.assertTrue(
            _delivery_tool_matches_action(
                {"action_type": "respond-email", "delivery_channel": "email"},
                "mcp__outlook__SendEmailWithAttachments",
            )
        )
        self.assertTrue(
            _delivery_tool_matches_action(
                {"action_type": "schedule-meeting", "delivery_channel": "teams"},
                "mcp__outlook_calendar__CreateEvent",
            )
        )
        action = {
            "action_type": "follow-up",
            "delivery_channel": "teams",
            "destination_ref": "sarah@microsoft.com",
            "draft": "Approved text",
        }
        matching = {
            "tools": [{
                "name": "mcp__m365_teams__PostMessage",
                "ok": True,
                "input": json.dumps({
                    "recipient": "sarah@microsoft.com",
                    "message": "Approved text",
                }),
            }]
        }
        self.assertTrue(_delivery_evidence_matches(action, matching))
        wrong_recipient = json.loads(json.dumps(matching))
        wrong_recipient["tools"][0]["input"] = json.dumps({
            "recipient": "other@microsoft.com",
            "message": "Approved text",
        })
        self.assertFalse(_delivery_evidence_matches(action, wrong_recipient))
        changed_content = json.loads(json.dumps(matching))
        changed_content["tools"][0]["input"] = json.dumps({
            "recipient": "sarah@microsoft.com",
            "message": "Different text",
        })
        self.assertFalse(_delivery_evidence_matches(action, changed_content))
        multiline_action = {
            **action,
            "draft": "First line\nSecond line",
        }
        multiline = {
            "tools": [{
                "name": "mcp__m365_teams__PostMessage",
                "ok": True,
                "input": json.dumps({
                    "recipient": "sarah@microsoft.com",
                    "message": "First line\nSecond line",
                }),
            }]
        }
        self.assertTrue(_delivery_evidence_matches(multiline_action, multiline))
        meeting_action = {
            "action_type": "schedule-meeting",
            "delivery_channel": "teams",
            "destination_ref": "sarah@microsoft.com",
            "draft": (
                "**Project review**\n"
                "- **When:** Friday, August 21, 10:00-10:25 AM ET (25 min)\n"
                "- **Attendee:** Sarah\n"
                "- **Teams meeting:** included\n\n"
                "**Agenda**\n"
                "- Review project status"
            ),
        }
        changed_meeting = {
            "tools": [{
                "name": "mcp__outlook_calendar__CreateEvent",
                "ok": True,
                "input": {
                    "attendees": ["sarah@microsoft.com"],
                    "body": "Different meeting",
                },
            }]
        }
        self.assertFalse(_delivery_evidence_matches(meeting_action, changed_meeting))
        approved_event = {
            "subject": "Project review",
            "start": "2026-08-21T10:00:00",
            "end": "2026-08-21T10:25:00",
            "time_zone": "America/New_York",
            "attendees": ["sarah@microsoft.com"],
            "is_online_meeting": True,
            "content_type": "html",
            "body": "Review project status",
        }
        matching_meeting = {
            "tools": [{
                "name": "mcp__outlook_calendar__CreateEvent",
                "ok": True,
                "input": {
                    key: value
                    for key, value in approved_event.items()
                    if key not in {"body", "content_type"}
                } | {
                    "body": (
                        cr._render_calendar_event_body(
                            meeting_action["draft"], approved_event["subject"]
                        )
                        + "<br><br><!-- aether-footer -->"
                        + cr._AETHER_FOOTERS["calendar"]
                    ),
                },
            }]
        }
        self.assertTrue(
            _delivery_evidence_matches(
                meeting_action, matching_meeting, approved_event
            )
        )
        stale_trace_with_approved_input = json.loads(json.dumps(matching_meeting))
        tool = stale_trace_with_approved_input["tools"][0]
        tool["approved_input"] = tool["input"]
        tool["input"] = approved_event
        self.assertTrue(
            _delivery_evidence_matches(
                meeting_action, stale_trace_with_approved_input, approved_event
            )
        )
        changed_approved_input = json.loads(
            json.dumps(stale_trace_with_approved_input)
        )
        changed_approved_input["tools"][0]["approved_input"]["body"] = (
            "Different approved body"
        )
        self.assertFalse(
            _delivery_evidence_matches(
                meeting_action, changed_approved_input, approved_event
            )
        )
        failed_approved_write = json.loads(
            json.dumps(stale_trace_with_approved_input)
        )
        failed_approved_write["tools"][0]["ok"] = False
        self.assertFalse(
            _delivery_evidence_matches(
                meeting_action, failed_approved_write, approved_event
            )
        )
        missing_footer = json.loads(json.dumps(matching_meeting))
        missing_footer["tools"][0]["input"]["body"] = (
            cr._render_calendar_event_body(
                meeting_action["draft"], approved_event["subject"]
            )
        )
        self.assertFalse(
            _delivery_evidence_matches(
                meeting_action, missing_footer, approved_event
            )
        )
        multi_attendee_action = {
            **meeting_action,
            "destination_ref": json.dumps([
                "sarah@microsoft.com",
                "rima@microsoft.com",
            ], separators=(",", ":")),
        }
        multi_attendee_event = {
            **approved_event,
            "attendees": ["sarah@microsoft.com", "rima@microsoft.com"],
        }
        multi_attendee_evidence = json.loads(json.dumps(matching_meeting))
        multi_attendee_evidence["tools"][0]["input"]["attendees"] = [
            "sarah@microsoft.com",
            "rima@microsoft.com",
        ]
        self.assertTrue(
            _delivery_evidence_matches(
                multi_attendee_action,
                multi_attendee_evidence,
                multi_attendee_event,
            )
        )
        extra_recipient = json.loads(json.dumps(matching_meeting))
        extra_recipient["tools"][0]["input"]["attendees"].append(
            "attacker@microsoft.com"
        )
        self.assertFalse(_delivery_evidence_matches(
            meeting_action, extra_recipient, approved_event
        ))
        aliased_recipient = json.loads(json.dumps(matching_meeting))
        aliased_recipient["tools"][0]["input"]["bcc"] = [
            "attacker@microsoft.com"
        ]
        self.assertFalse(_delivery_evidence_matches(
            meeting_action, aliased_recipient, approved_event
        ))
        altered_content = json.loads(json.dumps(matching_meeting))
        altered_content["tools"][0]["input"]["content"] = "Different meeting"
        self.assertFalse(_delivery_evidence_matches(
            meeting_action, altered_content, approved_event
        ))
        changed_case = json.loads(json.dumps(matching_meeting))
        changed_case["tools"][0]["input"]["body"] = meeting_action["draft"].upper()
        self.assertFalse(
            _delivery_evidence_matches(
                meeting_action, changed_case, approved_event
            )
        )
        smuggled_meeting = json.loads(json.dumps(changed_meeting))
        smuggled_meeting["tools"][0]["input"] = {
            "attendees": ["attacker@microsoft.com"],
            "body": (
                "Project review on Friday at 10:00 changed; originally for "
                "sarah@microsoft.com"
            ),
        }
        self.assertFalse(_delivery_evidence_matches(
            meeting_action, smuggled_meeting, approved_event
        ))
        email_action = {
            "action_type": "respond-email",
            "delivery_channel": "email",
            "destination_ref": "sarah@microsoft.com",
            "draft": "Subject: Status update\n\nApproved email body",
        }
        matching_email = {
            "tools": [{
                "name": "mcp__outlook__SendEmailWithAttachments",
                "ok": True,
                "input": {
                    "to": ["sarah@microsoft.com"],
                    "subject": "Status update",
                    "body": "Approved email body",
                    "content_type": "Text",
                },
            }]
        }
        self.assertTrue(_delivery_evidence_matches(email_action, matching_email))
        approved_email = json.loads(json.dumps(matching_email))
        approved_email["tools"][0]["approved_input"] = {
            "to": ["sarah@microsoft.com"],
            "subject": "Status update",
            "content_type": "HTML",
            "body": (
                "Approved email body<br><br><!-- aether-footer -->"
                '<span style="font-size:11px;color:#666;">Sent by '
                '<a href="https://aka.ms/cowork?cw_source=outlook&amp;'
                'cw_tool=SendEmailWithAttachments">Copilot Cowork</a></span>'
            ),
        }
        self.assertTrue(_delivery_evidence_matches(email_action, approved_email))
        changed_approved_body = json.loads(json.dumps(approved_email))
        changed_approved_body["tools"][0]["approved_input"]["body"] = (
            "Different body<br><br><!-- aether-footer -->"
            + approved_email["tools"][0]["approved_input"]["body"].split(
                "<!-- aether-footer -->", 1
            )[1]
        )
        self.assertFalse(
            _delivery_evidence_matches(email_action, changed_approved_body)
        )
        failed_approved_email = json.loads(json.dumps(approved_email))
        failed_approved_email["tools"][0]["ok"] = False
        self.assertFalse(_delivery_evidence_matches(
            email_action, failed_approved_email
        ))
        duplicate_recipient = json.loads(json.dumps(matching_email))
        duplicate_recipient["tools"][0]["input"]["cc"] = [
            "sarah@microsoft.com"
        ]
        self.assertFalse(_delivery_evidence_matches(
            email_action, duplicate_recipient
        ))
        email_with_attachment = json.loads(json.dumps(matching_email))
        email_with_attachment["tools"][0]["input"]["attachments"] = [{
            "path": "C:/sensitive.pdf",
        }]
        self.assertFalse(_delivery_evidence_matches(
            email_action, email_with_attachment
        ))
        changed_subject = json.loads(json.dumps(matching_email))
        changed_subject["tools"][0]["input"]["subject"] = "Different subject"
        self.assertFalse(_delivery_evidence_matches(email_action, changed_subject))
        duplicate = {"tools": matching["tools"] * 2}
        self.assertFalse(_delivery_evidence_matches(action, duplicate))

    def test_requires_riveter_confirmation_header(self):
        tid = self.make_task()
        self._ready_action(tid)
        self.assertEqual(self._execute(tid, confirmed=False).code, 403)

    def test_rejects_snapshot_changed_after_modal_opened(self):
        from src.db import get_connection
        from src.models import get_latest_task_action

        tid = self.make_task()
        self._ready_action(tid)
        parent = get_latest_task_action(tid)
        snapshot = {
            "parent_action_id": parent["id"],
            "draft": parent["draft"],
            "destination_ref": parent["destination_ref"],
            "destination_display": parent["destination_display"],
            "delivery_channel": parent["delivery_channel"],
            "destination_confirmed_at": parent["destination_confirmed_at"],
        }
        conn = get_connection()
        conn.execute(
            "UPDATE task_actions SET draft_edited=? WHERE id=?",
            ("Changed after review", parent["id"]),
        )
        conn.commit()
        conn.close()

        self.assertEqual(self._execute(tid, snapshot=snapshot).code, 409)
        self.assertEqual(self.started, [])

    def test_creates_audited_execution_child_and_starts_follow_up(self):
        tid = self.make_task()
        parent_id = self._ready_action(tid)

        response = self._execute(tid)

        self.assertEqual(response.code, 202)
        action = json.loads(response.body)["action"]
        self.assertEqual(action["state"], "executing")
        self.assertEqual(action["parent_action_id"], parent_id)
        self.assertEqual(self.started[0][2], "tenant:user:conversation")
        self.assertEqual(self.started[0][3]["approval_kind"], "teams")
        self.assertEqual(
            self.started[0][3]["approved_snapshot"]["destination_ref"],
            "sarah@microsoft.com",
        )
        self.assertEqual(
            self.started[0][3]["approved_snapshot"]["draft"],
            "Hi Sarah - the deck is attached.",
        )
        self.assertIn("Sarah Goodwin", self.started[0][1])
        self.assertIn("deck is attached", self.started[0][1])

        edit = self.fetch(
            f"/api/tasks/{tid}/cowork",
            method="PUT",
            body=json.dumps({"draft_edited": "Changed after execution"}),
        )
        self.assertEqual(edit.code, 409)

    def test_routes_email_and_calendar_approvals_by_action(self):
        email_tid = self.make_task()
        self._ready_action(
            email_tid,
            delivery_channel="email",
            draft="Subject: Deck follow-up\n\nHi Sarah - the deck is attached.",
        )
        self.assertEqual(self._execute(email_tid).code, 202)
        self.assertEqual(self.started[-1][3]["approval_kind"], "email")

        calendar_tid = self.make_task(
            action_type="schedule-meeting",
            key_people=json.dumps([
                {"name": "Rima Reyes", "email": "rima@microsoft.com"},
            ]),
        )
        calendar_event = {
            "subject": "Phil / Rima 1:1",
            "start": "2026-08-19T11:05:00",
            "end": "2026-08-19T11:30:00",
            "time_zone": "America/New_York",
            "attendees": ["rima@microsoft.com"],
            "content_type": "html",
            "body": "Quick 1:1 to sync up.",
        }
        self._ready_action(
            calendar_tid,
            action_type="schedule-meeting",
            delivery_channel=None,
            destination_ref="rima@microsoft.com",
            destination_display="Rima Reyes",
            finding=(
                "Phil / Rima 1:1\nWhen: Wednesday, August 19, "
                "11:05-11:30 AM ET\n**Teams meeting:** included"
            ),
            draft=None,
            tool_trace=json.dumps([{
                "tool_name": "mcp__outlook_calendar__CreateEvent",
                "ok": True,
                "input": json.dumps(calendar_event),
            }]),
        )
        self.assertEqual(self._execute(calendar_tid).code, 202)
        self.assertEqual(self.started[-1][3]["approval_kind"], "calendar")
        self.assertEqual(
            self.started[-1][3]["approved_calendar_event"],
            {**calendar_event, "is_online_meeting": True},
        )

    def test_calendar_execution_defaults_omitted_optional_preview_fields(self):
        calendar_tid = self.make_task(
            action_type="schedule-meeting",
            key_people=json.dumps([
                {"name": "Rima Reyes", "email": "rima@microsoft.com"},
            ]),
        )
        escaped_body = (
            "&lt;p&gt;Quick 1:1 to sync up.&lt;/p&gt;"
            "&lt;ul&gt;&lt;li&gt;Current priorities&lt;/li&gt;&lt;/ul&gt;"
        )
        calendar_event = {
            "subject": "Phil / Rima 1:1",
            "start": "2026-08-17T15:05:00",
            "end": "2026-08-17T15:30:00",
            "time_zone": "America/New_York",
            "attendees": ["rima@microsoft.com"],
            "body": escaped_body,
        }
        self._ready_action(
            calendar_tid,
            action_type="schedule-meeting",
            delivery_channel=None,
            destination_ref="rima@microsoft.com",
            destination_display="Rima Reyes",
            finding=(
                "Phil / Rima 1:1\nWhen: Monday, August 17, "
                "3:05-3:30 PM ET\n**Teams meeting:** included"
            ),
            draft=None,
            tool_trace=json.dumps([{
                "tool_name": "mcp__outlook_calendar__CreateEvent",
                "ok": True,
                "input": json.dumps(calendar_event),
            }]),
        )

        response = self._execute(calendar_tid)

        self.assertEqual(response.code, 202, response.body)
        self.assertTrue(
            self.started[-1][3]["approved_calendar_event"]["is_online_meeting"]
        )
        self.assertEqual(
            self.started[-1][3]["approved_calendar_event"],
            {
                **calendar_event,
                "content_type": "html",
                "is_online_meeting": True,
            },
        )

    def test_calendar_execution_accepts_where_teams_meeting_marker(self):
        calendar_tid = self.make_task(
            action_type="schedule-meeting",
            key_people=json.dumps([
                {"name": "Rima Reyes", "email": "rima@microsoft.com"},
            ]),
        )
        calendar_event = {
            "subject": "Phil / Rima 1:1",
            "start": "2026-08-17T15:05:00",
            "end": "2026-08-17T15:30:00",
            "time_zone": "America/New_York",
            "attendees": ["rima@microsoft.com"],
            "body": "&lt;p&gt;Quick 1:1 to sync up.&lt;/p&gt;",
        }
        self._ready_action(
            calendar_tid,
            action_type="schedule-meeting",
            delivery_channel=None,
            destination_ref="rima@microsoft.com",
            destination_display="Rima Reyes",
            finding=(
                "Phil / Rima 1:1\nWhen: Monday, August 17, "
                "3:05-3:30 PM ET\n**Where:** Teams meeting"
            ),
            draft=None,
            tool_trace=json.dumps([{
                "tool_name": "mcp__outlook_calendar__CreateEvent",
                "ok": True,
                "input": json.dumps(calendar_event),
            }]),
        )

        response = self._execute(calendar_tid)

        self.assertEqual(response.code, 202, response.body)

    def test_calendar_execution_rejects_a_past_start_without_launching(self):
        tid = self.make_task(
            action_type="schedule-meeting",
            key_people=json.dumps([
                {"name": "Rima Reyes", "email": "rima@microsoft.com"},
            ]),
        )
        event = {
            "subject": "Phil / Rima 1:1",
            "start": "2026-08-12T15:05:00",
            "end": "2026-08-12T15:30:00",
            "time_zone": "Eastern Standard Time",
            "attendees": ["rima@microsoft.com"],
            "is_online_meeting": True,
            "body": "model proposal",
        }
        self._ready_action(
            tid,
            action_type="schedule-meeting",
            delivery_channel=None,
            destination_ref="rima@microsoft.com",
            destination_display="Rima Reyes",
            finding=(
                "**Phil / Rima 1:1**\n"
                "- **When:** Wednesday, August 12, 3:05-3:30 PM ET\n"
                "- **Teams meeting:** included\n\n"
                "**Agenda**\n- Quick 1:1 to sync up"
            ),
            draft=None,
            tool_trace=json.dumps([{
                "tool_name": "mcp__outlook_calendar__CreateEvent",
                "ok": True,
                "input": json.dumps(event),
            }]),
        )

        response = self._execute(tid)

        self.assertEqual(response.code, 409)
        self.assertIn("time has passed", json.loads(response.body)["error"])
        self.assertEqual(self.started, [])

    def test_calendar_execution_rejects_duration_drift_without_launching(self):
        tid = self.make_task(
            title="Schedule a 45-minute meeting",
            action_type="schedule-meeting",
            key_people=json.dumps([
                {"name": "Rima Reyes", "email": "rima@microsoft.com"},
            ]),
        )
        event = {
            "subject": "Phil / Rima 1:1",
            "start": "2026-08-17T15:05:00",
            "end": "2026-08-17T15:30:00",
            "time_zone": "Eastern Standard Time",
            "attendees": ["rima@microsoft.com"],
            "is_online_meeting": True,
            "body": "model proposal",
        }
        self._ready_action(
            tid,
            action_type="schedule-meeting",
            delivery_channel=None,
            destination_ref="rima@microsoft.com",
            destination_display="Rima Reyes",
            finding=(
                "**Phil / Rima 1:1**\n"
                "- **When:** Monday, August 17, 3:05-3:30 PM ET\n"
                "- **Teams meeting:** included\n\n"
                "**Agenda**\n- Quick 1:1 to sync up"
            ),
            draft=None,
            tool_trace=json.dumps([{
                "tool_name": "mcp__outlook_calendar__CreateEvent",
                "ok": True,
                "input": json.dumps(event),
            }]),
        )

        response = self._execute(tid)

        self.assertEqual(response.code, 409)
        self.assertIn("duration changed", json.loads(response.body)["error"])
        self.assertEqual(self.started, [])

    def test_calendar_execution_rejects_missing_structured_preview(self):
        tid = self.make_task(
            action_type="schedule-meeting",
            key_people=json.dumps([
                {"name": "Rima Reyes", "email": "rima@microsoft.com"},
            ]),
        )
        self._ready_action(
            tid,
            action_type="schedule-meeting",
            delivery_channel=None,
            destination_ref="rima@microsoft.com",
            destination_display="Rima Reyes",
        )
        response = self._execute(tid)
        self.assertEqual(response.code, 409)
        self.assertEqual(self.started, [])

    def test_schedule_execution_uses_finding_when_draft_is_empty(self):
        meeting_details = (
            "Phil / Rima 1:1\n"
            "When: Monday, August 17, 3:05-3:30 PM ET\n"
            "Teams meeting: included\n"
            "Agenda: Current priorities and open questions"
        )
        calendar_event = {
            "subject": "Phil / Rima 1:1",
            "start": "2026-08-17T15:05:00",
            "end": "2026-08-17T15:30:00",
            "time_zone": "America/New_York",
            "attendees": ["rima@microsoft.com"],
            "content_type": "html",
            "body": "Current priorities and open questions",
        }
        tid = self.make_task(
            action_type="schedule-meeting",
            key_people=json.dumps([
                {"name": "Rima Reyes", "email": "rima@microsoft.com"},
            ]),
        )
        self._ready_action(
            tid,
            action_type="schedule-meeting",
            draft=None,
            finding=f"\n\t{meeting_details}\n",
            delivery_channel=None,
            destination_ref="rima@microsoft.com",
            destination_display="Rima Reyes",
            tool_trace=json.dumps([{
                "tool_name": "mcp__outlook_calendar__CreateEvent",
                "ok": True,
                "input": json.dumps(calendar_event),
            }]),
        )

        response = self._execute(tid)

        self.assertEqual(response.code, 202)
        action = json.loads(response.body)["action"]
        self.assertEqual(action["draft"], meeting_details)
        self.assertEqual(
            self.started[-1][3]["approved_snapshot"]["draft"],
            meeting_details,
        )
        self.assertIn("Monday, August 17", self.started[-1][1])

    def test_rejects_unconfirmed_destination(self):
        tid = self.make_task()
        self._ready_action(tid, destination_confirmed_at=None)
        self.assertEqual(self._execute(tid).code, 409)
        self.assertEqual(self.started, [])

    def test_rejects_empty_draft_and_missing_conversation(self):
        tid = self.make_task()
        self._ready_action(tid, draft="")
        self.assertEqual(self._execute(tid).code, 409)

        tid2 = self.make_task()
        self._ready_action(tid2, conversation_id=None)
        self.assertEqual(self._execute(tid2).code, 409)

    def test_rejects_subjectless_email_draft(self):
        tid = self.make_task(action_type="respond-email")
        self._ready_action(
            tid,
            action_type="respond-email",
            delivery_channel="email",
            draft="afafsas",
        )

        response = self._execute(tid)

        self.assertEqual(response.code, 409)
        self.assertIn("subject", json.loads(response.body)["error"].lower())
        self.assertEqual(self.started, [])

    def test_rejects_email_with_empty_subject_line(self):
        tid = self.make_task(action_type="respond-email")
        self._ready_action(
            tid,
            action_type="respond-email",
            delivery_channel="email",
            draft="Subject:\n\nHi Phil,\n\nThis has no subject.",
        )

        response = self._execute(tid)

        self.assertEqual(response.code, 409)
        self.assertEqual(self.started, [])

    def test_rejects_email_with_subject_but_no_body(self):
        tid = self.make_task(action_type="respond-email")
        self._ready_action(
            tid,
            action_type="respond-email",
            delivery_channel="email",
            draft="Subject: Hello",
        )

        response = self._execute(tid)

        self.assertEqual(response.code, 409)
        self.assertIn("body", json.loads(response.body)["error"].lower())
        self.assertEqual(self.started, [])

    def test_delivery_channel_email_requires_subject_and_body(self):
        tid = self.make_task(action_type="follow-up")
        self._ready_action(
            tid,
            action_type="follow-up",
            delivery_channel="email",
            draft="Subject: Hello",
        )

        response = self._execute(tid)

        self.assertEqual(response.code, 409)
        self.assertEqual(self.started, [])

    def test_execution_runner_receives_the_execution_action_id(self):
        tid = self.make_task()
        self._ready_action(tid)

        response = self._execute(tid)

        self.assertEqual(response.code, 202)
        child = json.loads(response.body)["action"]
        self.assertEqual(self.started[-1][3]["action_id"], child["id"])

    def test_double_submit_creates_only_one_execution(self):
        tid = self.make_task()
        self._ready_action(tid)
        self.assertEqual(self._execute(tid).code, 202)
        self.assertEqual(self._execute(tid).code, 409)

    def test_running_execution_blocks_a_new_preview(self):
        tid = self.make_task()
        self._ready_action(tid)
        self.assertEqual(self._execute(tid).code, 202)

        response = self.fetch(
            f"/api/tasks/{tid}/cowork",
            method="POST",
            body="{}",
            headers={"Content-Type": "application/json"},
        )

        self.assertEqual(response.code, 409)

    def test_requires_api_transport(self):
        from src.handlers import cowork as cowork_handler

        tid = self.make_task()
        self._ready_action(tid)
        cowork_handler.EXECUTE_TRANSPORT_ENABLED_FN = lambda: False
        self.assertEqual(self._execute(tid).code, 409)

    def test_positive_write_evidence_finalises_as_executed(self):
        tid = self.make_task()
        self._ready_action(tid)
        self.assertEqual(self._execute(tid).code, 202)
        payload = {
            "terminal_status": "ok",
            "conversation_id": "tenant:user:conversation",
            "text": "The Teams message was sent.",
            "tool_trace": [
                {
                    "tool_name": "mcp__m365_teams__PostMessage",
                    "ok": True,
                    "duration_seconds": 1.2,
                }
            ],
            "sse_events": [
                {
                    "event": "ts",
                    "tid": "send-1",
                    "tn": "mcp__m365_teams__PostMessage",
                    "inp": json.dumps({
                        "recipient": "sarah@microsoft.com",
                        "message": "Hi Sarah - the deck is attached.",
                    }),
                },
                {
                    "event": "tx",
                    "tid": "send-1",
                    "tn": "mcp__m365_teams__PostMessage",
                    "ok": True,
                },
            ],
            "callback_exchanges": [],
        }
        cr._runs[cr.execution_label(tid)] = {
            "proc": None,
            "thread": None,
            "progress": [],
            "result": {
                "exit_code": 0,
                "stdout": json.dumps(payload),
                "stderr": "",
                "error": None,
                "auth_failed": False,
                "cost_credits": 2.0,
            },
        }

        _, data = self.get_preview(tid)

        self.assertEqual(data["action"]["state"], "executed")
        self.assertIsNotNone(data["action"]["delivery_confirmed_at"])
        self.assertIsNone(data["action"]["error"])

    def test_matching_calendar_write_finalises_as_executed(self):
        reviewed_draft = (
            "**Phil / Rima 1:1**\n"
            "- **When:** Wednesday, August 19, 11:05-11:30 AM ET (25 min)\n"
            "- **Attendee:** Rima Reyes\n"
            "- **Teams meeting:** included\n\n"
            "**Agenda**\n"
            "- Quick 1:1 to sync up"
        )
        calendar_event = {
            "subject": "Phil / Rima 1:1",
            "start": "2026-08-19T11:05:00",
            "end": "2026-08-19T11:30:00",
            "time_zone": "America/New_York",
            "attendees": ["rima@microsoft.com"],
            "is_online_meeting": True,
            "content_type": "html",
            "body": "Quick 1:1 to sync up.",
        }
        tid = self.make_task(
            action_type="schedule-meeting",
            key_people=json.dumps([
                {"name": "Rima Reyes", "email": "rima@microsoft.com"},
            ]),
        )
        self._ready_action(
            tid,
            action_type="schedule-meeting",
            draft=None,
            finding=reviewed_draft,
            delivery_channel=None,
            destination_ref="rima@microsoft.com",
            destination_display="Rima Reyes",
            tool_trace=json.dumps([{
                "tool_name": "mcp__outlook_calendar__CreateEvent",
                "ok": True,
                "input": json.dumps(calendar_event),
            }]),
        )
        self.assertEqual(self._execute(tid).code, 202)
        executed_event = {
            key: value
            for key, value in calendar_event.items()
            if key not in {"body", "content_type"}
        }
        executed_event["body"] = (
            cr._render_calendar_event_body(
                reviewed_draft, calendar_event["subject"]
            )
            + "<br><br><!-- aether-footer -->"
            + cr._AETHER_FOOTERS["calendar"]
        )
        payload = {
            "terminal_status": "ok",
            "conversation_id": "tenant:user:conversation",
            "text": "The calendar event was created.",
            "tool_trace": [{
                "tool_name": "mcp__outlook_calendar__CreateEvent",
                "ok": True,
                "duration_seconds": 1.2,
            }],
            "sse_events": [
                {
                    "event": "ts",
                    "tid": "calendar-1",
                    "tn": "mcp__outlook_calendar__CreateEvent",
                    "inp": json.dumps(calendar_event),
                },
                {
                    "event": "tx",
                    "tid": "calendar-1",
                    "tn": "mcp__outlook_calendar__CreateEvent",
                    "ok": True,
                },
            ],
            "approved_inputs": {
                "calendar-1": executed_event,
            },
            "callback_exchanges": [],
        }
        cr._runs[cr.execution_label(tid)] = {
            "proc": None,
            "thread": None,
            "progress": [],
            "result": {
                "exit_code": 0,
                "stdout": json.dumps(payload),
                "stderr": "",
                "error": None,
                "auth_failed": False,
                "cost_credits": 2.0,
            },
        }

        _, data = self.get_preview(tid)

        self.assertEqual(data["action"]["state"], "executed")
        self.assertIsNotNone(data["action"]["delivery_confirmed_at"])
        self.assertIsNone(data["action"]["error"])

    def test_missing_write_evidence_is_delivery_unconfirmed(self):
        tid = self.make_task()
        self._ready_action(tid)
        self.assertEqual(self._execute(tid).code, 202)
        cr._runs[cr.execution_label(tid)] = {
            "proc": None,
            "thread": None,
            "progress": [],
            "result": {
                "exit_code": 0,
                "stdout": json.dumps(
                    {
                        "terminal_status": "ok",
                        "conversation_id": "tenant:user:conversation",
                        "text": "Done.",
                        "tool_trace": [],
                        "sse_events": [],
                        "callback_exchanges": [],
                    }
                ),
                "stderr": "",
                "error": None,
                "auth_failed": False,
                "cost_credits": 1.0,
            },
        }

        _, data = self.get_preview(tid)

        self.assertEqual(data["action"]["state"], "execute_unconfirmed")
        self.assertIn("check the destination", data["action"]["error"].lower())

    def test_explicit_cancellation_returns_to_ready_without_delivery_warning(self):
        tid = self.make_task()
        self._ready_action(tid)
        self.assertEqual(self._execute(tid).code, 202)
        cr._runs[cr.execution_label(tid)] = {
            "proc": None,
            "thread": None,
            "progress": [],
            "result": {
                "exit_code": 0,
                "stdout": json.dumps({
                    "terminal_status": "ok",
                    "conversation_id": "tenant:user:conversation",
                    "text": (
                        "Cancelled, nothing was sent. The draft is still here "
                        "whenever you want to use it."
                    ),
                    "tool_trace": [],
                    "sse_events": [],
                    "callback_exchanges": [],
                }),
                "stderr": "",
                "error": None,
                "auth_failed": False,
                "cost_credits": 1.0,
            },
        }

        _, data = self.get_preview(tid)

        self.assertEqual(data["action"]["state"], "ready")
        self.assertIsNone(data["action"]["error"])
        self.assertIsNone(data["action"]["delivery_confirmed_at"])

    def test_cancellation_text_cannot_override_successful_write_evidence(self):
        tid = self.make_task()
        self._ready_action(tid)
        self.assertEqual(self._execute(tid).code, 202)
        cr._runs[cr.execution_label(tid)] = {
            "proc": None,
            "thread": None,
            "progress": [],
            "result": {
                "exit_code": 0,
                "stdout": json.dumps({
                    "terminal_status": "ok",
                    "conversation_id": "tenant:user:conversation",
                    "text": "Cancelled, nothing was sent.",
                    "tool_trace": [{
                        "tool_name": "mcp__m365_teams__PostMessage",
                        "ok": True,
                    }],
                    "sse_events": [{
                        "event": "ts",
                        "tid": "send-1",
                        "tn": "mcp__m365_teams__PostMessage",
                        "inp": json.dumps({
                            "recipient": "sarah@microsoft.com",
                            "message": "Hi Sarah - the deck is attached.",
                        }),
                    }, {
                        "event": "tx",
                        "tid": "send-1",
                        "tn": "mcp__m365_teams__PostMessage",
                        "ok": True,
                    }],
                    "callback_exchanges": [],
                }),
                "stderr": "",
                "error": None,
                "auth_failed": False,
                "cost_credits": 1.0,
            },
        }

        _, data = self.get_preview(tid)

        self.assertEqual(data["action"]["state"], "executed")
        self.assertIsNotNone(data["action"]["delivery_confirmed_at"])

    def test_cancellation_text_cannot_hide_failed_write_attempt(self):
        tid = self.make_task()
        self._ready_action(tid)
        self.assertEqual(self._execute(tid).code, 202)
        cr._runs[cr.execution_label(tid)] = {
            "proc": None,
            "thread": None,
            "progress": [],
            "result": {
                "exit_code": 1,
                "stdout": json.dumps({
                    "terminal_status": "fail",
                    "conversation_id": "tenant:user:conversation",
                    "text": "Cancelled, nothing was sent.",
                    "tool_trace": [{
                        "tool_name": "mcp__outlook__SendEmailWithAttachments",
                        "ok": False,
                    }],
                    "sse_events": [],
                    "callback_exchanges": [],
                }),
                "stderr": "send failed",
                "error": "send failed",
                "auth_failed": False,
                "cost_credits": 1.0,
            },
        }

        _, data = self.get_preview(tid)

        self.assertEqual(data["action"]["state"], "execute_unconfirmed")
        self.assertIn("check the destination", data["action"]["error"].lower())
        self.assertIsNone(data["action"]["delivery_confirmed_at"])

    def test_cancel_terminal_status_cannot_hide_failed_write_attempt(self):
        tid = self.make_task()
        self._ready_action(tid)
        self.assertEqual(self._execute(tid).code, 202)
        cr._runs[cr.execution_label(tid)] = {
            "proc": None,
            "thread": None,
            "progress": [],
            "result": {
                "exit_code": 0,
                "stdout": json.dumps({
                    "terminal_status": "cancel",
                    "conversation_id": "tenant:user:conversation",
                    "text": "Cancelled, nothing was sent.",
                    "tool_trace": [{
                        "tool_name": "mcp__outlook__SendEmailWithAttachments",
                        "ok": False,
                    }],
                    "sse_events": [],
                    "callback_exchanges": [],
                }),
                "stderr": "",
                "error": None,
                "auth_failed": False,
                "cost_credits": 1.0,
            },
        }

        _, data = self.get_preview(tid)

        self.assertEqual(data["action"]["state"], "execute_unconfirmed")
        self.assertIn("check the destination", data["action"]["error"].lower())
        self.assertIsNone(data["action"]["delivery_confirmed_at"])


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


# ------------------------------------------------------- preview safety


class TestPreviewBarrier(CoworkAPITestBase):
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


class TestLiveProgress(CoworkAPITestBase):
    """A preview runs for a median of 119s (p90 224s, 93% over 60s) and used to
    show a bare spinner. The CLI streams liveness to stderr the whole time, so
    GET now returns the tail of it and the card can say what is happening."""

    STDERR = (
        "Update available: 1.21.92 -> 1.21.97. Run: cowork update\n"
        "Or: irm https://aka.ms/cowork/ps1 | iex\n"
        "[cowork] streaming - 0:04 elapsed - init: Ready\n"
        "[cowork] streaming - 0:24 elapsed - tool: tool_search_tool\n"
        "[cowork] streaming - 1:22 elapsed - Searching for your training sessions\n"
    )

    def test_get_returns_a_progress_list(self):
        tid = self.make_task()
        self.start(tid, FakeProc(stdout=GOOD_STDOUT, stderr=self.STDERR))
        _, body = self.get_preview(tid)
        self.assertIsInstance(body["action"].get("progress"), list)

    def test_progress_carries_the_cli_status_lines(self):
        tid = self.make_task()
        self.start(tid, FakeProc(stdout=GOOD_STDOUT, stderr=self.STDERR))
        _, body = self.get_preview(tid)
        joined = " ".join(body["action"]["progress"])
        self.assertIn("Searching for your training sessions", joined)
        self.assertIn("Ready", joined)

    def test_raw_tool_names_are_not_shown(self):
        """`tool: mcp__outlook_calendar__FindMeetingTimes` is not something to
        put in front of a user; the CLI's own human copy is."""
        tid = self.make_task()
        self.start(tid, FakeProc(stdout=GOOD_STDOUT, stderr=self.STDERR))
        _, body = self.get_preview(tid)
        joined = " ".join(body["action"]["progress"])
        self.assertNotIn("tool_search_tool", joined)

    def test_noise_is_not_shown_to_the_user(self):
        """The update banner and the iex install hint are not progress."""
        tid = self.make_task()
        self.start(tid, FakeProc(stdout=GOOD_STDOUT, stderr=self.STDERR))
        _, body = self.get_preview(tid)
        joined = " ".join(body["action"]["progress"])
        self.assertNotIn("Update available", joined)
        self.assertNotIn("iex", joined)

    def test_the_cowork_prefix_is_stripped(self):
        """It is an implementation detail of the CLI, not something a user
        should read."""
        tid = self.make_task()
        self.start(tid, FakeProc(stdout=GOOD_STDOUT, stderr=self.STDERR))
        _, body = self.get_preview(tid)
        for line in body["action"]["progress"]:
            self.assertNotIn("[cowork]", line)

    def test_a_run_with_no_progress_still_returns_a_list(self):
        """Callers must never have to guard the key."""
        tid = self.make_task()
        self.start(tid, FakeProc(stdout=GOOD_STDOUT, stderr=""))
        _, body = self.get_preview(tid)
        self.assertEqual(body["action"].get("progress"), [])


class TestHandoffStatus(CoworkAPITestBase):
    """After "Open in Cowork", what happened?

    Handing a draft over is fire-and-forget today. GET /v1/tasks is keyed by the
    SAME composite conversation id our deep link uses, and a read-only probe
    against production (2026-08-10) matched 17 of our 18 stored ids. The state
    worth surfacing is `needs_user_input`: Cowork is blocked waiting on Phil.

    Purely additive - when the lookup returns None the card renders exactly as
    it does today, which is why this ships unflagged.
    """

    def _ready(self):
        tid = self.make_task()
        self.start(tid, FakeProc(stdout=GOOD_STDOUT))
        self.get_preview(tid)
        return tid

    def test_handoff_is_absent_when_the_lookup_returns_nothing(self):
        from src.handlers import cowork as cowork_handler

        cowork_handler.HANDOFF_FN = lambda _cid: None
        tid = self._ready()
        _, body = self.get_preview(tid)
        self.assertNotIn("handoff", body["action"])

    def test_handoff_state_is_surfaced_when_available(self):
        from src.handlers import cowork as cowork_handler

        cowork_handler.HANDOFF_FN = lambda _cid: {
            "state": "needs_user_input", "waiting_on_user": True,
            "last_activity": 1786400663554, "title": "A task",
        }
        tid = self._ready()
        _, body = self.get_preview(tid)
        self.assertEqual(body["action"]["handoff"]["state"], "needs_user_input")
        self.assertTrue(body["action"]["handoff"]["waiting_on_user"])

    def test_a_failing_lookup_cannot_break_the_card(self):
        """Decoration must never be able to fail a preview."""
        from src.handlers import cowork as cowork_handler

        def boom(_cid):
            raise OSError("endpoint down")

        cowork_handler.HANDOFF_FN = boom
        tid = self._ready()
        response, body = self.get_preview(tid)
        self.assertEqual(response.code, 200)
        self.assertNotIn("handoff", body["action"])


class TestStopPreview(CoworkAPITestBase):
    """DELETE stops a run in flight.

    The only call in TodoIQ that reaches Cowork and changes something - and it
    strictly REDUCES what can happen. It stops work; it cannot start or send
    anything. That is why it is safe while there is still no execute route.
    """

    def setUp(self):
        super().setUp()
        from src.handlers import cowork as cowork_handler
        self._cancel = cowork_handler.CANCEL_FN
        self.cancelled = []

        def fake_cancel(cid):
            self.cancelled.append(cid)
            return True

        cowork_handler.CANCEL_FN = fake_cancel
        self.addCleanup(lambda: setattr(cowork_handler, "CANCEL_FN", self._cancel))

    def _running(self):
        """A task with a preview still in flight."""
        tid = self.make_task()
        proc = FakeProc(stdout=GOOD_STDOUT)
        proc.hold = True
        self.start(tid, proc)
        return tid

    def test_delete_on_a_missing_preview_is_404(self):
        tid = self.make_task()
        r = self.fetch(f"/api/tasks/{tid}/cowork", method="DELETE")
        self.assertEqual(r.code, 404)

    def test_delete_on_a_finished_preview_is_409(self):
        tid = self.make_task()
        self.start(tid, FakeProc(stdout=GOOD_STDOUT))
        self.get_preview(tid)
        r = self.fetch(f"/api/tasks/{tid}/cowork", method="DELETE")
        self.assertEqual(r.code, 409)

    def test_no_execute_route_was_introduced(self):
        """DELETE stops work. It must never become a way to send."""
        from src.handlers.cowork import CoworkHandler
        self.assertFalse(hasattr(CoworkHandler, "execute"))
        self.assertFalse(hasattr(CoworkHandler, "send"))


class TestRefineTurn(CoworkAPITestBase):
    """POST /cowork/refine — one more turn on the SAME conversation.

    A Redo starts a brand new Cowork conversation and re-researches M365 from
    zero (27s-6min, 69-355 credits measured). A refine continues the existing
    conversation, which still holds that research.
    """

    def setUp(self):
        super().setUp()
        from src.services import cowork_runner as cr_mod
        self.continued = []

        def fake_continue(task_id, conversation_id, instruction, **kw):
            self.continued.append(
                {
                    "task_id": task_id,
                    "cid": conversation_id,
                    "text": instruction,
                    "interaction_mode": kw.get("interaction_mode"),
                    "action_id": kw.get("action_id"),
                    "schedule_people": kw.get("schedule_people"),
                    "schedule_duration": kw.get("schedule_duration"),
                }
            )
            return cr_mod.preview_label(task_id)

        import src.handlers.cowork as handler_mod
        self._orig = handler_mod.continue_preview
        handler_mod.continue_preview = fake_continue
        self.addCleanup(
            lambda: setattr(handler_mod, "continue_preview", self._orig)
        )

    def _ready(self):
        tid = self.make_task()
        self.start(tid, FakeProc(stdout=GOOD_STDOUT))
        self.get_preview(tid)
        return tid

    def _refine(self, tid, instruction="make it shorter"):
        return self.fetch(
            f"/api/tasks/{tid}/cowork/refine",
            method="POST",
            body=json.dumps({"instruction": instruction}),
            headers={"Content-Type": "application/json"},
        )

    def test_it_continues_the_existing_conversation(self):
        tid = self._ready()
        r = self._refine(tid)
        self.assertEqual(r.code, 202)
        self.assertEqual(len(self.continued), 1)
        self.assertEqual(self.continued[0]["cid"], "conv-abc")
        self.assertIsNotNone(self.continued[0]["action_id"])

    def test_schedule_refine_requires_fresh_selection_with_certification_context(self):
        from src.db import get_connection
        from src.models import get_latest_task_action

        tid = self.make_task(
            title="Schedule a 25-minute review",
            action_type="schedule-meeting",
            key_people=json.dumps([{
                "name": "Rima Reyes",
                "email": "rima@microsoft.com",
            }]),
        )
        self.start(tid, FakeProc(stdout=GOOD_STDOUT))
        self.get_preview(tid)
        conn = get_connection()
        selected = '{"kind":"interaction_answer","question_raw":"{}"}'
        conn.execute(
            "UPDATE task_actions SET conversation_id='conv-schedule',"
            "answered_interaction=? WHERE task_id=?",
            (selected, tid),
        )
        conn.commit()
        conn.close()

        response = self._refine(tid, "start 5 minutes late")

        self.assertEqual(response.code, 202)
        self.assertEqual(
            self.continued[0]["schedule_people"],
            [{"name": "Rima Reyes", "email": "rima@microsoft.com"}],
        )
        self.assertEqual(self.continued[0]["schedule_duration"], 25)
        self.assertIsNotNone(self.continued[0]["action_id"])
        latest = get_latest_task_action(tid)
        self.assertIsNone(latest["answered_interaction"])
        self.assertIn("fresh FindMeetingTimes", latest["composed_prompt"])
        self.assertIn("new exact slot", latest["composed_prompt"])
        self.assertIn(
            "25 minutes",
            latest["composed_prompt"],
        )

    def test_it_creates_a_new_row_linked_to_its_parent(self):
        """The audit chain: the original attempt survives."""
        tid = self._ready()
        before = json.loads(
            self.fetch(f"/api/tasks/{tid}/cowork?history=1").body
        )["actions"]
        self._refine(tid)
        after = json.loads(
            self.fetch(f"/api/tasks/{tid}/cowork?history=1").body
        )["actions"]
        self.assertEqual(len(after), len(before) + 1)
        newest = after[0] if after[0]["id"] > after[-1]["id"] else after[-1]
        self.assertTrue(newest.get("parent_action_id"))

    def test_the_instruction_is_recorded(self):
        tid = self._ready()
        self._refine(tid, "aim it just at Greg")
        self.assertEqual(self.continued[0]["text"], "aim it just at Greg")

    def test_no_interaction_mode_is_not_inherited(self):
        tid = self.make_task()
        self.start(
            tid,
            FakeProc(stdout=GOOD_STDOUT),
            body={"interaction_mode": "no_interaction"},
        )
        self.get_preview(tid)
        response = self._refine(tid)
        self.assertEqual(response.code, 202)
        self.assertEqual(self.continued[0]["interaction_mode"], "interaction")
        newest = json.loads(response.body)["action"]
        self.assertEqual(newest["interaction_mode"], "interaction")

    def test_an_empty_instruction_is_rejected(self):
        tid = self._ready()
        self.assertEqual(self._refine(tid, "   ").code, 400)
        self.assertEqual(self.continued, [])

    def test_a_task_with_no_preview_is_404(self):
        tid = self.make_task()
        self.assertEqual(self._refine(tid).code, 404)

    def test_a_running_preview_is_409(self):
        tid = self.make_task()
        proc = FakeProc(stdout=GOOD_STDOUT)
        self.start(tid, proc)
        self.assertEqual(self._refine(tid).code, 409)

    def test_a_running_execution_is_409(self):
        from src.db import get_connection

        tid = self._ready()
        conn = get_connection()
        conn.execute(
            "UPDATE task_actions SET state='executing' WHERE task_id=?",
            (tid,),
        )
        conn.commit()
        conn.close()

        self.assertEqual(self._refine(tid).code, 409)

    def test_no_execute_route_was_introduced(self):
        """`delete` is a Tornado base method, so only our own names are checked."""
        from src.handlers.cowork import CoworkRefineHandler
        own = set(vars(CoworkRefineHandler))
        self.assertEqual(own & {"execute", "send", "deliver"}, set())
        self.assertIn("post", own)


class TestEnrichExecutingState(CoworkAPITestBase):
    def setUp(self):
        super().setUp()
        from src.handlers import cowork as handler_mod

        self.handler = handler_mod
        self._store = handler_mod.BLOCKED_QUESTION_STORE_FN
        self.store_calls = []
        handler_mod.HANDOFF_FN = lambda _cid: {
            "state": "needs_user_input",
            "waiting_on_user": True,
        }
        handler_mod.BLOCKED_QUESTION_FN = lambda _cid: {
            "invocation_id": "aq-execution",
            "questions": [{
                "id": "0",
                "producer_id": "approval",
                "header": "",
                "question": "Send it?",
                "options": [],
            }],
        }
        handler_mod.BLOCKED_QUESTION_STORE_FN = (
            lambda *args: self.store_calls.append(args) or True
        )
        self.addCleanup(
            lambda: setattr(
                handler_mod, "BLOCKED_QUESTION_STORE_FN", self._store
            )
        )

    def test_executing_ignores_stale_waiting_handoff_and_question(self):
        action = {
            "id": 9001,
            "state": "executing",
            "conversation_id": "tenant:user:shared",
            "destination_kind": "none",
            "blocked_question": None,
        }
        enriched = self.handler._enrich(action)
        self.assertIs(enriched["waiting_on_user"], False)
        self.assertIsNone(enriched["interaction_request"])
        self.assertEqual(self.store_calls, [])

    def test_executing_skips_answered_question_recovery(self):
        action = {
            "id": 9002,
            "state": "executing",
            "conversation_id": "tenant:user:shared",
            "destination_kind": "none",
            "blocked_question": "",
        }
        enriched = self.handler._enrich(action)
        self.assertIs(enriched["waiting_on_user"], False)
        self.assertEqual(self.store_calls, [])

    def test_previewing_still_surfaces_a_real_question(self):
        action = {
            "id": 9003,
            "state": "previewing",
            "conversation_id": "tenant:user:preview",
            "destination_kind": "none",
            "blocked_question": None,
        }
        enriched = self.handler._enrich(action)
        self.assertIs(enriched["waiting_on_user"], True)
        self.assertEqual(
            enriched["interaction_request"]["invocation_id"], "aq-execution"
        )
        self.assertEqual(len(self.store_calls), 1)


class TestInteractionAnswer(CoworkAPITestBase):
    def setUp(self):
        super().setUp()
        from src.handlers import cowork as handler_mod
        self.handler = handler_mod
        self._answer = handler_mod.ANSWER_FN
        self.answers = []
        handler_mod.HANDOFF_FN = lambda cid: {
            "state": "needs_user_input",
            "waiting_on_user": True,
        }
        handler_mod.BLOCKED_QUESTION_FN = lambda cid: "Use account A or B?"
        handler_mod.ANSWER_FN = lambda cid, invocation_id, answers: (
            self.answers.append((cid, invocation_id, answers)) or True
        )
        self.addCleanup(lambda: setattr(handler_mod, "ANSWER_FN", self._answer))

    def _blocked(self, state="previewing"):
        from src.db import get_connection
        from src.models import create_task_action

        tid = self.make_task()
        action = create_task_action(
            tid,
            action_type="follow-up",
            conversation_id="t:u:blocked",
        )
        conn = get_connection()
        conn.execute(
            "UPDATE task_actions SET blocked_question=?, state=? WHERE id=?",
            (json.dumps({
                "invocation_id": "invoke-1",
                "questions": [{
                    "id": "0", "producer_id": "account", "header": "",
                    "question": "Use account A or B?", "options": [],
                }],
            }), state, action["id"]),
        )
        conn.commit()
        conn.close()
        return tid

    def _answer_request(self, tid, answers=None):
        return self.fetch(
            f"/api/tasks/{tid}/cowork/answer",
            method="POST",
            body=json.dumps({
                "invocation_id": "invoke-1",
                "answers": answers or {"0": "Use A"},
            }),
            headers={"Content-Type": "application/json"},
        )

    def _blocked_schedule(self):
        from src.db import get_connection

        tid = self._blocked()
        conn = get_connection()
        conn.execute(
            "UPDATE tasks SET title='Schedule a 25-minute review',"
            "action_type='schedule-meeting',key_people=? WHERE id=?",
            (
                json.dumps([{
                    "name": "Jay Padimiti",
                    "email": "jay.padimiti@microsoft.com",
                }]),
                tid,
            ),
        )
        conn.execute(
            "UPDATE task_actions SET action_type='schedule-meeting' WHERE task_id=?",
            (tid,),
        )
        interaction = {
            "invocation_id": "invoke-1",
            "questions": [{
                "id": "0",
                "producer_id": "slot",
                "header": "Pick a time",
                "question": "Which time works?",
                "options": [{
                    "value": "Wed 8/19, 1:05 PM ET",
                    "label": "Wed 8/19, 1:05 PM ET",
                    "description": (
                        '[avail:{"jay.padimiti@microsoft.com":"free"}]'
                    ),
                    "image_url": "",
                }],
                "multi_select": False,
                "image_url": "",
            }],
        }
        conn.execute(
            "UPDATE task_actions SET blocked_question=? WHERE task_id=?",
            (json.dumps(interaction), tid),
        )
        conn.commit()
        conn.close()
        return tid

    def test_it_answers_the_same_live_conversation(self):
        tid = self._blocked()
        response = self._answer_request(tid)
        self.assertEqual(response.code, 202)
        self.assertEqual(
            self.answers,
            [("t:u:blocked", "invoke-1", {"0": "Use A"})],
        )
        body = json.loads(response.body)
        self.assertFalse(body["action"]["waiting_on_user"])
        self.assertEqual(body["action"]["blocked_question"], "")

    def test_it_answers_an_execution_interaction_in_the_same_conversation(self):
        tid = self._blocked(state="executing")

        response = self._answer_request(tid)

        self.assertEqual(response.code, 202)
        self.assertEqual(
            self.answers,
            [("t:u:blocked", "invoke-1", {"0": "Use A"})],
        )
        body = json.loads(response.body)
        self.assertEqual(body["action"]["state"], "executing")
        self.assertFalse(body["action"]["waiting_on_user"])

    def test_schedule_slot_without_current_evidence_is_rejected(self):
        tid = self._blocked_schedule()

        visible = json.loads(
            self.fetch(f"/api/tasks/{tid}/cowork").body
        )["action"]["interaction_request"]
        self.assertEqual(visible["questions"][0]["options"], [])
        self.assertIn(
            "could not verify suitable working-hours slots",
            visible["questions"][0]["question"],
        )

        response = self._answer_request(
            tid, {"0": "Wed 8/19, 1:05 PM ET"}
        )

        self.assertEqual(response.code, 409)
        self.assertEqual(self.answers, [])

    def test_schedule_free_text_correction_remains_allowed(self):
        tid = self._blocked_schedule()

        response = self._answer_request(
            tid, {"0": "Check Jay's timezone and availability again"}
        )

        self.assertEqual(response.code, 202)
        self.assertIn(
            "Re-run FindMeetingTimes",
            self.answers[-1][2]["0"],
        )
        self.assertIn(
            "Check Jay's timezone and availability again",
            self.answers[-1][2]["0"],
        )

    def test_schedule_answer_persists_the_selected_certified_slot(self):
        from src.db import get_connection
        from src.models import get_latest_task_action

        tid = self._blocked_schedule()
        values = (
            "Thu 8/20, 1:05 PM ET",
            "Fri 8/21, 10:05 AM ET",
            "Mon 8/24, 2:05 PM ET",
        )
        slots = [
            {
                "value": value,
                "start": start,
                "end": end,
                "timezone": "Eastern Standard Time",
                "availability": {"jay.padimiti@microsoft.com": "free"},
            }
            for value, start, end in (
                (
                    values[0],
                    "2099-08-20T13:05:00-04:00",
                    "2099-08-20T13:30:00-04:00",
                ),
                (
                    values[1],
                    "2099-08-21T10:05:00-04:00",
                    "2099-08-21T10:30:00-04:00",
                ),
                (
                    values[2],
                    "2099-08-24T14:05:00-04:00",
                    "2099-08-24T14:30:00-04:00",
                ),
            )
        ]
        interaction = {
            "invocation_id": "invoke-1",
            "questions": [{
                "id": "0",
                "producer_id": "slot",
                "header": "Pick a time",
                "question": "Which time works?",
                "multi_select": False,
                "image_url": "",
                "options": [
                    {
                        "value": value,
                        "label": value,
                        "description": "",
                        "image_url": "",
                    }
                    for value in values
                ],
            }],
            "schedule_evidence": {
                "valid": True,
                "source": "FindMeetingTimes+interaction",
                "query_backed": True,
                "attendees": ["jay.padimiti@microsoft.com"],
                "duration_minutes": 25,
                "slots": slots,
            },
        }
        conn = get_connection()
        action = get_latest_task_action(tid)
        conn.execute(
            "UPDATE task_actions SET blocked_question=? WHERE id=?",
            (json.dumps(interaction), action["id"]),
        )
        conn.commit()
        conn.close()

        response = self._answer_request(tid, {"0": values[0]})

        self.assertEqual(response.code, 202)
        stored = get_latest_task_action(tid)
        record = json.loads(stored["answered_interaction"])
        self.assertEqual(record["answers"], {"0": values[0]})
        self.assertEqual(
            record["interaction"]["schedule_evidence"]["slots"][0]["start"],
            "2099-08-20T13:05:00-04:00",
        )

    def test_schedule_slot_is_rejected_after_attendee_change(self):
        from src.db import get_connection

        tid = self._blocked_schedule()
        conn = get_connection()
        row = conn.execute(
            "SELECT id,blocked_question FROM task_actions WHERE task_id=?", (tid,)
        ).fetchone()
        interaction = json.loads(row["blocked_question"])
        interaction["schedule_evidence"] = {
            "valid": True,
            "source": "FindMeetingTimes+interaction",
            "attendees": ["jay.padimiti@microsoft.com"],
            "query_backed": True,
        }
        conn.execute(
            "UPDATE task_actions SET blocked_question=? WHERE id=?",
            (json.dumps(interaction), row["id"]),
        )
        conn.execute(
            "UPDATE tasks SET key_people=? WHERE id=?",
            (
                json.dumps([{
                    "name": "Adele Vance",
                    "email": "adele.vance@microsoft.com",
                }]),
                tid,
            ),
        )
        conn.commit()
        conn.close()

        response = self._answer_request(
            tid, {"0": "Wed 8/19, 1:05 PM ET"}
        )

        self.assertEqual(response.code, 409)
        self.assertEqual(self.answers, [])

    def test_schedule_slot_is_rejected_after_duration_change(self):
        from src.db import get_connection
        from src.models import get_latest_task_action

        tid = self._blocked_schedule()
        action = get_latest_task_action(tid)
        interaction = json.loads(action["blocked_question"])
        selected = "Thu 8/20, 1:05 PM ET"
        interaction["questions"][0]["options"] = [{
            "value": selected,
            "label": selected,
            "description": "",
        }]
        interaction["schedule_evidence"] = {
            "valid": True,
            "source": "FindMeetingTimes+interaction",
            "query_backed": True,
            "attendees": ["jay.padimiti@microsoft.com"],
            "duration_minutes": 25,
            "slots": [{
                "value": selected,
                "start": "2099-08-20T13:05:00-04:00",
                "end": "2099-08-20T13:30:00-04:00",
                "timezone": "Eastern Standard Time",
                "availability": {"jay.padimiti@microsoft.com": "free"},
            }],
        }
        conn = get_connection()
        conn.execute(
            "UPDATE task_actions SET blocked_question=? WHERE id=?",
            (json.dumps(interaction), action["id"]),
        )
        conn.execute(
            "UPDATE tasks SET title='Schedule a 45-minute review' WHERE id=?",
            (tid,),
        )
        conn.commit()
        conn.close()

        response = self._answer_request(tid, {"0": selected})

        self.assertEqual(response.code, 409)
        self.assertEqual(self.answers, [])

    def test_empty_answer_is_rejected(self):
        tid = self._blocked()
        self.assertEqual(self._answer_request(tid, {"0": "  "}).code, 400)
        self.assertEqual(self.answers, [])

    def test_stale_invocation_is_rejected(self):
        tid = self._blocked()
        response = self.fetch(
            f"/api/tasks/{tid}/cowork/answer",
            method="POST",
            body=json.dumps({
                "invocation_id": "invoke-old",
                "answers": {"0": "Use A"},
            }),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(response.code, 409)
        self.assertEqual(self.answers, [])

    def test_api_failure_keeps_the_answer_claimed_when_state_is_ambiguous(self):
        tid = self._blocked()
        self.handler.ANSWER_FN = lambda *_: (_ for _ in ()).throw(
            RuntimeError("upstream unavailable")
        )

        response = self._answer_request(tid)

        self.assertEqual(response.code, 502)
        from src.models import get_latest_task_action

        action = get_latest_task_action(tid)
        self.assertEqual(action["blocked_question"], "")
        self.assertIsNotNone(action["answered_interaction"])

    def test_definitive_api_rejection_restores_the_question_for_retry(self):
        tid = self._blocked()
        from src.services.cowork_runner import CoworkAnswerRejected

        self.handler.ANSWER_FN = lambda *_: (_ for _ in ()).throw(
            CoworkAnswerRejected(403)
        )

        response = self._answer_request(tid)

        self.assertEqual(response.code, 403)
        from src.models import get_latest_task_action

        action = get_latest_task_action(tid)
        self.assertNotEqual(action["blocked_question"], "")
        self.assertIsNone(action["answered_interaction"])

    def test_concurrent_stop_state_wins_over_stale_answer_response(self):
        tid = self._blocked()
        from src.models import get_latest_task_action, update_task_action

        def stop_then_accept(*_):
            action = get_latest_task_action(tid)
            update_task_action(
                action["id"],
                frozenset({"state", "error"}),
                state="failed",
                error="Stopped by user.",
            )
            return True

        self.handler.ANSWER_FN = stop_then_accept
        response = self._answer_request(tid)

        self.assertEqual(response.code, 202)
        self.assertEqual(json.loads(response.body)["action"]["state"], "failed")

    def test_api_failure_does_not_overwrite_a_newer_question(self):
        tid = self._blocked()
        from src.models import get_latest_task_action, set_blocked_question_if_missing

        newer = json.dumps({
            "invocation_id": "invoke-2",
            "questions": [{
                "id": "0", "producer_id": "next", "header": "",
                "question": "A newer question?", "options": [],
            }],
        })

        def fail_after_new_question(*_):
            action = get_latest_task_action(tid)
            set_blocked_question_if_missing(action["id"], newer)
            raise RuntimeError("response timed out")

        self.handler.ANSWER_FN = fail_after_new_question
        response = self._answer_request(tid)

        self.assertEqual(response.code, 502)
        follow_up = self.fetch(f"/api/tasks/{tid}/cowork")
        interaction = json.loads(follow_up.body)["action"]["interaction_request"]
        self.assertEqual(interaction["invocation_id"], "invoke-2")

    def test_ambiguous_api_failure_reconciles_the_upstream_question(self):
        tid = self._blocked()
        self.handler.BLOCKED_QUESTION_FN = lambda *_: {
            "invocation_id": "invoke-2",
            "questions": [{
                "id": "0", "producer_id": "next", "header": "",
                "question": "A newer question?", "options": [],
            }],
        }
        self.handler.ANSWER_FN = lambda *_: (_ for _ in ()).throw(
            TimeoutError("response timed out")
        )

        response = self._answer_request(tid)

        self.assertEqual(response.code, 502)
        follow_up = self.fetch(f"/api/tasks/{tid}/cowork")
        interaction = json.loads(follow_up.body)["action"]["interaction_request"]
        self.assertEqual(interaction["invocation_id"], "invoke-2")

    def test_partial_answer_map_is_rejected(self):
        tid = self._blocked()
        from src.db import get_connection

        conn = get_connection()
        row = conn.execute(
            "SELECT id, blocked_question FROM task_actions WHERE task_id=?",
            (tid,),
        ).fetchone()
        interaction = json.loads(row["blocked_question"])
        interaction["questions"].append({
            "id": "1", "producer_id": "reason", "header": "",
            "question": "Why?", "options": [],
        })
        conn.execute(
            "UPDATE task_actions SET blocked_question=? WHERE id=?",
            (json.dumps(interaction), row["id"]),
        )
        conn.commit()
        conn.close()

        self.assertEqual(self._answer_request(tid, {"0": "Use A"}).code, 400)
        self.assertEqual(self.answers, [])

    def test_it_refuses_when_cowork_is_not_waiting(self):
        tid = self._blocked()
        self.handler.HANDOFF_FN = lambda cid: {
            "state": "running", "waiting_on_user": False,
        }
        self.assertEqual(self._answer_request(tid).code, 409)
        self.assertEqual(self.answers, [])
