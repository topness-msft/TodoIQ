"""Cancellation — the capability the subprocess path does not have.

Proven live on 2026-08-10 against the real runtime:

    POST /v1/conversations/{conversation_id}/pause  {"mode": "hard"}
    -> 200 {"success": true, "pendingLlmCalls": 0, "pendingToolCalls": 0}
    -> `rl st=cancel` on the SSE stream 0.9s later
    -> run fully stopped 3.0s after the request

Contrast with what we had:

    cowork_cli library   close_live() did NOT halt the turn — still running at
                         50s. That is why the library migration was closed.
    subprocess           proc.kill() kills OUR process; the server-side run
                         keeps going and keeps spending credits.

So this is the first time a TodoIQ user can actually stop work they started.

The route is POST (not DELETE) and is a *pause*, per
aether_runtime/src/orchestrator/api/v1/control.py:
  soft = finish the current turn, hard = interrupt now, cancelling in-flight
  LLM and tool calls.
"""

import unittest
from unittest import mock

from src.services import cowork_runner as cr
from src.services.cowork_runner import (
    _api_payload_from_events,
    _iter_sse,
    parse_cowork_output,
)


CANCELLED_SSE = [
    "event: rl",
    'data: {"st":"started"}',
    "",
    "event: dx",
    'data: {"t":"Weekly team meetings work best when"}',
    "",
    "event: rl",
    'data: {"st":"cancel"}',
]


class TestCancelIsTerminal(unittest.TestCase):
    """A cancelled run must END, not hang until the 660s timeout."""

    def test_cancel_is_a_terminal_run_state(self):
        self.assertIn("cancel", cr._TERMINAL_RUN_STATES)

    def test_ok_is_not_a_terminal_conversation_state(self):
        self.assertNotIn("ok", cr._TERMINAL_RUN_STATES)
        self.assertIn("ok", cr._TURN_COMPLETE_RUN_STATES)

    def test_fail_is_a_terminal_run_state(self):
        self.assertIn("fail", cr._TERMINAL_RUN_STATES)

    def test_terminal_status_reports_cancelled(self):
        payload = _api_payload_from_events(
            list(_iter_sse(CANCELLED_SSE)), "t:u:cw-1",
        )
        self.assertEqual(payload["terminal_status"], "cancel")

    def test_partial_text_is_kept(self):
        """Whatever Cowork produced before the stop is still worth showing."""
        payload = _api_payload_from_events(
            list(_iter_sse(CANCELLED_SSE)), "t:u:cw-1",
        )
        self.assertIn("Weekly team meetings", payload["text"])


class TestCancelIsNotAFailure(unittest.TestCase):
    """Stopping something on purpose is not an error to apologise for."""

    def setUp(self):
        import json

        payload = _api_payload_from_events(
            list(_iter_sse(CANCELLED_SSE)), "t:u:cw-1",
        )
        self.parsed = parse_cowork_output(json.dumps(payload), "")

    def test_cancelled_run_is_flagged_as_cancelled(self):
        self.assertTrue(self.parsed["cancelled"])

    def test_cancelled_run_does_not_read_as_a_crash(self):
        self.assertNotIn("failed", (self.parsed["error"] or "").lower())

    def test_a_normal_run_is_not_flagged_as_cancelled(self):
        import json

        payload = _api_payload_from_events(
            list(_iter_sse(["event: rl", 'data: {"st":"ok"}'])), "t:u:c",
        )
        self.assertFalse(parse_cowork_output(json.dumps(payload), "")["cancelled"])


class TestCancelRun(unittest.TestCase):
    """cancel_run() posts the pause and reports whether it was accepted."""

    def test_posts_hard_pause_to_the_conversation(self):
        calls = {}

        def fake_post(path, body):
            calls["path"] = path
            calls["body"] = body
            return mock.Mock(status_code=200,
                             json=mock.Mock(return_value={"success": True}))

        ok = cr.cancel_run("t:u:cw-9", _post=fake_post)
        self.assertTrue(ok)
        self.assertEqual(calls["path"], "/v1/conversations/t:u:cw-9/pause")
        self.assertEqual(calls["body"]["mode"], "hard")

    def test_carries_a_reason_for_the_audit_trail(self):
        calls = {}

        def fake_post(path, body):
            calls["body"] = body
            return mock.Mock(status_code=200,
                             json=mock.Mock(return_value={"success": True}))

        cr.cancel_run("t:u:cw-9", _post=fake_post)
        self.assertTrue(calls["body"].get("reason"))

    def test_a_blank_conversation_id_does_not_call_the_network(self):
        called = []

        def spy(path, body):
            called.append(path)

        self.assertFalse(cr.cancel_run("", _post=spy))
        self.assertEqual(called, [])

    def test_a_failed_post_reports_false_rather_than_raising(self):
        def boom(path, body):
            raise OSError("island unreachable")

        self.assertFalse(cr.cancel_run("t:u:cw-9", _post=boom))

    def test_a_non_200_reports_false(self):
        def denied(path, body):
            return mock.Mock(status_code=404,
                             json=mock.Mock(return_value={}))

        self.assertFalse(cr.cancel_run("t:u:cw-9", _post=denied))

    def test_success_false_in_the_body_reports_false(self):
        """200 is not the same as "it stopped"."""
        def refused(path, body):
            return mock.Mock(status_code=200,
                             json=mock.Mock(return_value={"success": False}))

        self.assertFalse(cr.cancel_run("t:u:cw-9", _post=refused))


if __name__ == "__main__":
    unittest.main()
