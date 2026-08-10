"""Canonical tool names, harvested from sse_events the CLI already sends us.

THE GAP THIS CLOSES
-------------------
`_barrier_verdict` decides whether a write ran without interception. To do that
it must recognise a write tool, and `tool_trace` makes that hard: G1d recorded an
intercepted Teams post as the display label "Post message", which appears in none
of the 154 canonical names in that probe's config. So `_looks_like_write` fell
back to verb heuristics ("send", "post", "create"...), which is a guess.

The same run's `sse_events` carries the real name. From the G1b capture:

    tool_trace: {"tool_name": "Send email with attachments", ...}
    sse_events: {"event": "ts", "tn": "mcp__outlook__SendEmailWithAttachments", ...}

Same call, two names. The `ts` (TOOL_START) event has the canonical one, and we
were already receiving it and discarding it.

That upgrades write detection from a heuristic to an exact denylist match, and
it costs nothing: no extra call, no transport change, just reading a key of the
JSON we already parse.

The heuristic stays as a fallback, because `sse_events` may be absent (older
payloads, or a run that died before emitting) and because a genuinely new write
tool would be missing from our vendored list either way.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services.cowork_runner import parse_cowork_output  # noqa: E402

FIXTURE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "fixtures", "g1b-stdout.json"
)


def _stdout(**over):
    payload = {
        "terminal_status": "ok",
        "text": "DRAFT:\nhi\nEND DRAFT",
        "tool_trace": [],
        "sse_events": [],
        "callback_exchanges": [],
    }
    payload.update(over)
    return json.dumps(payload)


class TestCanonicalToolsExposed(unittest.TestCase):
    def test_parse_returns_a_canonical_tool_list(self):
        r = parse_cowork_output(_stdout())
        self.assertIn("tools", r)

    def test_tools_is_always_a_list(self):
        for raw in ("", "junk", _stdout()):
            self.assertIsInstance(parse_cowork_output(raw)["tools"], list)

    def test_a_tool_start_event_yields_its_canonical_name(self):
        r = parse_cowork_output(
            _stdout(sse_events=[
                {"event": "ts", "tid": "a", "tn": "mcp__outlook__SendEmailWithAttachments"},
            ])
        )
        self.assertEqual(
            [t["name"] for t in r["tools"]],
            ["mcp__outlook__SendEmailWithAttachments"],
        )

    def test_exec_events_supply_duration_and_ok(self):
        r = parse_cowork_output(
            _stdout(sse_events=[
                {"event": "ts", "tid": "a", "tn": "tool_search_tool"},
                {"event": "tx", "tid": "a", "tn": "tool_search_tool", "dur": 290, "ok": True},
            ])
        )
        self.assertEqual(len(r["tools"]), 1)
        self.assertEqual(r["tools"][0]["duration_ms"], 290)
        self.assertIs(r["tools"][0]["ok"], True)

    def test_a_start_without_an_exec_is_still_reported(self):
        """A run killed mid-tool still tells us the call was attempted, which is
        exactly the case where we most want to know."""
        r = parse_cowork_output(
            _stdout(sse_events=[{"event": "ts", "tid": "a", "tn": "mcp__teams__PostMessage"}])
        )
        self.assertEqual(len(r["tools"]), 1)
        self.assertIsNone(r["tools"][0]["ok"])

    def test_non_tool_events_are_ignored(self):
        r = parse_cowork_output(
            _stdout(sse_events=[
                {"event": "th", "c": "thinking out loud"},
                {"event": "rl", "st": "ok"},
                {"event": "session"},
            ])
        )
        self.assertEqual(r["tools"], [])

    def test_malformed_events_do_not_break_the_parse(self):
        r = parse_cowork_output(
            _stdout(sse_events=["not a dict", {"event": "ts"}, None, {"tn": "x"}])
        )
        self.assertIsInstance(r["tools"], list)


class TestCanonicalNamesSharpenTheBarrier(unittest.TestCase):
    """The reason this is worth doing at all.

    Canonical names from ``sse_events`` are what let the verdict tell an
    intercepted write apart from an unrequested one. After the 2026-08-10
    accuracy fix the interesting distinction is no longer "is it a write" but
    "did we ask to block THIS tool", and that question can only be answered
    against a canonical name.
    """

    def test_a_display_label_resolves_to_the_tool_we_asked_to_block(self):
        """"Post message" appears in no config entry, but space-insensitive
        matching resolves it to the canonical PostMessage we did denylist."""
        r = parse_cowork_output(
            _stdout(tool_trace=[{"tool_name": "Post message"}])
        )
        self.assertEqual(r["barrier"]["status"], "held_unconfirmed")

    def test_a_canonical_write_name_is_caught_even_with_no_write_verb(self):
        """The heuristic keys on verbs. "Compose something" contains none, so
        the display label alone would slip past — the canonical name from
        sse_events is what identifies it as a write at all."""
        r = parse_cowork_output(
            _stdout(
                tool_trace=[{"tool_name": "Compose something"}],
                sse_events=[{"event": "ts", "tid": "a",
                             "tn": "mcp__outlook__SendEmailWithAttachments"}],
            )
        )
        self.assertEqual(r["barrier"]["status"], "held_unconfirmed")

    def test_a_canonical_name_outside_the_denylist_is_a_breach(self):
        """The payoff: an unrequested write is now distinguishable, and that is
        the shape the 2026-08-10 released-tool spike produced."""
        r = parse_cowork_output(
            _stdout(
                tool_trace=[{"tool_name": "Compose something"}],
                sse_events=[{"event": "ts", "tid": "a",
                             "tn": "mcp__someapp__SendThing"}],
            )
        )
        self.assertEqual(r["barrier"]["status"], "BREACHED")
        self.assertIn("SendThing", r["barrier"]["reason"])

    def test_interception_still_reads_as_held(self):
        r = parse_cowork_output(
            _stdout(
                text="> BLOCKED: TodoIQ preview mode intercepted this call. Nothing sent.",
                sse_events=[{"event": "ts", "tid": "a",
                             "tn": "mcp__outlook__SendEmailWithAttachments"}],
            )
        )
        self.assertEqual(r["barrier"]["status"], "held")

    def test_a_read_only_canonical_name_does_not_trip_it(self):
        r = parse_cowork_output(
            _stdout(sse_events=[{"event": "ts", "tid": "a", "tn": "tool_search_tool"}])
        )
        self.assertEqual(r["barrier"]["status"], "not_exercised")


class TestAgainstTheRealCapture(unittest.TestCase):
    """G1b again: a real run where Graph confirmed nothing was sent."""

    def setUp(self):
        with open(FIXTURE, encoding="utf-8") as fh:
            self.raw = fh.read()

    def test_canonical_names_are_recovered_from_the_real_run(self):
        tools = parse_cowork_output(self.raw)["tools"]
        names = [t["name"] for t in tools]
        self.assertIn("mcp__outlook__SendEmailWithAttachments", names)
        self.assertIn("tool_search_tool", names)

    def test_the_canonical_name_differs_from_the_display_label(self):
        """The whole premise. If these ever converge upstream, this test says so
        and the heuristic fallback can be reconsidered."""
        parsed = parse_cowork_output(self.raw)
        display = [t["tool_name"] for t in parsed["tool_trace"]]
        canonical = [t["name"] for t in parsed["tools"]]
        self.assertIn("Send email with attachments", display)
        self.assertNotIn("Send email with attachments", canonical)

    def test_durations_survive_the_round_trip(self):
        tools = parse_cowork_output(self.raw)["tools"]
        send = [t for t in tools if "SendEmail" in t["name"]][0]
        self.assertEqual(send["duration_ms"], 403)

    def test_the_real_run_still_reads_as_held(self):
        """Non-negotiable: this capture must never read as a breach."""
        r = parse_cowork_output(self.raw)
        self.assertEqual(r["barrier"]["status"], "held")
        self.assertIsNone(r["error"])


if __name__ == "__main__":
    unittest.main()
