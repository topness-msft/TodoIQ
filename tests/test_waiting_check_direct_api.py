"""Public waiting entry points retain their pre-migration HTTP contracts."""

import json
from unittest.mock import Mock, patch

import pytest
import tornado.testing
import tornado.web

from src import app, models
from src.handlers import sync_api
from src.services import checks, suggestion_checks, claude_runner
from tests.test_waiting_check_workflow import store, presence
from tests.test_suggestion_check_workflow import finish


@pytest.mark.usefixtures("store")
class WaitingCheckAPI(tornado.testing.AsyncHTTPTestCase):
    def get_app(self):
        return tornado.web.Application([
            (r"/api/sync-status", sync_api.SyncStatusHandler),
            (r"/api/runner-status", sync_api.RunnerStatusHandler),
        ])

    def setUp(self):
        super().setUp()
        self.runtime = Mock()
        self.runtime.execute_ask.return_value = {"answer": presence(True), "conversation_id": "PRIVATE"}
        self.worker = checks.WaitingChecks(runtime_provider=lambda: self.runtime)
        self.patches = [
            patch.object(checks, "get_waiting_checks", return_value=self.worker),
            patch.object(claude_runner, "run_copilot", side_effect=AssertionError("CLI forbidden")),
            patch.object(sync_api, "demo_mode", return_value=False),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        if self.worker._thread:
            finish(self.worker)
        for item in reversed(self.patches):
            item.stop()
        super().tearDown()

    def post(self, **body):
        return self.fetch("/api/sync-status", method="POST", body=json.dumps({"waiting_check": True, **body}))

    def test_targeted_int_and_numeric_string_launch_exact_any_status(self):
        task = models.create_task(title="Synthetic target", status="completed", key_people="[]")
        for value in (task["id"], str(task["id"]), " " + str(task["id"]) + " "):
            response = self.post(task_id=value)
            assert response.code == 200
            result = json.loads(response.body)
            assert result["ok"] and result["run_id"] and result["started_at"]
            assert finish(self.worker)["outcome"] == "succeeded"
            assert models.get_task(task["id"])["status"] == "completed"
        assert not self.runtime.mock_calls
        claude_runner.run_copilot.assert_not_called()

    def test_global_launch_and_missing_or_invalid_never_broaden(self):
        for value in ("not-id", True, {}, 2.5, "1;evil"):
            assert self.post(task_id=value).code == 400
        for value in (0, -1, 99999):
            assert self.post(task_id=value).code == 404
        assert self.worker._thread is None
        result = self.post()
        assert result.code == 200 and json.loads(result.body)["ok"]
        assert finish(self.worker)["outcome"] == "succeeded"
        claude_runner.run_copilot.assert_not_called()

    def test_targeted_global_busy_remains_200_ok_false_and_other_failure_500(self):
        task = models.create_task(title="Synthetic target")
        with patch.object(self.worker, "launch", return_value={"ok": False, "message": "Waiting check already running."}):
            for body in ({}, {"task_id": task["id"]}):
                response = self.post(**body)
                assert response.code == 200 and not json.loads(response.body)["ok"]
        with patch.object(self.worker, "launch", return_value={"ok": False, "message": "Could not start the waiting check."}):
            assert self.post().code == 500
        assert self.worker._thread is None

    def test_all_waiting_entrypoints_avoid_cli_on_provider_failure(self):
        task = models.create_task(title="Synthetic waiting", status="waiting",
                                  key_people='[{"email":"target@example.test"}]')
        self.runtime.execute_ask.side_effect = ValueError("PRIVATE")
        for body in ({"task_id": task["id"]}, {}):
            assert self.post(**body).code == 200
            assert finish(self.worker)["outcome"] == "failed"
        app._check_waiting()
        assert finish(self.worker)["outcome"] == "failed"
        assert self.runtime.execute_ask.call_count == 3
        claude_runner.run_copilot.assert_not_called()

    def test_both_labels_merged_with_flat_runs_completions_and_queue(self):
        legacy = {"sync": True, "parse": True, "skill:prepare:7": True,
                  "suggestion-check": True, "waiting-check": True,
                  "_runs": {key: {"run_id": "legacy"} for key in ("sync", "parse", "skill:prepare:7", "suggestion-check", "waiting-check")}}
        done = {key: {"run_id": "legacy-done"} for key in legacy if key != "_runs"}
        suggestion = Mock()
        suggestion.status.return_value = {"suggestion-check": True, "_runs": {"suggestion-check": {"run_id": "suggestion"}}}
        suggestion.completion.return_value = {"run_id": "suggestion-done", "exit_code": 0}
        with (
            patch.object(sync_api, "get_status", return_value=legacy),
            patch.object(sync_api, "get_exit_info", return_value=done),
            patch.object(checks, "get_checks", return_value=suggestion),
            patch.object(self.worker, "status", return_value={"waiting-check": True, "_runs": {"waiting-check": {"run_id": "waiting"}}}),
            patch.object(self.worker, "completion", return_value={"run_id": "waiting-done", "exit_code": 1}),
        ):
            payload = json.loads(self.fetch("/api/runner-status").body)
        assert payload["_runs"]["waiting-check"]["run_id"] == "waiting"
        assert payload["_runs"]["suggestion-check"]["run_id"] == "suggestion"
        assert payload["_completed"]["waiting-check"]["run_id"] == "waiting-done"
        assert payload["_completed"]["suggestion-check"]["run_id"] == "suggestion-done"
        for label in ("sync", "parse"):
            assert label not in payload and label not in payload["_runs"]
            assert payload["_completed"].get(label) != done[label]
        for label in ("skill:prepare:7",):
            assert payload[label] and payload["_runs"][label] == legacy["_runs"][label]
            assert payload["_completed"][label] == done[label]
        assert payload["waiting-check"] and payload["suggestion-check"]
        assert "_suggestion_check_queue" in payload


def test_idle_direct_labels_remove_stale_legacy_runs_and_completions(monkeypatch):
    for name in ("get_checks", "get_waiting_checks"):
        fake = Mock()
        fake.status.return_value = {"_runs": {}}
        fake.completion.return_value = None
        monkeypatch.setattr(checks, name, lambda fake=fake: fake)
    legacy = {"sync": True, "suggestion-check": True, "waiting-check": True,
              "_runs": {"sync": {"run_id": "keep"}, "suggestion-check": {}, "waiting-check": {}}}
    reader = Mock(return_value=legacy)
    monkeypatch.setattr(suggestion_checks, "get_status", reader)
    assert suggestion_checks._check_status() == {"_runs": {}}
    reader.assert_called_once_with()
    assert checks.merged_completions({"sync": {}, "suggestion-check": {}, "waiting-check": {}}) == {"sync": {}}
