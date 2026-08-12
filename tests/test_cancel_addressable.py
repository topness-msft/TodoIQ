"""Stop must work from the moment a run starts.

Phil pressed Stop ~12s into task 2269 and got:

    "Stop was requested but Cowork did not confirm it. The run may still be
     finishing on the server."

and the card then went back to a spinner. Reproduced: the row had NO
conversation_id and was stranded in 'previewing'.

Cause: the conversation id is minted inside the run and only written to the row
when the run FINISHES. Cancel targets
POST /v1/conversations/{id}/pause, so for the whole "Preparing workspace /
Connecting to container" window there is nothing to address. Two consequences,
both bad: the server-side run carries on spending credits, and the still-live
worker overwrites the 'failed' row back to 'previewing', so the card returns to
a spinner and the task can never be previewed again.

We mint that id OURSELVES before the first request (it is echoed back, not
issued by the server), so there is no reason to wait for the run to end. Minting
up front makes Stop addressable from t=0.

The catch this must not trip over: passing a conversation_id currently ALSO
means "this is a follow-up turn", which selects a different request shape
(GET /v1/subscribe + POST /v1/messages). Turn 1 must keep POSTing /v1/subscribe.
The id and the turn kind have to be separable.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.services import cowork_runner as cr  # noqa: E402


class _FakeStream:
    def __init__(self, status, lines):
        self.status_code = status
        self._lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_lines(self):
        return iter(self._lines)

    def read(self):
        return b""


class _FakeClient:
    def __init__(self, lines):
        self.calls = []
        self._lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def stream(self, verb, url, **kw):
        self.calls.append({"verb": verb, "url": url, "json": kw.get("json")})
        return _FakeStream(200, self._lines)

    def post(self, url, **kw):
        self.calls.append({"verb": "POST", "url": url, "json": kw.get("json")})

        class _R:
            status_code = 200

            def json(self_inner):
                return {"ok": True}

        return _R()


_TERMINAL = [
    "event: run_lifecycle",
    'data: {"status":"ok"}',
    "",
]


class MintedUpFrontTest(unittest.TestCase):
    def setUp(self):
        self.client = _FakeClient(_TERMINAL)
        self._auth = cr._api_auth_fn
        self._http = cr._api_http_client_fn
        cr._api_auth_fn = lambda: ("tok", "https://base", "t", "u")
        cr._api_http_client_fn = lambda *a, **k: self.client
        self.addCleanup(self._restore)

    def _restore(self):
        cr._api_auth_fn = self._auth
        cr._api_http_client_fn = self._http

    def test_a_supplied_id_can_still_be_turn_one(self):
        """The id and the turn kind must be separable."""
        cr._api_run_default(
            "do it", {"tool_names": []}, lambda t: None,
            conversation_id="t:u:abc", is_follow_up=False,
        )
        first = self.client.calls[0]
        self.assertEqual(first["verb"], "POST")
        self.assertTrue(first["url"].endswith("/v1/subscribe"))
        self.assertEqual(first["json"]["conversationId"], "t:u:abc")

    def test_the_supplied_id_is_the_one_used(self):
        out = cr._api_run_default(
            "do it", {"tool_names": []}, lambda t: None,
            conversation_id="t:u:abc", is_follow_up=False,
        )
        self.assertEqual(out["conversation_id"], "t:u:abc")

    def test_a_follow_up_still_uses_the_follow_up_shape(self):
        cr._api_run_default(
            "revise", {"tool_names": []}, lambda t: None,
            conversation_id="t:u:abc", is_follow_up=True,
        )
        self.assertEqual(self.client.calls[0]["verb"], "GET")

    def test_an_omitted_id_is_still_minted(self):
        out = cr._api_run_default(
            "do it", {"tool_names": []}, lambda t: None,
        )
        self.assertTrue(out["conversation_id"].startswith("t:u:"))


class MintHelperTest(unittest.TestCase):
    def test_new_conversation_id_is_addressable_and_web_app_shaped(self):
        import uuid
        cid = cr.new_conversation_id(lambda: ("tok", "https://b", "tenant", "oid"))
        tenant, oid, session = cid.split(":")
        self.assertEqual(tenant, "tenant")
        self.assertEqual(oid, "oid")
        self.assertEqual(str(uuid.UUID(session)), session)

    def test_it_fails_soft_when_auth_is_unavailable(self):
        def boom():
            raise RuntimeError("not logged in")

        self.assertIsNone(cr.new_conversation_id(boom))


if __name__ == "__main__":
    unittest.main()
