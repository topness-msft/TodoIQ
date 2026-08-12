import unittest

from src.services import cowork_runner as cr


class _Stream:
    status_code = 200

    def __init__(self, lines):
        self.lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_lines(self):
        return iter(self.lines)


class _Client:
    def __init__(self, lines):
        self.lines = lines
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def stream(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return _Stream(self.lines)


class ReadBlockedQuestionTest(unittest.TestCase):
    def setUp(self):
        self._auth = cr._api_auth_fn
        self._client = cr._api_http_client_fn
        cr._api_auth_fn = lambda: ("tok", "https://island", "t", "u")
        self.addCleanup(lambda: setattr(cr, "_api_auth_fn", self._auth))
        self.addCleanup(lambda: setattr(cr, "_api_http_client_fn", self._client))

    def test_it_replays_history_without_last_event_id(self):
        client = _Client([
            "event: aq",
            'data: {"iid":"invoke-1","q":[{"id":"account","question":"Which account should I use?"}]}',
            "event: rl", 'data: {"st":"needs_user_input"}',
            "event: rpc", 'data: {"cnt":2,"ts":1}',
        ])
        cr._api_http_client_fn = lambda: client

        question = cr.read_blocked_question("t:u:conversation")

        self.assertEqual(question, {
            "invocation_id": "invoke-1",
            "questions": [{
                "id": "0",
                "producer_id": "account",
                "header": "",
                "question": "Which account should I use?",
                "options": [],
                "multi_select": False,
                "image_url": "",
            }],
        })
        method, url, kwargs = client.calls[0]
        self.assertEqual(method, "GET")
        self.assertIn("conversationId=t%3Au%3Aconversation&since=0", url)
        self.assertNotIn("Last-Event-Id", kwargs["headers"])

    def test_it_discards_completed_prior_turn_text(self):
        client = _Client([
            "event: aq",
            'data: {"iid":"old","q":[{"id":"old","question":"Old?"}]}',
            "event: aa", 'data: {"iid":"old","answers":{"0":"Done"}}',
            "event: rl", 'data: {"st":"ok"}',
            "event: aq",
            'data: {"iid":"new","q":[{"id":"choice","question":"Choose A or B?"}]}',
            "event: rl", 'data: {"st":"needs_user_input"}',
            "event: rpc", 'data: {"cnt":5,"ts":1}',
        ])
        cr._api_http_client_fn = lambda: client
        got = cr.read_blocked_question("t:u:conversation")
        self.assertEqual(got["invocation_id"], "new")
        self.assertEqual(got["questions"][0]["question"], "Choose A or B?")

    def test_it_returns_the_latest_still_pending_question(self):
        client = _Client([
            "event: aq",
            'data: {"iid":"old","q":[{"id":"old","question":"Old question?"}]}',
            "event: rl", 'data: {"st":"needs_user_input"}',
            "event: aa", 'data: {"iid":"old","answers":{"0":"Done"}}',
            "event: rl", 'data: {"st":"ok"}',
            "event: aq",
            'data: {"iid":"current","q":[{"id":"current","question":"Current question?"}]}',
            "event: rl", 'data: {"st":"needs_user_input"}',
            "event: rpc", 'data: {"cnt":6,"ts":1}',
        ])
        cr._api_http_client_fn = lambda: client
        got = cr.read_blocked_question("t:u:conversation")
        self.assertEqual(got["invocation_id"], "current")
        self.assertEqual(got["questions"][0]["question"], "Current question?")

    def test_no_blocked_state_returns_none(self):
        client = _Client([
            "event: aq",
            'data: {"iid":"done","q":[{"id":"done","question":"Finished?"}]}',
            "event: aa", 'data: {"iid":"done","answers":{"0":"Done"}}',
            "event: rl", 'data: {"st":"ok"}',
            "event: rpc", 'data: {"cnt":3,"ts":1}',
        ])
        cr._api_http_client_fn = lambda: client
        self.assertIsNone(cr.read_blocked_question("t:u:conversation"))

    def test_ok_run_lifecycle_does_not_clear_a_pending_question(self):
        client = _Client([
            "event: aq",
            'data: {"iid":"pending","q":[{"question":"Still pending?"}]}',
            "event: rl", 'data: {"st":"ok"}',
            "event: rpc", 'data: {"cnt":2,"ts":1}',
        ])
        cr._api_http_client_fn = lambda: client
        got = cr.read_blocked_question("t:u:conversation")
        self.assertEqual(got["invocation_id"], "pending")

    def test_rpc_returns_the_replay_snapshot_before_live_events(self):
        client = _Client([
            "event: aq",
            'data: {"iid":"snapshot","q":[{"question":"Current?"}]}',
            "event: rpc", 'data: {"cnt":1,"ts":1}',
            "event: aq",
            'data: {"iid":"live","q":[{"question":"Too late"}]}',
        ])
        cr._api_http_client_fn = lambda: client
        got = cr.read_blocked_question("t:u:conversation")
        self.assertEqual(got["invocation_id"], "snapshot")

    def test_terminal_fail_closes_replay_without_rpc(self):
        client = _Client([
            "event: aq",
            'data: {"iid":"failed","q":[{"question":"Never answer"}]}',
            "event: rl", 'data: {"st":"fail"}',
        ])
        cr._api_http_client_fn = lambda: client
        self.assertIsNone(cr.read_blocked_question("t:u:conversation"))

    def test_it_uses_web_client_answer_keys_and_preserves_producer_ids(self):
        client = _Client([
            "event: aq",
            'data: {"iid":"multi","q":['
            '{"id":"account","header":"","question":"Which account?",'
            '"imageUrl":"https://images.example.test/account.png",'
            '"options":[{"label":"A","description":"Primary",'
            '"imageUrl":"https://images.example.test/a.png"},{"label":"B"}]},'
            '{"id":"reason","question":"Why?","multiSelect":true,'
            '"options":[{"label":"Because A"},{"label":"Because B"}]}]}',
            "event: rpc", 'data: {"cnt":1,"ts":1}',
        ])
        cr._api_http_client_fn = lambda: client
        got = cr.read_blocked_question("t:u:conversation")
        self.assertEqual(
            [question["id"] for question in got["questions"]],
            ["0", "1"],
        )
        self.assertEqual(
            [question["producer_id"] for question in got["questions"]],
            ["account", "reason"],
        )
        self.assertEqual(got["questions"][0]["options"][0]["value"], "A")
        self.assertEqual(
            got["questions"][0]["options"][0]["image_url"],
            "https://images.example.test/a.png",
        )
        self.assertEqual(
            got["questions"][0]["image_url"],
            "https://images.example.test/account.png",
        )
        self.assertTrue(got["questions"][1]["multi_select"])

    def test_incomplete_replay_does_not_expose_an_unconfirmed_question(self):
        client = _Client([
            "event: aq",
            'data: {"iid":"partial","q":[{"question":"Maybe answered later?"}]}',
        ])
        cr._api_http_client_fn = lambda: client
        self.assertIsNone(cr.read_blocked_question("t:u:conversation"))

    def test_an_unreadable_replay_fails_soft(self):
        cr._api_http_client_fn = lambda: (_ for _ in ()).throw(
            RuntimeError("unavailable")
        )
        self.assertIsNone(cr.read_blocked_question("t:u:conversation"))


if __name__ == "__main__":
    unittest.main()
