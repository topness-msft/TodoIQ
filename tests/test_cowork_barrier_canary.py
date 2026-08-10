"""The write barrier is conditional. Detect when it silently stops working.

Reading the Aether server source on 2026-08-10 changed what we can honestly
claim. `tool_callback_config` is not a product safety feature - it is an
eval-harness mechanism, and it is tenant-gated:

    # aether_runtime/src/orchestrator/api/v1/tool_callback.py
    def _check_tenant_allowed(tenant_id: str) -> None:
        if tenant_id not in EVAL_ALLOWED_TENANTS:
            raise HTTPException(status_code=404, detail="Not found")

    # aether_runtime/src/orchestrator/domain/eval/auth.py
    EVAL_ALLOWED_TENANTS = SYNTHETIC_EVAL_TENANTS | frozenset({
        "72f988bf-86f1-41af-91ab-2d7cd011db47",  # Microsoft (dogfood)
        ...
    })

Our barrier holds because Phil signs in on `72f988bf`, which is on that list.
Upstream issue #18550 documents the same gate dropping the config on the MSA
consumer path, after which 25 approval-gated write tools ran for real.

The failure is SILENT. No error is raised; the config stops being honoured and
the tool executes. A run that looks entirely normal can have sent real mail.

WHAT THE SIGNAL ACTUALLY IS
---------------------------
The first version of these tests assumed `callback_exchanges` records
interceptions. **It does not.** In the G1b fixture - the probe that PROVED
interception works, with Graph confirming nothing was sent - that array is
empty. Three candidate signals are all useless, and the first two were already
documented as such in cowork_runner:

    callback_exchanges  empty on a confirmed interception
    tool_trace[].ok     True in G1 (really sent) and G1b (blocked)
    tool_trace[].output "" in both

What separates them is that `static_results` feeds the tool a canned string,
which the agent then quotes back. From the real G1b text:

    > BLOCKED: TodoIQ preview mode intercepted this call...
    So in this case, the tool-callback interception **did** prevent the write

So the marker in the agent text is the observable evidence of interception, and
its ABSENCE alongside a write tool is the breach signature.

KNOWN LIMIT, stated rather than hidden: this is per-RUN, not per-call. If two
writes occur and only one is intercepted, the marker is still present and the
run reads as held. Tightening that needs a CLI that populates
`callback_exchanges`, which is why that array is still honoured when present.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services.cowork_runner import parse_cowork_output  # noqa: E402

BLOCK_MARKER = "BLOCKED: TodoIQ preview mode intercepted this call."


def _stdout(**over):
    payload = {
        "terminal_status": "ok",
        "duration_seconds": 12.0,
        "conversation_id": "t:u:cw-1",
        "text": "DRAFT:\nhello\nEND DRAFT",
        "tool_trace": [],
        "callback_exchanges": [],
    }
    payload.update(over)
    return json.dumps(payload)


def _blocked_text():
    return (
        "I invoked the email send tool. Its return value, quoted verbatim:\n\n"
        f"> {BLOCK_MARKER} The email was NOT sent.\n\n"
        "DRAFT:\nhello\nEND DRAFT"
    )


class TestVerdictShape(unittest.TestCase):
    def test_parse_reports_a_barrier_verdict(self):
        self.assertIn("barrier", parse_cowork_output(_stdout(), ""))

    def test_verdict_has_status_and_reason(self):
        b = parse_cowork_output(_stdout(), "")["barrier"]
        self.assertIn(b["status"], {"held", "not_exercised", "BREACHED"})
        self.assertTrue(b["reason"])

    def test_unparseable_output_still_carries_a_verdict(self):
        r = parse_cowork_output("not json", "")
        self.assertEqual(r["barrier"]["status"], "not_exercised")


class TestNotExercised(unittest.TestCase):
    """No write attempted proves nothing either way. Say so; don't imply safety."""

    def test_no_tools_at_all(self):
        r = parse_cowork_output(_stdout(), "")
        self.assertEqual(r["barrier"]["status"], "not_exercised")

    def test_read_only_tools(self):
        r = parse_cowork_output(
            _stdout(tool_trace=[{"tool_name": "tool_search_tool", "ok": True}]), ""
        )
        self.assertEqual(r["barrier"]["status"], "not_exercised")

    def test_not_exercised_never_sets_an_error(self):
        self.assertIsNone(parse_cowork_output(_stdout(), "")["error"])


class TestHeld(unittest.TestCase):
    def test_write_plus_block_marker_is_held(self):
        r = parse_cowork_output(
            _stdout(
                text=_blocked_text(),
                tool_trace=[{"tool_name": "Send email with attachments", "ok": True}],
            ),
            "",
        )
        self.assertEqual(r["barrier"]["status"], "held")

    def test_held_does_not_set_an_error(self):
        r = parse_cowork_output(
            _stdout(
                text=_blocked_text(),
                tool_trace=[{"tool_name": "Send email with attachments", "ok": True}],
            ),
            "",
        )
        self.assertIsNone(r["error"])

    def test_callback_exchanges_alone_also_counts_as_held(self):
        """A future CLI may populate this. Honour it when it is there."""
        r = parse_cowork_output(
            _stdout(
                tool_trace=[{"tool_name": "outlook-SendEmail", "ok": True}],
                callback_exchanges=[{"tool_name": "outlook-SendEmail"}],
            ),
            "",
        )
        self.assertEqual(r["barrier"]["status"], "held")


class TestBreached(unittest.TestCase):
    """The case that matters: a write ran and nothing intercepted it."""

    def test_write_with_no_marker_is_breached(self):
        r = parse_cowork_output(
            _stdout(tool_trace=[{"tool_name": "Send email with attachments"}]), ""
        )
        self.assertEqual(r["barrier"]["status"], "BREACHED")

    def test_breach_names_the_tool(self):
        r = parse_cowork_output(
            _stdout(tool_trace=[{"tool_name": "outlook-SendEmailWithAttachments"}]), ""
        )
        self.assertIn("outlook-SendEmailWithAttachments", r["barrier"]["reason"])

    def test_breach_sets_an_error_so_it_cannot_present_as_success(self):
        r = parse_cowork_output(
            _stdout(tool_trace=[{"tool_name": "outlook-SendEmailWithAttachments"}]), ""
        )
        self.assertTrue(r["error"])

    def test_display_labels_are_caught_not_just_canonical_names(self):
        """G1d logged an intercepted Teams post as "Post message" - a label in
        none of the 154 config entries. Matching only the denylist would miss
        precisely the calls that matter."""
        r = parse_cowork_output(_stdout(tool_trace=[{"tool_name": "Post message"}]), "")
        self.assertEqual(r["barrier"]["status"], "BREACHED")

    def test_blank_terminal_status_does_not_short_circuit_the_check(self):
        """Upstream #10925: a blank terminal_status can accompany a COMPLETED
        write. Our own G-series saw exactly that."""
        r = parse_cowork_output(
            _stdout(
                terminal_status="",
                tool_trace=[{"tool_name": "outlook-SendEmailWithAttachments"}],
            ),
            "",
        )
        self.assertEqual(r["barrier"]["status"], "BREACHED")

    def test_a_refusal_is_not_an_interception(self):
        """The agent declining is not the barrier working. Only the canned
        marker proves static_results fired."""
        r = parse_cowork_output(
            _stdout(
                text="I won't send that email.",
                tool_trace=[{"tool_name": "outlook-SendEmail"}],
            ),
            "",
        )
        self.assertEqual(r["barrier"]["status"], "BREACHED")


class TestAgainstTheRealG1bCapture(unittest.TestCase):
    """The load-bearing tests. G1b is a real captured run where Graph confirmed
    the email was NOT sent. Any detector that flags it is wrong, and the first
    version of this one did exactly that."""

    def setUp(self):
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "fixtures", "g1b-stdout.json"
        )
        with open(path, encoding="utf-8") as fh:
            self.raw = fh.read()

    def test_real_intercepted_run_reads_as_held(self):
        self.assertEqual(parse_cowork_output(self.raw)["barrier"]["status"], "held")

    def test_real_intercepted_run_has_no_error(self):
        self.assertIsNone(parse_cowork_output(self.raw)["error"])

    def test_the_fixture_really_does_lack_callback_exchanges(self):
        """Pin the premise this detector rests on, so a CLI change that starts
        populating the array surfaces here rather than silently altering what
        'held' means."""
        self.assertEqual(json.loads(self.raw).get("callback_exchanges"), [])

    def test_the_fixture_really_does_carry_a_write_tool(self):
        names = [t["tool_name"] for t in json.loads(self.raw)["tool_trace"]]
        self.assertIn("Send email with attachments", names)


if __name__ == "__main__":
    unittest.main()
