"""Live progress on the API transport must say what Cowork is DOING.

Found while watching a real run (task 2183). After 4 minutes the card had 25
progress lines and 22 of them were the identical string "Connecting MCP
servers". Nothing about the actual work.

Cause: `_api_progress` only read `tk` events, which are the container-init task
card. The CLI maps many more event types (cowork_cli/services/send_progress.py
:102-162), and the one that carries the human sentence is `ps`:

    ps   {"msg": "Searching your Teams and calendar"}   <- the readable one
    th                                                   thinking
    dx   text delta                                      writing
    fr                                                   finalizing
    tk   {"items":[{"af": "Connecting MCP servers"}]}    init phases

So the subprocess path showed "Searching your Teams and calendar" while the API
path showed container plumbing on a loop. That is a regression in the one thing
Phase 1 was built for: a run takes a median of 119s, and the card has to say
something true while the user waits.

Consecutive duplicates are also suppressed. A repeated line is not progress,
and 22 copies of one string pushed everything informative out of a ring that
only keeps the tail.
"""

import unittest

from src.services.cowork_runner import _api_progress_text


class TestTheHumanMessageIsPreferred(unittest.TestCase):
    def test_ps_carries_the_readable_sentence(self):
        self.assertEqual(
            _api_progress_text("ps", {"msg": "Searching your Teams and calendar"}),
            "Searching your Teams and calendar",
        )

    def test_ps_is_trimmed_and_bounded(self):
        got = _api_progress_text("ps", {"msg": "  " + "x" * 200 + "  "})
        self.assertTrue(got.startswith("x"))
        self.assertLessEqual(len(got), 80)

    def test_an_empty_ps_is_ignored(self):
        self.assertIsNone(_api_progress_text("ps", {"msg": "   "}))


class TestPhaseEventsAreSurfaced(unittest.TestCase):
    def test_thinking(self):
        self.assertEqual(_api_progress_text("th", {}), "Thinking")

    def test_writing(self):
        self.assertEqual(_api_progress_text("dx", {"t": "abc"}), "Writing the reply")

    def test_finalizing(self):
        self.assertEqual(_api_progress_text("fr", {}), "Finalizing")

    def test_init_task_card_still_reports(self):
        got = _api_progress_text(
            "tk", {"items": [{"af": "Connecting MCP servers"}]}
        )
        self.assertEqual(got, "Connecting MCP servers")


class TestDeveloperNoiseIsNotShown(unittest.TestCase):
    """Same discipline as _progress_text on the subprocess path: raw tool names
    are developer-facing, and the runtime emits its own human copy alongside."""

    def test_tool_start_is_not_progress(self):
        self.assertIsNone(
            _api_progress_text("ts", {"tn": "mcp__m365_search__SearchM365"})
        )

    def test_tool_end_is_not_progress(self):
        self.assertIsNone(_api_progress_text("tx", {"tn": "x", "ok": True}))

    def test_unknown_events_are_ignored(self):
        self.assertIsNone(_api_progress_text("zz", {"whatever": 1}))

    def test_malformed_payloads_do_not_raise(self):
        for bad in (None, [], "nope", {"items": "not a list"}, {"items": [None]}):
            with self.subTest(bad=bad):
                self.assertIsNone(_api_progress_text("tk", bad))


class TestConsecutiveDuplicatesAreSuppressed(unittest.TestCase):
    """22 copies of one string is not progress, and it evicts the useful tail."""

    def setUp(self):
        from src.services import cowork_runner as cr

        cr.reset_registry()
        self.cr = cr
        self.label = "test:progress"
        cr._runs[self.label] = {
            "proc": None, "thread": None, "result": None,
            "progress": __import__("collections").deque(maxlen=200),
        }
        self.addCleanup(cr.reset_registry)

    def test_a_repeated_line_is_recorded_once(self):
        for _ in range(5):
            self.cr._append_progress(self.label, "Connecting MCP servers")
        self.assertEqual(self.cr.get_progress(self.label),
                         ["Connecting MCP servers"])

    def test_a_changed_line_is_recorded(self):
        self.cr._append_progress(self.label, "Connecting MCP servers")
        self.cr._append_progress(self.label, "Searching your Teams and calendar")
        self.assertEqual(len(self.cr.get_progress(self.label)), 2)

    def test_a_line_may_recur_after_something_else(self):
        """Only CONSECUTIVE duplicates are noise; a genuine return to a phase
        is real information."""
        self.cr._append_progress(self.label, "Thinking")
        self.cr._append_progress(self.label, "Writing the reply")
        self.cr._append_progress(self.label, "Thinking")
        self.assertEqual(len(self.cr.get_progress(self.label)), 3)


if __name__ == "__main__":
    unittest.main()
