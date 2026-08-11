"""Phase 3: the API run transport, behind the flag.

`_api_payload_from_events` is the load-bearing piece. It turns an SSE stream into
the SAME JSON document the CLI writes to stdout, so everything downstream -
parse_cowork_output, _barrier_verdict, _canonical_tools, _extract_draft, both
UIs - is untouched. That equivalence is the whole reason this migration is
cheap, so it is tested directly rather than only through the transport.

Event shapes are taken from real captures against the runtime on 2026-08-10:

    event: ts    {"tid","tn","ts","inp"}      tool start, inp = resolved args
    event: tx    {"tid","tn","dur","ok","ts"} tool end
    event: dx    {"t"}                        assistant text delta
    event: rl    {"st"}                       run lifecycle: started / ok

The SSE kind lives on its own `event:` line, NOT inside the `data:` JSON. Reading
ev["event"] yields None for every event, which is what made an early spike appear
to hang for 600 seconds. The parser must track the preceding line.
"""

import json
import shutil
import tempfile
import unittest
from unittest import mock

from src.services import cowork_runner as cr
from src.services.cowork_runner import (
    _api_payload_from_events,
    _iter_sse,
    parse_cowork_output,
)


class _FakeProc:
    """Minimal stand-in for the subprocess path in routing tests."""

    returncode = 0

    def __init__(self):
        import io

        self.stdout = io.StringIO('{"terminal_status":"ok","text":"hi"}')
        self.stderr = io.StringIO("")

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass

    def communicate(self, timeout=None):
        return self.stdout.getvalue(), ""


SSE = (
    "event: session\n"
    'data: {"sid":"abc","tenant":"t","user":"u"}\n'
    "\n"
    "event: rl\n"
    'data: {"st":"started","ts":1}\n'
    "\n"
    "event: ts\n"
    'data: {"tid":"a","tn":"tool_search_tool","inp":"{\\"pattern\\":\\"x\\"}"}\n'
    "\n"
    "event: tx\n"
    'data: {"tid":"a","tn":"tool_search_tool","dur":407,"ok":true}\n'
    "\n"
    "event: ts\n"
    'data: {"tid":"b","tn":"mcp__outlook__SendEmailWithAttachments",'
    '"inp":"{\\"to\\":[\\"phtopnes@microsoft.com\\"],\\"subject\\":\\"Hi\\"}"}\n'
    "\n"
    "event: tx\n"
    'data: {"tid":"b","tn":"mcp__outlook__SendEmailWithAttachments","dur":303,"ok":true}\n'
    "\n"
    "event: dx\n"
    'data: {"t":"I wasn\'t able to send that - "}\n'
    "\n"
    "event: dx\n"
    'data: {"t":"the send was blocked before anything went out."}\n'
    "\n"
    "event: rl\n"
    'data: {"st":"ok","ts":9}\n'
    "\n"
)


class TestSseParsing(unittest.TestCase):
    def test_kind_comes_from_the_event_line_not_the_data(self):
        """The bug that made an early spike look like a 600s hang."""
        kinds = [kind for kind, _ in _iter_sse(SSE.splitlines())]
        self.assertIn("ts", kinds)
        self.assertIn("rl", kinds)

    def test_data_is_decoded_to_objects(self):
        events = dict(_iter_sse(SSE.splitlines()))
        self.assertIsInstance(events["rl"], dict)

    def test_malformed_data_lines_are_skipped(self):
        lines = ["event: rl", "data: {not json", "", "event: rl", 'data: {"st":"ok"}']
        self.assertEqual([k for k, _ in _iter_sse(lines)], ["rl"])

    def test_comments_and_ids_do_not_become_events(self):
        lines = [": keepalive", "id: seq:1", "event: rl", 'data: {"st":"ok"}']
        self.assertEqual([k for k, _ in _iter_sse(lines)], ["rl"])


class TestPayloadEquivalence(unittest.TestCase):
    """The API result must be indistinguishable from the CLI's, downstream."""

    def setUp(self):
        self.payload = _api_payload_from_events(
            list(_iter_sse(SSE.splitlines())), conversation_id="t:u:cw-1",
        )

    def test_terminal_status_comes_from_the_run_lifecycle(self):
        self.assertEqual(self.payload["terminal_status"], "ok")

    def test_text_is_the_concatenated_deltas(self):
        self.assertIn("blocked before anything went out", self.payload["text"])

    def test_conversation_id_is_carried(self):
        self.assertEqual(self.payload["conversation_id"], "t:u:cw-1")

    def test_tool_trace_uses_canonical_names(self):
        names = [t["tool_name"] for t in self.payload["tool_trace"]]
        self.assertIn("mcp__outlook__SendEmailWithAttachments", names)

    def test_sse_events_carry_the_kind_inline_for_canonical_tools(self):
        """_canonical_tools reads ev["event"], so the kind must be folded in."""
        kinds = {e.get("event") for e in self.payload["sse_events"]}
        self.assertIn("ts", kinds)
        self.assertIn("tx", kinds)

    def test_resolved_tool_arguments_are_preserved(self):
        """`inp` is what a Cowork-style approval card would be built from."""
        starts = [e for e in self.payload["sse_events"] if e.get("event") == "ts"]
        send = [e for e in starts if "SendEmail" in (e.get("tn") or "")]
        self.assertTrue(send)
        self.assertIn("phtopnes@microsoft.com", send[0]["inp"])


class TestDownstreamIsUnchanged(unittest.TestCase):
    """The real proof: the existing parser handles it with no special casing."""

    def setUp(self):
        payload = _api_payload_from_events(
            list(_iter_sse(SSE.splitlines())), conversation_id="t:u:cw-1",
        )
        self.parsed = parse_cowork_output(json.dumps(payload), "")

    def test_parses_without_error(self):
        self.assertIsNone(self.parsed["error"])

    def test_barrier_verdict_is_computed(self):
        """A paraphrased block over the API reads as held, exactly as over the
        subprocess after the Phase 1 accuracy fix."""
        self.assertEqual(self.parsed["barrier"]["status"], "held")

    def test_canonical_tools_are_recovered(self):
        names = [t["name"] for t in self.parsed["tools"]]
        self.assertIn("mcp__outlook__SendEmailWithAttachments", names)


class TestFailureShapes(unittest.TestCase):
    def test_a_run_that_never_reached_terminal_is_not_ok(self):
        lines = ["event: rl", 'data: {"st":"started"}']
        payload = _api_payload_from_events(list(_iter_sse(lines)), "t:u:c")
        self.assertNotEqual(payload["terminal_status"], "ok")

    def test_an_empty_stream_still_produces_a_parseable_document(self):
        payload = _api_payload_from_events([], "t:u:c")
        parsed = parse_cowork_output(json.dumps(payload), "")
        self.assertIsInstance(parsed, dict)

    def test_error_terminal_status_is_carried(self):
        lines = ["event: rl", 'data: {"st":"error"}']
        payload = _api_payload_from_events(list(_iter_sse(lines)), "t:u:c")
        self.assertEqual(payload["terminal_status"], "error")


if __name__ == "__main__":
    unittest.main()


class TestFlagRouting(unittest.TestCase):
    """start_preview picks a transport. Default is the proven subprocess."""

    def setUp(self):
        cr.reset_registry()
        self.addCleanup(cr.reset_registry)
        self._cost = cr._cost_snapshot_fn
        cr._cost_snapshot_fn = lambda: None
        self.addCleanup(lambda: setattr(cr, "_cost_snapshot_fn", self._cost))
        self._precheck = cr.tenant_barrier_precheck
        cr.tenant_barrier_precheck = lambda **k: {"status": "ok", "reason": ""}
        self.addCleanup(lambda: setattr(cr, "tenant_barrier_precheck", self._precheck))
        self.tmp = tempfile.mkdtemp(prefix="cw-transport-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_flag_off_uses_the_subprocess_path(self):
        spawned = []

        def spawn(argv, **kw):
            spawned.append(argv)
            return _FakeProc()

        with mock.patch.object(cr, "api_transport_enabled", lambda: False):
            cr.start_preview(1, "hello", spawn=spawn, log_dir=self.tmp)
        cr.wait_for(cr.preview_label(1), timeout=10)
        self.assertEqual(len(spawned), 1)

    def test_flag_on_does_not_spawn_a_subprocess(self):
        spawned = []

        def spawn(argv, **kw):
            spawned.append(argv)
            return _FakeProc()

        def fake_run(prompt, config, on_progress, conversation_id=None):
            return {"terminal_status": "ok", "text": "hi", "sse_events": [],
                    "tool_trace": [], "conversation_id": "t:u:cw-x",
                    "callback_exchanges": [], "duration_seconds": None}

        with mock.patch.object(cr, "api_transport_enabled", lambda: True), \
             mock.patch.object(cr, "_api_run_fn", fake_run):
            cr.start_preview(2, "hello", spawn=spawn, log_dir=self.tmp)
            cr.wait_for(cr.preview_label(2), timeout=10)
        self.assertEqual(spawned, [])

    def test_api_result_has_the_same_shape_as_the_subprocess_result(self):
        """The invariant that makes everything downstream unchanged."""
        def fake_run(prompt, config, on_progress, conversation_id=None):
            return {"terminal_status": "ok", "text": "hi", "sse_events": [],
                    "tool_trace": [], "conversation_id": "t:u:cw-x",
                    "callback_exchanges": [], "duration_seconds": None}

        with mock.patch.object(cr, "api_transport_enabled", lambda: True), \
             mock.patch.object(cr, "_api_run_fn", fake_run):
            label = cr.start_preview(3, "hello", log_dir=self.tmp)
            cr.wait_for(label, timeout=10)
        result = cr.get_result(label)
        self.assertEqual(
            sorted(result.keys()),
            ["auth_failed", "cost_credits", "error", "exit_code", "stderr", "stdout"],
        )
        self.assertIsNone(result["error"])
        parsed = cr.parse_cowork_output(result["stdout"], result["stderr"])
        self.assertEqual(parsed["conversation_id"], "t:u:cw-x")

    def test_api_failure_becomes_an_error_result_not_a_hang(self):
        def boom(prompt, config, on_progress, conversation_id=None):
            raise OSError("island unreachable")

        with mock.patch.object(cr, "api_transport_enabled", lambda: True), \
             mock.patch.object(cr, "_api_run_fn", boom):
            label = cr.start_preview(4, "hello", log_dir=self.tmp)
            cr.wait_for(label, timeout=10)
        result = cr.get_result(label)
        self.assertFalse(cr.is_running(label))
        self.assertTrue(result["error"])

    def test_api_progress_reaches_the_same_ring_the_card_reads(self):
        def fake_run(prompt, config, on_progress, conversation_id=None):
            on_progress("Searching your Teams and calendar")
            return {"terminal_status": "ok", "text": "hi", "sse_events": [],
                    "tool_trace": [], "conversation_id": "t:u:cw-x",
                    "callback_exchanges": [], "duration_seconds": None}

        with mock.patch.object(cr, "api_transport_enabled", lambda: True), \
             mock.patch.object(cr, "_api_run_fn", fake_run):
            label = cr.start_preview(5, "hello", log_dir=self.tmp)
            cr.wait_for(label, timeout=10)
        self.assertIn("Searching your Teams and calendar", cr.get_progress(label))

    def test_the_barrier_config_is_still_built_on_the_api_path(self):
        """The write barrier is transport-independent and must stay on."""
        seen = {}

        def fake_run(prompt, config, on_progress, conversation_id=None):
            seen["config"] = config
            return {"terminal_status": "ok", "text": "", "sse_events": [],
                    "tool_trace": [], "conversation_id": "t:u:cw-x",
                    "callback_exchanges": [], "duration_seconds": None}

        with mock.patch.object(cr, "api_transport_enabled", lambda: True), \
             mock.patch.object(cr, "_api_run_fn", fake_run):
            label = cr.start_preview(6, "hello", log_dir=self.tmp)
            cr.wait_for(label, timeout=10)
        self.assertGreater(len(seen["config"]["tool_names"]), 100)
        self.assertTrue(seen["config"]["static_results"])

