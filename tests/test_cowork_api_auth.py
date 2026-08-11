"""Auth recovery on the API transport.

Auth expires SILENTLY. On the subprocess path that shows up as exit 1 with
EMPTY stdout and a hint only on stderr, and _collect (L1358-1404) recovers by
running `cowork auth login` and retrying the whole run once.

_collect_api had no equivalent. An expired token would surface as a generic
"Cowork run failed: ..." and strand the preview, which is the single biggest
correctness gap blocking the API transport from becoming the default.

The API expression of the same failure is different and must be recognised on
its own terms:

  - msal acquire_token_silent returns None or a dict with no access_token
    (refresh token expired or revoked)
  - the runtime answers 401/403 on POST /v1/subscribe

Both mean "re-authenticate", and neither should read as "Cowork is broken".
"""

import unittest
from unittest import mock

from src.services import cowork_runner as cr


class TestRecognisingAuthFailure(unittest.TestCase):
    def test_a_401_is_an_auth_failure(self):
        self.assertTrue(cr._is_auth_failure(RuntimeError(
            "POST /v1/subscribe failed: HTTP 401")))

    def test_a_403_is_an_auth_failure(self):
        self.assertTrue(cr._is_auth_failure(RuntimeError(
            "POST /v1/subscribe failed: HTTP 403")))

    def test_a_silent_msal_refusal_is_an_auth_failure(self):
        self.assertTrue(cr._is_auth_failure(cr.CoworkAuthExpired("no token")))

    def test_an_unrelated_error_is_not_an_auth_failure(self):
        self.assertFalse(cr._is_auth_failure(OSError("island unreachable")))

    def test_a_500_is_not_an_auth_failure(self):
        """Re-authenticating would not help and would waste a device-code
        prompt on what is actually a server problem."""
        self.assertFalse(cr._is_auth_failure(RuntimeError(
            "POST /v1/subscribe failed: HTTP 500")))


class TestRecoveryFlow(unittest.TestCase):
    """Mirrors _collect's behaviour: log in once, retry once, then give up."""

    def setUp(self):
        cr.reset_registry()
        self.addCleanup(cr.reset_registry)
        self._cost = cr._cost_snapshot_fn
        cr._cost_snapshot_fn = lambda: None
        self.addCleanup(lambda: setattr(cr, "_cost_snapshot_fn", self._cost))
        import tempfile, shutil
        self.tmp = tempfile.mkdtemp(prefix="cw-auth-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._precheck = cr.tenant_barrier_precheck
        cr.tenant_barrier_precheck = lambda **k: {"status": "ok", "reason": ""}
        self.addCleanup(lambda: setattr(cr, "tenant_barrier_precheck", self._precheck))

    def _run(self, runner, login):
        with mock.patch.object(cr, "api_transport_enabled", lambda: True), \
             mock.patch.object(cr, "_api_run_fn", runner), \
             mock.patch.object(cr, "_auth_login_fn", login):
            label = cr.start_preview(77001, "hi", log_dir=self.tmp)
            cr.wait_for(label, timeout=10)
        return cr.get_result(label)

    def test_auth_failure_triggers_a_login_and_a_retry(self):
        attempts = []

        def runner(prompt, config, on_progress, conversation_id=None):
            attempts.append(1)
            if len(attempts) == 1:
                raise cr.CoworkAuthExpired("token expired")
            return {"terminal_status": "ok", "text": "recovered",
                    "sse_events": [], "tool_trace": [],
                    "conversation_id": "t:u:c", "callback_exchanges": [],
                    "duration_seconds": None}

        logins = []

        def login(argv, **kw):
            logins.append(argv)
            return type("R", (), {"returncode": 0})()

        result = self._run(runner, login)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(len(logins), 1)
        self.assertIsNone(result["error"])
        self.assertIn("recovered", result["stdout"])

    def test_a_failed_login_reports_the_actionable_message(self):
        def runner(prompt, config, on_progress, conversation_id=None):
            raise cr.CoworkAuthExpired("token expired")

        def login(argv, **kw):
            return type("R", (), {"returncode": 1})()

        result = self._run(runner, login)
        self.assertTrue(result["auth_failed"])
        self.assertIn("cowork auth login", result["error"])

    def test_it_does_not_retry_forever(self):
        """One login, one retry. A token that is still bad after that is a
        real problem and must surface, not loop."""
        attempts = []

        def runner(prompt, config, on_progress, conversation_id=None):
            attempts.append(1)
            raise cr.CoworkAuthExpired("still expired")

        def login(argv, **kw):
            return type("R", (), {"returncode": 0})()

        result = self._run(runner, login)
        self.assertEqual(len(attempts), 2)
        self.assertTrue(result["auth_failed"])

    def test_a_non_auth_error_does_not_trigger_a_login(self):
        logins = []

        def runner(prompt, config, on_progress, conversation_id=None):
            raise OSError("island unreachable")

        def login(argv, **kw):
            logins.append(argv)
            return type("R", (), {"returncode": 0})()

        result = self._run(runner, login)
        self.assertEqual(logins, [])
        self.assertFalse(result["auth_failed"])
        self.assertIn("island unreachable", result["error"])


if __name__ == "__main__":
    unittest.main()

