"""The block message must not outlive the turn that produced it.

Phil, in the Cowork web app, on a conversation TodoIQ started:

    "I couldn't send these. This task is in preview mode, so the invites were
     blocked before anything was created."

"preview mode" is our string, not his and not Cowork's. It comes from
_BLOCK_MESSAGE, which the barrier feeds back as the tool result whenever a
write is intercepted.

Evidence it is the message and not the config:

* task 2132 action 83 shows 4x mcp__outlook_calendar__CreateEvent spoofed, so
  the block text entered that conversation's history;
* task 2269 (Freada) was cancelled before any write was attempted, so no block
  text ever entered ITS history - and finishing THAT one in the web app worked.

So the config is not poisoning the conversation. Our own words are. _SAFETY is
carefully turn-scoped ("This scopes the current turn only. It is not a standing
restriction"), but _BLOCK_MESSAGE said "Do not retry, and do not attempt
another tool to achieve the same effect" with no scope at all. A model reading
its own history reasonably treats that as permanent, which breaks the entire
premise that the user finishes the job in Cowork.

Scoping the text does NOT weaken the barrier. The barrier is the
toolCallbackConfig we send on every request we make; a retry inside the same
turn is spoofed regardless of what this text says. The text only narrates.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.services import cowork_runner as cr  # noqa: E402


class BlockMessageScopeTest(unittest.TestCase):
    def test_the_marker_sentence_is_unchanged(self):
        """_BLOCK_MARKER is derived from it and is matched against captures."""
        self.assertTrue(
            cr._BLOCK_MESSAGE.startswith(
                "BLOCKED: TodoIQ preview mode intercepted this call."
            )
        )
        self.assertEqual(
            cr._BLOCK_MARKER, "BLOCKED: TodoIQ preview mode intercepted this call."
        )

    def test_it_still_says_nothing_happened(self):
        self.assertIn("Nothing was sent", cr._BLOCK_MESSAGE)

    def test_it_no_longer_reads_as_a_standing_prohibition(self):
        text = cr._BLOCK_MESSAGE.lower()
        self.assertNotIn("do not retry, and do not attempt another tool", text)

    def test_it_scopes_itself_to_this_turn(self):
        text = cr._BLOCK_MESSAGE.lower()
        self.assertIn("this turn", text)

    def test_it_says_a_later_request_is_not_covered(self):
        """The user finishing the job in Cowork must not be refused."""
        text = cr._BLOCK_MESSAGE.lower()
        self.assertIn("not a standing restriction", text)

    def test_it_still_tells_the_model_what_to_do_now(self):
        self.assertIn("draft", cr._BLOCK_MESSAGE.lower())

    def test_the_config_still_carries_the_message_for_every_tool(self):
        """The real barrier is unchanged: every denylisted name is spoofed."""
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = cr.build_callback_config("scopetest", log_dir=Path(tmp))
            config = json.loads(path.read_text(encoding="utf-8"))

        self.assertTrue(config["tool_names"])
        self.assertEqual(
            set(config["static_results"].values()), {cr._BLOCK_MESSAGE}
        )
        # The config uses the dash spelling plus the bare name; the runtime
        # reports mcp__outlook_calendar__CreateEvent. Both are covered, and
        # Layer 1 of the runtime is deny-by-default regardless of the list.
        self.assertIn("outlook_calendar-CreateEvent", config["tool_names"])
        self.assertIn("CreateEvent", config["tool_names"])


if __name__ == "__main__":
    unittest.main()
