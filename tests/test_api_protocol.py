"""A follow-up turn uses a different request shape from the first turn.

Task 2268 refine failed in 6 seconds: `POST /v1/subscribe` returned 200 and the
stream closed 1.1s later with no events at all, so there was no terminal `rl`
and the run reported "Cowork finished with status unknown".

Cause: I used the TURN-1 request shape for a follow-up turn. There is no
published spec for this SSE protocol, but the `cowork` CLI is another client of
the same HTTP API, and its source (cowork_cli/services/live_session.py:213-238)
documents the sequence:

    turn 1      POST /v1/subscribe          prompt rides the subscribe body
    follow-up   GET  /v1/subscribe          re-resolves pod locality, opens SSE
                POST /v1/messages           delivers the prompt

A fresh POST /v1/subscribe on an EXISTING conversation is not the sanctioned
path. It happened to work on task 2183 (the actor pod was still warm) and
failed on 2268 once the session had been suspended, which is exactly the kind
of intermittent failure that a "works on my machine" test would miss.

SAFETY: `toolCallbackConfig` rides on the POST /v1/messages body, so the write
barrier is still sent per turn. The test below pins that, because losing it on
the follow-up path would mean an unbarriered turn.
"""

import unittest
import uuid

from src.services import cowork_runner as cr


class _FakeStream:
    """Context manager standing in for httpx's streaming response."""

    def __init__(self, status, lines):
        self.status_code = status
        self._lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_lines(self):
        return iter(self._lines)

    def read(self):
        return b""


class _FakeClient:
    """Records the request sequence so the protocol shape can be asserted."""

    def __init__(self, lines=None, stream_status=200, post_status=200):
        self.calls = []
        self._lines = lines if lines is not None else [
            "event: rl", 'data: {"st":"started"}',
            "event: dx", 'data: {"t":"revised draft"}',
            "event: rl", 'data: {"st":"ok"}',
        ]
        self._stream_status = stream_status
        self._post_status = post_status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def stream(self, method, url, **kw):
        self.calls.append({"verb": method, "url": url, "json": kw.get("json")})
        return _FakeStream(self._stream_status, self._lines)

    def post(self, url, **kw):
        self.calls.append({"verb": "POST", "url": url, "json": kw.get("json")})

        class _R:
            status_code = self._post_status
            text = ""

        return _R()

    def close(self):
        pass


class ApiProtocolTestBase(unittest.TestCase):
    def setUp(self):
        self._orig = cr._api_http_client_fn
        self.addCleanup(lambda: setattr(cr, "_api_http_client_fn", self._orig))
        self._auth = cr._api_auth_fn
        cr._api_auth_fn = lambda: ("tok", "https://island.example.com", "t", "u")
        self.addCleanup(lambda: setattr(cr, "_api_auth_fn", self._auth))

    def _run(self, conversation_id=None, client=None):
        client = client or _FakeClient()
        cr._api_http_client_fn = lambda: client
        payload = cr._api_run_default(
            "do the thing", {"tool_names": ["x"], "static_results": {"x": "y"}},
            lambda text: None, conversation_id=conversation_id,
        )
        return client, payload


class TestFirstTurnIsUnchanged(ApiProtocolTestBase):
    def test_a_new_conversation_posts_to_subscribe(self):
        client, _ = self._run()
        self.assertEqual(client.calls[0]["verb"], "POST")
        self.assertTrue(client.calls[0]["url"].endswith("/v1/subscribe"))

    def test_the_prompt_rides_the_subscribe_body(self):
        client, _ = self._run()
        body = client.calls[0]["json"]
        self.assertEqual(body["content"][0]["text"], "do the thing")

    def test_it_mints_a_conversation_id(self):
        _, payload = self._run()
        self.assertTrue(payload["conversation_id"].startswith("t:u:"))

    def test_the_session_segment_is_a_full_uuid(self):
        """Match the format the Cowork web app mints for its own tasks.

        The CLI mints ``cw-<8 hex>`` and we copied it. Every task the web app
        creates uses a full UUID as the third segment.

        This was changed while chasing an HTTP 403 from the web app. It does
        NOT fix that: the 403 reproduces with a full UUID too, and the cause is
        still unknown (see cowork-bug-reports-draft.md #9). The format is kept
        because matching the web app removes one confounder, not because it
        makes the handoff work.
        """
        _, payload = self._run()
        session = payload["conversation_id"].split(":")[-1]
        self.assertFalse(
            session.startswith("cw-"),
            "session ids should match the web app's UUID format",
        )
        # Raises ValueError if it is not a real UUID.
        self.assertEqual(str(uuid.UUID(session)), session)


class TestFollowUpUsesTheDocumentedShape(ApiProtocolTestBase):
    """The bug: a follow-up must NOT re-POST /v1/subscribe."""

    def test_it_opens_the_stream_with_get(self):
        client, _ = self._run(conversation_id="t:u:cw-existing")
        self.assertEqual(client.calls[0]["verb"], "GET")
        self.assertIn("/v1/subscribe", client.calls[0]["url"])

    def test_it_delivers_the_prompt_to_messages(self):
        client, _ = self._run(conversation_id="t:u:cw-existing")
        posts = [c for c in client.calls if c["verb"] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertTrue(posts[0]["url"].endswith("/v1/messages"))
        self.assertEqual(posts[0]["json"]["content"][0]["text"], "do the thing")

    def test_it_never_reposts_subscribe(self):
        client, _ = self._run(conversation_id="t:u:cw-existing")
        bad = [c for c in client.calls
               if c["verb"] == "POST" and c["url"].endswith("/v1/subscribe")]
        self.assertEqual(bad, [])

    def test_the_get_carries_the_conversation_id_as_a_query_param(self):
        """aether subscribe.py declares conversation_id as a required Query
        parameter (alias "conversationId"). Omitting it is a 400 — which is
        exactly what task 2268's refine hit after the verb was fixed."""
        client, _ = self._run(conversation_id="t:u:cw-existing")
        self.assertIn("conversationId=t%3Au%3Acw-existing", client.calls[0]["url"])

    def test_a_first_turn_does_not_request_replay(self):
        client, _ = self._run()
        self.assertNotIn("since=", client.calls[0]["url"])

    def test_the_get_carries_no_body(self):
        client, _ = self._run(conversation_id="t:u:cw-existing")
        self.assertIsNone(client.calls[0]["json"])

    def test_the_write_barrier_still_rides_the_follow_up(self):
        """SAFETY-CRITICAL: the barrier is per-request, so losing it here would
        mean an unbarriered turn."""
        client, _ = self._run(conversation_id="t:u:cw-existing")
        posts = [c for c in client.calls if c["verb"] == "POST"]
        self.assertTrue(posts[0]["json"]["toolCallbackConfig"]["static_results"])

    def test_it_keeps_the_conversation_id(self):
        _, payload = self._run(conversation_id="t:u:cw-existing")
        self.assertEqual(payload["conversation_id"], "t:u:cw-existing")


class TestApprovedExecutionOmitsTheBarrier(ApiProtocolTestBase):
    def test_none_config_omits_tool_callback_config_entirely(self):
        client = _FakeClient()
        cr._api_http_client_fn = lambda: client
        cr._api_run_default(
            "send the approved message",
            None,
            lambda text: None,
            conversation_id="t:u:cw-existing",
            is_follow_up=True,
        )
        post = [c for c in client.calls if c["verb"] == "POST"][0]
        self.assertNotIn("toolCallbackConfig", post["json"])


class TestAnEmptyStreamIsReportedHonestly(ApiProtocolTestBase):
    """What actually happened on 2268: 200, then silence.

    "Cowork finished with status unknown" gives the user nothing to act on. A
    stream that carried NO events is a distinct, recognisable failure.
    """

    def test_no_events_raises_something_actionable(self):
        client = _FakeClient(lines=[])
        with self.assertRaises(RuntimeError) as caught:
            self._run(conversation_id="t:u:cw-existing", client=client)
        self.assertIn("no events", str(caught.exception).lower())

    def test_a_first_turn_with_no_events_also_raises(self):
        client = _FakeClient(lines=[])
        with self.assertRaises(RuntimeError):
            self._run(client=client)

    def test_a_non_200_get_is_reported(self):
        client = _FakeClient(stream_status=404)
        with self.assertRaises(RuntimeError) as caught:
            self._run(conversation_id="t:u:cw-existing", client=client)
        self.assertIn("404", str(caught.exception))

    def test_a_failed_message_post_is_reported(self):
        client = _FakeClient(post_status=500)
        with self.assertRaises(RuntimeError) as caught:
            self._run(conversation_id="t:u:cw-existing", client=client)
        self.assertIn("500", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
