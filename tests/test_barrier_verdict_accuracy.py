"""Phase 1: the barrier verdict must be trustworthy before we change transports.

Measured on live data (2026-08-10): 12 of 18 real production rows reported
BREACHED, and every one was a false positive. A canary that cries wolf two times
in three is one the user learns to ignore, which is strictly worse than no
canary — and it is the instrument we would use to judge a NEW transport as safe.

Two independent causes, both covered here:

1. ``Bash`` is on the denylist (denied for containment, so a run cannot shell out
   to bypass the barrier) but it is NOT an M365 write. It never gets intercepted,
   so it tripped "write tool ran with no sign of interception" on every run.

2. The literal marker match is unreliable because the model PARAPHRASES. A real
   captured interception read:

       "I wasn't able to send that - the send was blocked before anything went
        out. ... Nothing was sent or saved."

   which is a perfect interception report that shares almost no substring with
   ``_BLOCK_MARKER``.

The redesign separates two questions that were previously conflated:

    did we ASK for this tool to be blocked?   (structural, certain)
    did we OBSERVE the block?                 (evidential, best-effort)

A write we never asked to block is the genuinely dangerous case — that is the
2026-08-10 spike where releasing one tool from ``tool_names`` removed the barrier
entirely with no approval gate in its place. The old code could not distinguish
that from a routine ``Bash`` call.
"""

import unittest

from src.services.cowork_runner import _BLOCK_MARKER, _barrier_verdict


def _t(name, ok=True):
    return {"tool_name": name, "ok": ok, "duration_seconds": 0.1}


class TestContainerToolsAreNotM365Writes(unittest.TestCase):
    """Cause 1: Bash is denied for containment, not because it mutates M365."""

    def test_bash_alone_does_not_report_breached(self):
        verdict = _barrier_verdict([_t("Bash")], [], "All done.")
        self.assertNotEqual(verdict["status"], "BREACHED")

    def test_bash_alone_reads_as_not_exercised(self):
        verdict = _barrier_verdict([_t("Bash")], [], "All done.")
        self.assertEqual(verdict["status"], "not_exercised")

    def test_bash_does_not_mask_a_real_unblocked_write(self):
        """A container tool must never suppress a genuine finding."""
        verdict = _barrier_verdict(
            [_t("Bash"), _t("SomeUnknownSendTool")], [], "Sent it.",
        )
        self.assertEqual(verdict["status"], "BREACHED")


class TestParaphrasedInterceptionCountsAsHeld(unittest.TestCase):
    """Cause 2: the model restates the canned result instead of quoting it."""

    PARAPHRASE = (
        "I wasn't able to send that - the send was blocked before anything "
        "went out. Here's the draft as it stood:\n\nNothing was sent or saved."
    )

    def test_literal_marker_still_counts(self):
        verdict = _barrier_verdict(
            [], [], _BLOCK_MARKER,
            tools=[{"name": "mcp__outlook__SendEmailWithAttachments"}],
        )
        self.assertEqual(verdict["status"], "held")

    def test_paraphrased_block_is_not_reported_as_breached(self):
        verdict = _barrier_verdict(
            [], [], self.PARAPHRASE,
            tools=[{"name": "mcp__outlook__SendEmailWithAttachments"}],
        )
        self.assertNotEqual(verdict["status"], "BREACHED")


class TestUnrequestedWriteIsTheRealBreach(unittest.TestCase):
    """The case the old code could not see, and the only one worth alarming on.

    On 2026-08-10 a spike removed ``outlook-SendEmailWithAttachments`` from
    ``tool_names``. The tool then ran with no barrier and no approval gate. That
    is a breach of our intent even if nothing happened to be delivered.
    """

    def test_write_tool_absent_from_the_denylist_reports_breached(self):
        verdict = _barrier_verdict(
            [], [], "Sent.", tools=[{"name": "mcp__someapp__SendThing"}],
        )
        self.assertEqual(verdict["status"], "BREACHED")

    def test_denylisted_write_without_confirmation_is_not_a_breach(self):
        """We asked for interception and the config was sent; absence of a
        quotable marker is missing evidence, not evidence of failure."""
        verdict = _barrier_verdict(
            [], [], "Here is a draft.",
            tools=[{"name": "mcp__outlook__SendEmailWithAttachments"}],
        )
        self.assertEqual(verdict["status"], "held_unconfirmed")


class TestLiveProductionRowsAreClean(unittest.TestCase):
    """Regression guard built from the real shapes that produced false alarms."""

    REAL_TRACES = [
        [_t("tool_search_tool"), _t("Search M365"), _t("Bash"),
         _t("Find meeting times"), _t("List chat messages")],
        [_t("Bash")],
        [_t("Get chat message"), _t("Bash"), _t("Find meeting times")],
    ]

    def test_no_read_only_run_reports_breached(self):
        for trace in self.REAL_TRACES:
            with self.subTest(trace=[t["tool_name"] for t in trace]):
                self.assertNotEqual(
                    _barrier_verdict(trace, [], "Here are the findings.")["status"],
                    "BREACHED",
                )


if __name__ == "__main__":
    unittest.main()
