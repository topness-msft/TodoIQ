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
    parse_execution_output,
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


class TestExecutionApprovalAnswer(unittest.TestCase):
    def _aq(self, question, options, *, multi=False):
        return {
            "iid": "approval-1",
            "q": [{
                "id": "confirm",
                "question": question,
                "options": [
                    {"label": label, "value": value}
                    for label, value in options
                ],
                "multiSelect": multi,
            }],
        }

    def test_matches_each_supported_write_channel(self):
        cases = [
            ("teams", "Send a chat?", [("Send", "send")], "send"),
            ("email", "Send this email?", [("Send", "send")], "send"),
            (
                "calendar",
                "Create this calendar event?",
                [("Create", "create")],
                "create",
            ),
        ]
        for channel, question, options, expected in cases:
            with self.subTest(channel=channel):
                answer = cr._execution_approval_answer(
                    self._aq(question, options), channel
                )
                self.assertEqual(answer, ("approval-1", {"0": expected}))

    def test_rejects_channel_mismatch_and_missing_information(self):
        self.assertIsNone(
            cr._execution_approval_answer(
                self._aq("Send this email?", [("Send", "send")]), "teams"
            )
        )
        self.assertIsNone(
            cr._execution_approval_answer(
                self._aq("Which account should I use?", [("Yes", "yes")]),
                "email",
            )
        )

    def test_rejects_approval_that_bundles_another_write(self):
        cases = [
            ("teams", "Send this Teams message and delete the original chat?"),
            ("email", "Send this email and create a calendar event?"),
            ("calendar", "Create this calendar event and post a Teams message?"),
        ]
        for channel, question in cases:
            with self.subTest(channel=channel):
                self.assertIsNone(
                    cr._execution_approval_answer(
                        self._aq(question, [("Yes", "yes")]), channel
                    )
                )

    def test_rejects_multi_question_multi_select_and_ambiguous_answers(self):
        two_questions = self._aq("Send a chat?", [("Send", "send")])
        two_questions["q"].append({
            "id": "other",
            "question": "Anything else?",
            "options": [{"label": "No", "value": "no"}],
        })
        self.assertIsNone(
            cr._execution_approval_answer(two_questions, "teams")
        )
        self.assertIsNone(
            cr._execution_approval_answer(
                self._aq("Send a chat?", [("Send", "send")], multi=True),
                "teams",
            )
        )
        self.assertIsNone(
            cr._execution_approval_answer(
                self._aq(
                    "Send a chat?",
                    [("Send", "send"), ("Yes", "yes")],
                ),
                "teams",
            )
        )

    def test_hanging_stream_resumes_after_exact_approval_answer(self):
        class Response:
            status_code = 200

            def __init__(self, client):
                self.client = client

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def iter_lines(self):
                yield "event: rl"
                yield 'data: {"st":"started"}'
                yield ""
                yield "event: aq"
                yield (
                    'data: {"iid":"approval-1","q":[{"id":"confirm",'
                    '"question":"Send a chat?","options":'
                    '[{"label":"Send","value":"send"},'
                    '{"label":"Cancel","value":"cancel"}]}]}'
                )
                yield ""
                answered = [
                    body for body in self.client.posts
                    if body["content"][0]["type"] == "ask_user_answer"
                ]
                self.assertTrue(answered)
                yield "event: rl"
                yield 'data: {"st":"ok"}'

            def assertTrue(self, value):
                if not value:
                    raise AssertionError("approval answer was not posted")

        class Posted:
            status_code = 202

        class Client:
            def __init__(self):
                self.posts = []

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def stream(self, *_args, **_kwargs):
                return Response(self)

            def post(self, _url, **kwargs):
                self.posts.append(kwargs["json"])
                return Posted()

        client = Client()
        with mock.patch.object(
            cr, "_api_auth_fn", return_value=("token", "https://api", "t", "u")
        ), mock.patch.object(cr, "_api_http_client_fn", return_value=client):
            payload = cr._api_run_default(
                "send it",
                None,
                lambda _text: None,
                conversation_id="t:u:cw-existing",
                is_follow_up=True,
                approval_kind="teams",
            )
        self.assertEqual(payload["terminal_status"], "ok")
        answer = client.posts[1]["content"][0]["rawEvent"]
        self.assertEqual(answer["invocationId"], "approval-1")
        self.assertEqual(answer["answers"], {"0": "send"})


class TestExecutionToolApproval(unittest.TestCase):
    def setUp(self):
        self.conversation_id = "tenant:user:conversation"
        self.snapshot = {
            "draft": "Circling back on the squad plan.",
            "destination_ref": "19:rima@unq.gbl.spaces",
            "delivery_channel": "teams",
        }
        self.ta = {
            "aid": "mcp-request:jrpc:2",
            "tn": "PostMessage",
            "sn": "m365_teams",
            "params": {
                "chat_id": "19:rima@unq.gbl.spaces",
                "body": (
                    "<p>Circling back on the squad plan.</p><br><br>"
                    "<!-- aether-footer -->"
                    "<span style=\"font-size:11px;color:#666;\">Sent by "
                    "<a href=\"https://aka.ms/cowork?cw_source=teams&amp;"
                    "cw_tool=PostMessage\">Copilot Cowork</a></span>"
                ),
            },
        }

    def test_live_teams_shape_builds_exact_web_approval(self):
        self.assertEqual(
            cr._execution_tool_approval(
                self.ta, "teams", self.snapshot, self.conversation_id
            ),
            {
                "always_allow": False,
                "approval_id": "mcp-request:jrpc:2",
                "approved": True,
                "conversation_id": self.conversation_id,
                "edited_input": None,
                "scope": None,
                "server_name": "m365_teams",
                "session_id": self.conversation_id,
                "tool_name": "PostMessage",
            },
        )

    def test_rejects_unapproved_or_changed_tool_calls(self):
        cases = [
            ({**self.ta, "aid": ""}, "teams", self.snapshot),
            ({**self.ta, "tn": "DeleteMessage"}, "teams", self.snapshot),
            ({**self.ta, "sn": "outlook"}, "teams", self.snapshot),
            (
                {
                    **self.ta,
                    "params": {
                        **self.ta["params"],
                        "chat_id": "19:someone-else@unq.gbl.spaces",
                    },
                },
                "teams",
                self.snapshot,
            ),
            (
                {
                    **self.ta,
                    "params": {
                        **self.ta["params"],
                        "body": "<p>Changed message.</p>",
                    },
                },
                "teams",
                self.snapshot,
            ),
            (
                {
                    **self.ta,
                    "params": {
                        **self.ta["params"],
                        "body": (
                            "<p><a href=\"https://evil.example\">Circling back "
                            "on the squad plan.</a></p><br><br>"
                            "<!-- aether-footer -->"
                            "<span style=\"font-size:11px;color:#666;\">Sent by "
                            "<a href=\"https://aka.ms/cowork?cw_source=teams&amp;"
                            "cw_tool=PostMessage\">Copilot Cowork</a></span>"
                        ),
                    },
                },
                "teams",
                self.snapshot,
            ),
            (self.ta, "email", self.snapshot),
            (self.ta, "calendar", self.snapshot),
            (self.ta, "teams", None),
        ]
        for data, kind, snapshot in cases:
            with self.subTest(data=data, kind=kind):
                self.assertIsNone(
                    cr._execution_tool_approval(
                        data, kind, snapshot, self.conversation_id
                    )
                )

    def test_ta_event_posts_once_to_tool_approval_and_resumes(self):
        class Response:
            status_code = 200

            def __init__(self, client):
                self.client = client

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def iter_lines(self):
                yield "event: rl"
                yield 'data: {"st":"started"}'
                yield ""
                for _ in range(2):
                    yield "event: ta"
                    yield "data: " + json.dumps(self.client.ta)
                    yield ""
                approvals = [
                    call for call in self.client.posts
                    if call["url"].endswith("/v1/tool-approval")
                ]
                if len(approvals) != 1:
                    raise AssertionError("tool approval was not posted exactly once")
                yield "event: rl"
                yield 'data: {"st":"ok"}'

        class Posted:
            status_code = 200
            text = '{"success":true}'

        class Client:
            def __init__(self, ta):
                self.ta = ta
                self.posts = []

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def stream(self, *_args, **_kwargs):
                return Response(self)

            def post(self, url, **kwargs):
                self.posts.append({"url": url, **kwargs})
                return Posted()

        client = Client(self.ta)
        with mock.patch.object(
            cr, "_api_auth_fn", return_value=("token", "https://api", "t", "u")
        ), mock.patch.object(cr, "_api_http_client_fn", return_value=client):
            payload = cr._api_run_default(
                "send it",
                None,
                lambda _text: None,
                conversation_id=self.conversation_id,
                is_follow_up=True,
                approval_kind="teams",
                approved_snapshot=self.snapshot,
            )

        self.assertEqual(payload["terminal_status"], "ok")
        approval = [
            call for call in client.posts
            if call["url"].endswith("/v1/tool-approval")
        ][0]
        self.assertEqual(
            approval["headers"]["X-Conversation-ID"], self.conversation_id
        )
        self.assertEqual(approval["json"]["approval_id"], self.ta["aid"])
        self.assertIsNone(approval["json"]["edited_input"])


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

    def test_tool_trace_keeps_input_for_safe_contextual_labels(self):
        send = [
            item for item in self.payload["tool_trace"]
            if "SendEmail" in item["tool_name"]
        ][0]
        self.assertIn("phtopnes@microsoft.com", send["input"])


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

    def test_fail_terminal_status_is_carried(self):
        lines = ["event: rl", 'data: {"st":"fail"}']
        payload = _api_payload_from_events(list(_iter_sse(lines)), "t:u:c")
        self.assertEqual(payload["terminal_status"], "fail")

    def test_container_bash_is_not_delivery_evidence(self):
        payload = {
            "terminal_status": "ok",
            "text": "Done.",
            "sse_events": [
                {"event": "ts", "tid": "bash-1", "tn": "Bash"},
                {
                    "event": "tx",
                    "tid": "bash-1",
                    "tn": "Bash",
                    "ok": True,
                },
            ],
            "tool_trace": [{"tool_name": "Bash", "ok": True}],
            "callback_exchanges": [],
        }

        parsed = parse_execution_output(json.dumps(payload))

        self.assertFalse(parsed["delivery_confirmed"])
        self.assertEqual(parsed["executed_write_tools"], [])


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

        def fake_run(prompt, config, on_progress, conversation_id=None, is_follow_up=None):
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
        def fake_run(prompt, config, on_progress, conversation_id=None, is_follow_up=None):
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
        def boom(prompt, config, on_progress, conversation_id=None, is_follow_up=None):
            raise OSError("island unreachable")

        with mock.patch.object(cr, "api_transport_enabled", lambda: True), \
             mock.patch.object(cr, "_api_run_fn", boom):
            label = cr.start_preview(4, "hello", log_dir=self.tmp)
            cr.wait_for(label, timeout=10)
        result = cr.get_result(label)
        self.assertFalse(cr.is_running(label))
        self.assertTrue(result["error"])

    def test_api_progress_reaches_the_same_ring_the_card_reads(self):
        def fake_run(prompt, config, on_progress, conversation_id=None, is_follow_up=None):
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

        def fake_run(prompt, config, on_progress, conversation_id=None, is_follow_up=None):
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

    def test_execution_is_api_only_and_never_builds_a_barrier(self):
        seen = {}

        def fake_run(prompt, config, on_progress, conversation_id=None, is_follow_up=None):
            seen.update(
                prompt=prompt,
                config=config,
                conversation_id=conversation_id,
                is_follow_up=is_follow_up,
            )
            return {
                "terminal_status": "ok",
                "text": "Sent.",
                "sse_events": [],
                "tool_trace": [],
                "conversation_id": conversation_id,
                "callback_exchanges": [],
                "duration_seconds": None,
            }

        with mock.patch.object(cr, "api_transport_enabled", lambda: True), \
             mock.patch.object(cr, "_api_run_fn", fake_run), \
             mock.patch.object(
                 cr, "build_callback_config",
                 side_effect=AssertionError("execution must not build the barrier"),
             ):
            label = cr.start_execution(
                7,
                "send the approved message",
                "t:u:cw-existing",
                log_dir=self.tmp,
            )
            cr.wait_for(label, timeout=10)

        self.assertIsNone(seen["config"])
        self.assertEqual(seen["conversation_id"], "t:u:cw-existing")
        self.assertTrue(seen["is_follow_up"])

    def test_execution_rejects_the_subprocess_transport(self):
        with mock.patch.object(cr, "api_transport_enabled", lambda: False):
            with self.assertRaises(RuntimeError):
                cr.start_execution(8, "send", "t:u:cw-existing", log_dir=self.tmp)

    def test_execution_setup_failure_releases_the_registry_slot(self):
        with mock.patch.object(cr, "api_transport_enabled", lambda: True), \
             mock.patch.object(
                 cr, "write_prompt_file", side_effect=OSError("disk unavailable")
             ):
            with self.assertRaises(OSError):
                cr.start_execution(
                    9, "send", "t:u:cw-existing", log_dir=self.tmp
                )

        self.assertFalse(cr.is_running(cr.execution_label(9)))
