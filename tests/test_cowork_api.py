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

    def test_no_interaction_mode_is_persisted_and_composed(self):
        tid = self.make_task()
        response = self.start(tid, body={"interaction_mode": "no_interaction"})
        action = json.loads(response.body)["action"]
        self.assertEqual(action["interaction_mode"], "no_interaction")
        self.assertIn("[INTERACTION]", action["composed_prompt"])

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

    def test_new_run_inherits_previous_mode_when_body_omits_it(self):
        tid = self.make_task()
        self.start(tid, body={"interaction_mode": "no_interaction"})
        response = self.start(tid, body={"redirect_text": "make it shorter"})
        action = json.loads(response.body)["action"]
        self.assertEqual(action["interaction_mode"], "no_interaction")

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

    def test_no_interaction_mode_is_inherited(self):
        tid = self.make_task()
        self.start(
            tid,
            FakeProc(stdout=GOOD_STDOUT),
            body={"interaction_mode": "no_interaction"},
        )
        self.get_preview(tid)
        response = self._refine(tid)
        self.assertEqual(response.code, 202)
        self.assertEqual(self.continued[0]["interaction_mode"], "no_interaction")
        newest = json.loads(response.body)["action"]
        self.assertEqual(newest["interaction_mode"], "no_interaction")

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

    def test_no_execute_route_was_introduced(self):
        """`delete` is a Tornado base method, so only our own names are checked."""
        from src.handlers.cowork import CoworkRefineHandler
        own = set(vars(CoworkRefineHandler))
        self.assertEqual(own & {"execute", "send", "deliver"}, set())
        self.assertIn("post", own)


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

    def _blocked(self):
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
            "UPDATE task_actions SET blocked_question=? WHERE id=?",
            (json.dumps({
                "invocation_id": "invoke-1",
                "questions": [{
                    "id": "0", "producer_id": "account", "header": "",
                    "question": "Use account A or B?", "options": [],
                }],
            }), action["id"]),
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
