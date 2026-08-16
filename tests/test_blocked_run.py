"""A run blocked on the user must say so, not spin.

Phil: "2132 is 11 mins into its cowork call". It was not slow and it was not
hung. GET /v1/tasks reported state=needs_user_input with lastActivity 7 minutes
earlier: Cowork had asked him something in the web app and was waiting. It would
never have finished on its own.

We already detect this. _WAITING_STATES is {"needs_user_input"} and
handoff_status returns waiting_on_user, but the badge is only read on a
finished card. While the run is still 'previewing' the card shows a spinner and
progress text ("Working on your request"), which is actively misleading: it says
work is happening when nothing is.

So the preview payload has to carry the same signal while previewing, which is
the one state where it actually changes what the user should do.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.handlers import cowork as handler  # noqa: E402


class BlockedRunTest(unittest.TestCase):
    def setUp(self):
        self._orig = handler.HANDOFF_FN
        self._question = handler.BLOCKED_QUESTION_FN
        self._store = handler.BLOCKED_QUESTION_STORE_FN
        self._update = handler.update_task_action
        self._clear = handler.clear_blocked_question_if_unchanged
        handler.BLOCKED_QUESTION_FN = lambda cid: None
        handler.BLOCKED_QUESTION_STORE_FN = lambda action_id, question: {
            "id": action_id, "blocked_question": question,
        }
        self.addCleanup(self._restore)

    def _restore(self):
        handler.HANDOFF_FN = self._orig
        handler.BLOCKED_QUESTION_FN = self._question
        handler.BLOCKED_QUESTION_STORE_FN = self._store
        handler.update_task_action = self._update
        handler.clear_blocked_question_if_unchanged = self._clear

    def _enrich(self, state, handoff, conversation_id="t:u:abc"):
        handler.HANDOFF_FN = lambda cid: handoff
        return handler._enrich({
            "id": 1, "task_id": 2, "state": state,
            "conversation_id": conversation_id,
        })

    def test_a_previewing_run_blocked_on_the_user_says_so(self):
        out = self._enrich("previewing", {
            "state": "needs_user_input", "waiting_on_user": True,
            "last_activity": None, "title": "",
        })
        self.assertTrue(out.get("waiting_on_user"))

    def test_a_previewing_run_that_is_really_working_does_not(self):
        out = self._enrich("previewing", {
            "state": "running", "waiting_on_user": False,
            "last_activity": None, "title": "",
        })
        self.assertFalse(out.get("waiting_on_user"))

    def test_no_conversation_id_means_no_claim_either_way(self):
        """Nothing to look up, so we must not assert it is fine."""
        out = self._enrich("previewing", None, conversation_id="")
        self.assertFalse(out.get("waiting_on_user"))

    def test_an_unreadable_handoff_does_not_break_the_card(self):
        def boom(cid):
            raise RuntimeError("throttled")

        handler.HANDOFF_FN = boom
        out = handler._enrich({
            "id": 1, "task_id": 2, "state": "previewing",
            "conversation_id": "t:u:abc",
        })
        self.assertFalse(out.get("waiting_on_user"))

    def test_a_blocked_run_recovers_and_persists_the_question_once(self):
        calls = []
        interaction = {
            "invocation_id": "invoke-1",
            "questions": [{
                "id": "account", "header": "",
                "question": "Which account should I use?", "options": [],
            }],
        }
        handler.BLOCKED_QUESTION_FN = lambda cid: interaction

        def store(action_id, question):
            calls.append((action_id, question))
            return {
                "id": action_id, "task_id": 2, "state": "previewing",
                "conversation_id": "t:u:abc", "blocked_question": question,
            }

        handler.BLOCKED_QUESTION_STORE_FN = store
        out = self._enrich("previewing", {
            "state": "needs_user_input", "waiting_on_user": True,
        })
        self.assertEqual(out["interaction_request"], interaction)
        self.assertEqual(json.loads(calls[0][1]), interaction)

    def test_a_persisted_question_is_not_replayed_again(self):
        handler.BLOCKED_QUESTION_FN = lambda cid: self.fail("unexpected replay")
        out = handler._enrich({
            "id": 1, "task_id": 2, "state": "previewing",
            "conversation_id": "t:u:abc",
            "blocked_question": json.dumps({
                "invocation_id": "stored", "questions": [],
            }),
        })
        self.assertEqual(out["interaction_request"]["invocation_id"], "stored")

    def test_an_executing_run_surfaces_a_persisted_question(self):
        interaction = {
            "invocation_id": "execution-question",
            "questions": [{
                "id": "0",
                "question": "Use the earlier draft or cancel?",
                "options": [],
            }],
        }

        out = handler._enrich({
            "id": 1,
            "task_id": 2,
            "state": "executing",
            "conversation_id": "t:u:abc",
            "blocked_question": json.dumps(interaction),
        })

        self.assertTrue(out["waiting_on_user"])
        self.assertEqual(out["interaction_request"], interaction)

    def test_an_executing_run_without_a_persisted_question_stays_running(self):
        out = self._enrich("executing", {
            "state": "needs_user_input",
            "waiting_on_user": True,
        })

        self.assertFalse(out["waiting_on_user"])
        self.assertIsNone(out["interaction_request"])

    def test_question_replay_failure_does_not_break_the_card(self):
        def boom(cid):
            raise RuntimeError("replay unavailable")

        handler.BLOCKED_QUESTION_FN = boom
        out = self._enrich("previewing", {
            "state": "needs_user_input", "waiting_on_user": True,
        })
        self.assertTrue(out["waiting_on_user"])
        self.assertIsNone(out.get("blocked_question"))

    def test_answered_sentinel_clears_after_the_run_resumes(self):
        calls = []
        handler.HANDOFF_FN = lambda cid: {
            "state": "running", "waiting_on_user": False,
        }

        def clear(action_id, blocked_question, answered_interaction):
            calls.append((blocked_question, answered_interaction))
            return True

        handler.clear_blocked_question_if_unchanged = clear
        out = handler._enrich({
            "id": 1, "task_id": 2, "state": "previewing",
            "conversation_id": "t:u:abc", "blocked_question": "",
        })
        self.assertIsNone(out["blocked_question"])
        self.assertEqual(calls, [("", None)])

    def test_answered_sentinel_suppresses_stale_waiting_status(self):
        handler.HANDOFF_FN = lambda cid: {
            "state": "needs_user_input", "waiting_on_user": True,
        }
        out = handler._enrich({
            "id": 1, "task_id": 2, "state": "previewing",
            "conversation_id": "t:u:abc", "blocked_question": "",
        })
        self.assertFalse(out["waiting_on_user"])

    def test_external_answer_clears_the_persisted_question_after_resume(self):
        calls = []
        handler.HANDOFF_FN = lambda cid: {
            "state": "running", "waiting_on_user": False,
        }

        def clear(action_id, blocked_question, answered_interaction):
            calls.append((blocked_question, answered_interaction))
            return True

        handler.clear_blocked_question_if_unchanged = clear
        out = handler._enrich({
            "id": 1, "task_id": 2, "state": "previewing",
            "conversation_id": "t:u:abc",
            "blocked_question": "Old question?",
        })
        self.assertIsNone(out["blocked_question"])
        self.assertEqual(calls, [("Old question?", None)])


if __name__ == "__main__":
    unittest.main()
