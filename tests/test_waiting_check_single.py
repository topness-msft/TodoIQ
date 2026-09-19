"""A "Check Now" button on one card should check that one task.

`requestWaitingCheckSingle(taskId)` took a task id, ignored it, and called the
global check (static/js/dashboard.js:2525-2532). Clicking Check Now on a single
waiting task re-ran every waiting task in the list.

That was tolerable when the button was tucked under a summary. It stopped being
tolerable once the card gained a "Couldn't check" state, because the obvious
response to a failed check is to retry THAT task - and the button silently
re-ran all of them instead, each one a WorkIQ subprocess.

The direct waiting worker keeps one single-flight label for both paths: a
per-task run and a global run write the same rows and must not overlap.
"""

import json
import os
import tempfile
import unittest
from unittest import mock

import tornado.testing

import src.db as db_module
from src.app import make_app
from src.models import create_task


class TestSingleTaskWaitingCheck(tornado.testing.AsyncHTTPTestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.original_db_path = db_module.DB_PATH
        db_module.DB_PATH = self.tmp.name
        conn = db_module.get_connection()
        db_module.init_db(conn)
        conn.close()
        self.launched = []
        super().setUp()

    def tearDown(self):
        super().tearDown()
        db_module.DB_PATH = self.original_db_path
        os.unlink(self.tmp.name)

    def get_app(self):
        return make_app()

    def _fake_runner(self):
        def runner(task_id=None, *, skip_empty=False):
            self.launched.append({"task_id": task_id, "label": "waiting-check"})
            return {"ok": True, "message": "started"}
        return mock.Mock(launch=runner)

    def _post(self, body):
        with (
            mock.patch("src.handlers.sync_api.checks.get_waiting_checks", return_value=self._fake_runner()),
            mock.patch("src.handlers.sync_api.run_copilot", side_effect=AssertionError("CLI forbidden")),
        ):
            return self.fetch("/api/sync-status", method="POST",
                              body=json.dumps(body))

    def test_a_task_id_scopes_the_direct_run_to_that_task(self):
        task = create_task(title="Waiting on Jason", status="waiting")
        response = self._post({"waiting_check": True, "task_id": task["id"]})
        self.assertEqual(response.code, 200)
        self.assertEqual(len(self.launched), 1)
        self.assertEqual(self.launched[0]["task_id"], task["id"])

    def test_without_a_task_id_the_global_check_still_runs(self):
        response = self._post({"waiting_check": True})
        self.assertEqual(response.code, 200)
        self.assertIsNone(self.launched[0]["task_id"])

    def test_both_paths_share_one_label_so_they_cannot_overlap(self):
        # A per-task run and a global run write the same rows.
        task = create_task(title="Waiting on Jason", status="waiting")
        self._post({"waiting_check": True, "task_id": task["id"]})
        self._post({"waiting_check": True})
        self.assertEqual({entry["label"] for entry in self.launched},
                         {"waiting-check"})

    def test_a_single_task_check_gets_a_realistic_budget(self):
        """180s killed a real run before it could write anything.

        The cost of a check is WorkIQ latency, not task count: it chains a
        presence probe, a thread read and possibly a person-scoped fallback,
        and individual WorkIQ calls in this project have been measured at
        95-250s. When the subprocess is killed mid-run nothing is written at
        all, so the card keeps showing its previous answer under the previous
        timestamp - the confusion the check exists to remove.
        """
        from src.handlers.sync_api import SINGLE_WAITING_CHECK_TIMEOUT
        from src.services.checks import TARGET_TIMEOUT

        task = create_task(title="Waiting on Jason", status="waiting")
        self._post({"waiting_check": True, "task_id": task["id"]})
        self.assertEqual(self.launched[0]["task_id"], task["id"])
        self.assertEqual(TARGET_TIMEOUT, SINGLE_WAITING_CHECK_TIMEOUT)
        # Comfortably above the slowest single-task run observed (200s+).
        self.assertGreaterEqual(SINGLE_WAITING_CHECK_TIMEOUT, 300)

    def test_an_unknown_task_is_refused_rather_than_run_globally(self):
        # Falling back to the global check would spend a WorkIQ run per waiting
        # task in response to what is almost certainly a stale dashboard row.
        response = self._post({"waiting_check": True, "task_id": 999999})
        self.assertEqual(response.code, 404)
        self.assertEqual(self.launched, [])

    def test_a_non_numeric_task_id_is_refused(self):
        response = self._post({"waiting_check": True, "task_id": "; rm -rf /"})
        self.assertEqual(response.code, 400)
        self.assertEqual(self.launched, [])

    def test_the_direct_run_receives_a_typed_integer_id(self):
        task = create_task(title="Waiting on Jason", status="waiting")
        self._post({"waiting_check": True, "task_id": str(task["id"])})
        self.assertEqual(self.launched[0]["task_id"], task["id"])
        self.assertIs(type(self.launched[0]["task_id"]), int)


if __name__ == "__main__":
    unittest.main()
