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

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.handlers import cowork as handler  # noqa: E402


class BlockedRunTest(unittest.TestCase):
    def setUp(self):
        self._orig = handler.HANDOFF_FN
        self.addCleanup(self._restore)

    def _restore(self):
        handler.HANDOFF_FN = self._orig

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


if __name__ == "__main__":
    unittest.main()
