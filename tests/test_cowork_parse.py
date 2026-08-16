"""Tests for parse_cowork_output.

The primary fixture is the **real 21KB stdout** from the Phase 0 spike against task
#2076 -- not a mock. Mocking this payload would defeat the purpose: the whole risk
is that Cowork's real output does not match what we imagined it would look like.

Observed shape of ``text``:

    <narration>
    ## Step 1 - Check result
    <findings prose>
    ## Step 2 - Draft nudge (not sent)
    > the draft, as a markdown blockquote
    Want me to send it, or tweak the tone first?

A critical lesson from the G1/G1b probes is encoded here: ``tool_trace[].ok`` is
True whether the tool executed or was intercepted, so it must never be treated as
evidence about whether a write occurred.
"""

import unittest
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from src.services.cowork_runner import parse_cowork_output

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def load(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return fh.read()


class TestRealSpikePayload(unittest.TestCase):
    """Against the genuine 21KB CLI output for task #2076."""

    @classmethod
    def setUpClass(cls):
        cls.raw = load("spike-2076-stdout.json")
        cls.res = parse_cowork_output(cls.raw)

    def test_no_error(self):
        self.assertIsNone(self.res["error"])

    def test_terminal_status(self):
        self.assertEqual(self.res["terminal_status"], "ok")

    def test_duration_captured(self):
        self.assertAlmostEqual(self.res["duration_seconds"], 42.297, places=2)

    def test_conversation_id_captured(self):
        self.assertTrue(self.res["conversation_id"])

    def test_draft_extracted_from_blockquote(self):
        draft = self.res["draft"]
        self.assertIsNotNone(draft)
        self.assertTrue(draft.startswith("Hey Brandon"))
        self.assertIn("PPCC exec panel", draft)

    def test_draft_has_quote_markers_stripped(self):
        self.assertNotIn(">", self.res["draft"][:2])
        for line in self.res["draft"].splitlines():
            self.assertFalse(line.lstrip().startswith(">"))

    def test_draft_preserves_em_dashes(self):
        # Real draft contains U+2014; losing it would signal an encoding bug.
        self.assertIn("\u2014", self.res["draft"])

    def test_finding_holds_the_research(self):
        finding = self.res["finding"]
        self.assertIn("Brandon has not responded", finding)

    def test_finding_excludes_the_draft(self):
        # The draft is rendered in its own editable control; duplicating it in the
        # findings pane would let the two drift apart after an edit.
        self.assertNotIn("Hey Brandon", self.res["finding"])

    def test_trailing_offer_to_send_removed(self):
        # "Want me to send it?" is an artefact of the chat framing and is answered
        # by the UI's own approve control.
        self.assertNotIn("Want me to send it", self.res["finding"])
        self.assertNotIn("Want me to send it", self.res["draft"])

    def test_tool_trace_simplified(self):
        trace = self.res["tool_trace"]
        self.assertEqual(len(trace), 3)
        self.assertEqual(trace[0]["tool_name"], "tool_search_tool")
        self.assertIn("List chat messages", [t["tool_name"] for t in trace])

    def test_tool_trace_is_json_serialisable(self):
        # Stored in a TEXT column.
        json.loads(json.dumps(self.res["tool_trace"]))


class TestG1bPayload(unittest.TestCase):
    """The interception probe: a different shape, must not crash."""

    def test_parses_without_error(self):
        res = parse_cowork_output(load("g1b-stdout.json"))
        self.assertIsNone(res["error"])
        self.assertEqual(res["terminal_status"], "ok")

    def test_tool_ok_flag_is_not_evidence_of_execution(self):
        # G1b: the send tool reports ok=True even though it was intercepted and
        # nothing was sent. Preserved verbatim, but the parser must never derive a
        # "was it sent" conclusion from it.
        res = parse_cowork_output(load("g1b-stdout.json"))
        self.assertNotIn("sent", res)
        self.assertNotIn("wrote", res)
        self.assertNotIn("executed", res)


class TestFailureModes(unittest.TestCase):

    def test_empty_stdout_is_an_error(self):
        # Exactly what an expired auth token produces.
        res = parse_cowork_output("")
        self.assertIsNotNone(res["error"])
        self.assertIsNone(res["draft"])

    def test_whitespace_stdout_is_an_error(self):
        self.assertIsNotNone(parse_cowork_output("   \n ")["error"])

    def test_invalid_json_is_an_error_not_a_crash(self):
        res = parse_cowork_output("this is not json at all")
        self.assertIsNotNone(res["error"])

    def test_truncated_json_is_an_error(self):
        self.assertIsNotNone(parse_cowork_output('{"text": "abc"')["error"])

    def test_auth_failure_detected_from_stderr(self):
        res = parse_cowork_output(
            "", stderr="Not authenticated. Run: cowork auth login"
        )
        self.assertIsNotNone(res["error"])
        self.assertIn("auth", res["error"].lower())

    def test_non_ok_terminal_status_surfaced(self):
        res = parse_cowork_output(json.dumps(
            {"text": "partial", "terminal_status": "error", "tool_trace": []}
        ))
        self.assertEqual(res["terminal_status"], "error")
        self.assertIsNotNone(res["error"])

    def test_result_shape_stable_on_every_failure(self):
        # "barrier" carries the write-barrier verdict and is present on every
        # path, including failures, so a caller never has to guess whether the
        # key exists before reading it. "cancelled" is guaranteed for the same
        # reason: the card decides between "you stopped this" and "this failed"
        # by reading it, and a missing key would read as a failure.
        expected = {"terminal_status", "duration_seconds", "conversation_id",
                    "finding", "draft", "tool_trace", "tools", "barrier",
                    "error", "raw_text", "cancelled"}
        for bad in ("", "junk", '{"text":', json.dumps({"text": "x"})):
            with self.subTest(bad=bad[:20]):
                self.assertEqual(set(parse_cowork_output(bad)), expected)


class TestDraftExtraction(unittest.TestCase):

    def _text(self, body):
        return json.dumps({"text": body, "terminal_status": "ok", "tool_trace": []})

    def test_no_blockquote_means_no_draft(self):
        # Never invent a draft: an empty editor is honest, a hallucinated one is not.
        res = parse_cowork_output(self._text("I looked but found nothing to say."))
        self.assertIsNone(res["draft"])
        self.assertIn("found nothing", res["finding"])

    def test_longest_blockquote_wins(self):
        body = (
            "Context:\n\n> a short quote from them\n\n"
            "Here is the draft:\n\n"
            "> Hi there, this is the actual\n> draft message I propose sending.\n"
        )
        draft = parse_cowork_output(self._text(body))["draft"]
        self.assertIn("actual", draft)
        self.assertNotIn("short quote", draft)

    def test_draft_cue_beats_length(self):
        # Cowork routinely quotes the original message while researching. When that
        # quote is LONGER than the proposed reply, picking by length alone hands the
        # user someone else's words to send back to them.
        body = (
            "Here is what Brandon originally wrote:\n\n"
            "> Hey Phil, following up on the PPCC exec panel work. We talked about\n"
            "> pulling a target list together and I wanted to see whether the FY26\n"
            "> numbers would be a sensible starting point, or whether we should be\n"
            "> waiting for Stephanie to come back to us with registration data.\n\n"
            "Here's the draft reply:\n\n"
            "> Sounds good, let's use FY26.\n"
        )
        draft = parse_cowork_output(self._text(body))["draft"]
        self.assertEqual(draft, "Sounds good, let's use FY26.")

    def test_draft_cue_recognised_after_nudge_wording(self):
        body = (
            "Long context quote:\n\n"
            "> " + ("filler words repeated many times " * 8) + "\n\n"
            "Here's a short Teams nudge you can drop into the chat:\n\n"
            "> Circling back on this one.\n"
        )
        self.assertEqual(parse_cowork_output(self._text(body))["draft"],
                         "Circling back on this one.")

    def test_falls_back_to_longest_without_a_cue(self):
        body = (
            "> short one\n\n"
            "> a considerably longer block of text that reads like a message\n"
        )
        draft = parse_cowork_output(self._text(body))["draft"]
        self.assertIn("considerably longer", draft)

    def test_multiline_blockquote_joined(self):
        body = "Draft:\n\n> line one\n> line two\n"
        self.assertEqual(parse_cowork_output(self._text(body))["draft"],
                         "line one\nline two")

    def test_blank_quoted_line_kept_as_paragraph_break(self):
        body = "Draft:\n\n> para one\n>\n> para two\n"
        self.assertEqual(parse_cowork_output(self._text(body))["draft"],
                         "para one\n\npara two")

    def test_fenced_code_block_preferred_when_present(self):
        body = "Draft below.\n\n```\nSubject: Hello\n\nBody text here.\n```\n"
        draft = parse_cowork_output(self._text(body))["draft"]
        self.assertIn("Subject: Hello", draft)
        self.assertNotIn("```", draft)

    def test_structured_markdown_email_is_extracted_from_live_response_shape(self):
        body = (
            "**Findings**\n\n- Verified context.\n\n---\n\n"
            "**Draft email (not sent)**\n\n"
            "**To:** phil@topness.com\n"
            "**Subject:** Thanks for joining the workshop\n\n"
            "Hi Phil,\n\nThanks for joining us.\n\nPhil\n\n"
            "Say the word and I'll send it, or adjust the tone first.\n\n---"
        )
        result = parse_cowork_output(self._text(body))

        self.assertEqual(
            result["draft"],
            "Subject: Thanks for joining the workshop\n\n"
            "Hi Phil,\n\nThanks for joining us.\n\nPhil",
        )
        self.assertIn("Verified context", result["finding"])
        self.assertIn("Draft recipient: phil@topness.com", result["finding"])
        self.assertNotIn("Draft email", result["finding"])
        self.assertNotIn("**To:**", result["finding"])
        self.assertNotIn("**Subject:**", result["finding"])

    def test_h2_structured_email_is_extracted_from_live_response_shape(self):
        body = (
            "## Findings\n\nVerified the original email.\n\n---\n\n"
            "## Draft reply (not sent)\n\n"
            "To: phil@topness.com\n"
            "Subject: RE: What is Kickstarter\n\n"
            "Hi Phil,\n\nHere is the overview.\n\nPhil\n\n---"
        )

        result = parse_cowork_output(self._text(body))

        self.assertEqual(
            result["draft"],
            "Subject: RE: What is Kickstarter\n\n"
            "Hi Phil,\n\nHere is the overview.\n\nPhil",
        )
        self.assertIn("Verified the original email", result["finding"])
        self.assertIn("Draft recipient: phil@topness.com", result["finding"])
        self.assertNotIn("Draft reply", result["finding"])
        self.assertNotIn("To: phil@topness.com", result["finding"])

    def test_incidental_subject_prose_is_not_treated_as_an_email_draft(self):
        body = (
            "The subject came up in the meeting.\n\n"
            "**Subject:** This is a finding, not a draft."
        )
        result = parse_cowork_output(self._text(body))

        self.assertIsNone(result["draft"])
        self.assertIn("This is a finding", result["finding"])

    def test_structured_email_beats_a_quoted_source_message(self):
        body = (
            "Original note:\n\n> This was the source message.\n\n"
            "**Draft email (not sent)**\n\n"
            "**To:** phil@topness.com\n"
            "**Subject:** Follow-up\n\n"
            "Hi Phil,\n\nHere is the reply.\n\nPhil\n\n---"
        )
        result = parse_cowork_output(self._text(body))

        self.assertTrue(result["draft"].startswith("Subject: Follow-up"))
        self.assertIn("source message", result["finding"])

    def test_empty_text_yields_no_draft(self):
        res = parse_cowork_output(self._text(""))
        self.assertIsNone(res["draft"])

    def test_raw_text_always_preserved(self):
        # The unabridged reply is kept so a parsing miss is recoverable rather than
        # silently lossy.
        body = "Some reply with no structure"
        self.assertEqual(parse_cowork_output(self._text(body))["raw_text"], body)


if __name__ == "__main__":
    unittest.main()
