"""Focused API and slash-command contracts for scoped suggestion re-check."""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, mock_open, patch
from uuid import UUID

import pytest
import tornado.testing
import tornado.web

from src.handlers.sync_api import RunnerStatusHandler, SyncStatusHandler
from src.services import claude_runner
from src.services.suggestion_checks import (
    SuggestionCheckQueue,
    _failed_suggestion_task_ids,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMMAND_PATH = PROJECT_ROOT / ".claude" / "commands" / "suggestion-check.md"
BUSY_MESSAGE = "A suggestion check is already running. Try again when it finishes."
RUN_A = "11111111-1111-4111-8111-111111111111"
RUN_B = "22222222-2222-4222-8222-222222222222"
START_A = "2026-09-10T14:00:00Z"
START_B = "2026-09-10T15:00:00Z"
FINISH_A = "2026-09-10T14:01:00Z"


def _assert_utc(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.utcoffset().total_seconds() == 0


@pytest.fixture
def clean_runner_state():
    maps = (
        claude_runner._processes,
        claude_runner._log_files,
        claude_runner._start_times,
        claude_runner._timeouts,
        claude_runner._runs,
        claude_runner._exit_info,
    )
    for mapping in maps:
        mapping.clear()
    yield
    for mapping in maps:
        mapping.clear()


def test_run_copilot_returns_unique_uuid_and_utc_started_at_for_each_actual_run(
    clean_runner_state,
):
    proc_a = Mock(pid=101, returncode=0)
    proc_a.poll.return_value = None
    proc_b = Mock(pid=102, returncode=0)
    proc_b.poll.return_value = None

    with (
        patch.object(claude_runner, "copilot_command_enabled", return_value=True),
        patch.object(claude_runner.subprocess, "Popen", side_effect=[proc_a, proc_b]),
        patch("builtins.open", mock_open()),
        patch.object(Path, "mkdir"),
    ):
        first = claude_runner.run_copilot("/suggestion-check", "suggestion-check")
        active_first = claude_runner.get_status()
        claude_runner._processes.clear()
        claude_runner._cleanup("suggestion-check")
        second = claude_runner.run_copilot("/suggestion-check", "suggestion-check")

    assert first["ok"] is True
    assert second["ok"] is True
    UUID(first["run_id"])
    UUID(second["run_id"])
    assert first["run_id"] != second["run_id"]
    _assert_utc(first["started_at"])
    _assert_utc(second["started_at"])
    assert active_first["_runs"]["suggestion-check"] == {
        "run_id": first["run_id"],
        "started_at": first["started_at"],
    }


def test_record_exit_preserves_exact_identity_and_immutable_finish_time(
    clean_runner_state,
):
    claude_runner._runs["suggestion-check"] = {
        "run_id": RUN_A,
        "started_at": START_A,
    }

    recorded = claude_runner._record_exit("suggestion-check", 0, None)
    first = claude_runner.get_exit_info("suggestion-check")
    second = claude_runner.get_exit_info("suggestion-check")

    assert recorded == first == second
    assert first["run_id"] == RUN_A
    assert first["started_at"] == START_A
    assert first["exit_code"] == 0
    assert first["error"] is None
    _assert_utc(first["finished_at"])


def test_identical_successive_successes_remain_distinguishable(
    clean_runner_state,
):
    claude_runner._runs["suggestion-check"] = {
        "run_id": RUN_A,
        "started_at": START_A,
    }
    first = claude_runner._record_exit("suggestion-check", 0, None)
    claude_runner._runs["suggestion-check"] = {
        "run_id": RUN_B,
        "started_at": START_B,
    }
    second = claude_runner._record_exit("suggestion-check", 0, None)

    assert first["exit_code"] == second["exit_code"] == 0
    assert first["error"] is second["error"] is None
    assert first["run_id"] == RUN_A
    assert second["run_id"] == RUN_B
    assert first != second


class SuggestionCheckAPITest(tornado.testing.AsyncHTTPTestCase):
    def get_app(self):
        return tornado.web.Application([
            (r"/api/sync-status", SyncStatusHandler),
            (r"/api/runner-status", RunnerStatusHandler),
        ])

    def post(self, body):
        return self.fetch(
            "/api/sync-status",
            method="POST",
            headers={"Content-Type": "application/json"},
            body=json.dumps(body),
        )

    @patch("src.handlers.sync_api.demo_mode", return_value=False)
    @patch("src.handlers.sync_api.is_running", return_value=False)
    @patch("src.handlers.sync_api.get_task")
    @patch("src.handlers.sync_api.run_copilot")
    def test_scoped_check_enqueues_exact_task(
        self, run_copilot, get_task, _is_running, _demo_mode
    ):
        get_task.return_value = {"id": 2693, "status": "suggested"}

        response = self.post({"suggestion_check": True, "task_id": 2693})

        assert response.code == 202
        payload = json.loads(response.body)
        assert payload["ok"] is True
        job = payload["suggestion_check_job"]
        assert job["task_id"] == 2693
        assert job["state"] == "queued"
        assert job["queue_position"] == 1
        assert job["run_id"] is None
        run_copilot.assert_not_called()

    @patch("src.handlers.sync_api.demo_mode", return_value=False)
    @patch("src.handlers.sync_api.is_running", return_value=False)
    @patch("src.handlers.sync_api.get_connection")
    @patch("src.handlers.sync_api.run_copilot")
    def test_global_check_keeps_existing_budget(
        self, run_copilot, get_connection, _is_running, _demo_mode
    ):
        conn = Mock()
        conn.execute.return_value.fetchone.return_value = (3,)
        get_connection.return_value = conn
        run_copilot.return_value = {
            "ok": True,
            "message": "started",
            "run_id": RUN_B,
            "started_at": START_B,
        }
        response = self.post({"suggestion_check": True})

        assert response.code == 200
        payload = json.loads(response.body)
        assert "suggestion_check_job" not in payload
        run_copilot.assert_called_once_with(
            "/suggestion-check",
            label="suggestion-check",
            timeout=300,
        )
        conn.close.assert_called_once_with()

    @patch("src.handlers.sync_api.demo_mode", return_value=False)
    @patch("src.handlers.sync_api.is_running", return_value=True)
    @patch("src.handlers.sync_api.get_task")
    @patch("src.handlers.sync_api.run_copilot")
    def test_targeted_request_queues_while_global_is_running(
        self, run_copilot, get_task, _is_running, _demo_mode
    ):
        get_task.return_value = {"id": 2693, "status": "suggested"}

        response = self.post({"suggestion_check": True, "task_id": 2693})

        assert response.code == 202
        assert json.loads(response.body)["suggestion_check_job"]["state"] == "queued"
        run_copilot.assert_not_called()

    @patch("src.handlers.sync_api.demo_mode", return_value=False)
    @patch("src.handlers.sync_api.is_running", return_value=True)
    @patch("src.handlers.sync_api.run_copilot")
    def test_global_request_is_refused_while_target_is_running(
        self, run_copilot, _is_running, _demo_mode
    ):
        self._app.suggestion_check_queue = Mock()
        self._app.suggestion_check_queue.has_work.return_value = True
        response = self.post({"suggestion_check": True})

        assert response.code == 409
        assert json.loads(response.body) == {"ok": False, "message": BUSY_MESSAGE}
        run_copilot.assert_not_called()

    @patch("src.handlers.sync_api.demo_mode", return_value=False)
    @patch("src.handlers.sync_api.is_running", return_value=False)
    @patch("src.handlers.sync_api.get_connection")
    @patch("src.handlers.sync_api.run_copilot")
    def test_raced_global_already_running_result_is_one_valid_409_response(
        self, run_copilot, get_connection, _is_running, _demo_mode
    ):
        conn = Mock()
        conn.execute.return_value.fetchone.return_value = (1,)
        get_connection.return_value = conn
        run_copilot.return_value = {
            "ok": False,
            "message": "'suggestion-check' already running.",
        }

        response = self.post({"suggestion_check": True})

        assert response.code == 409
        assert json.loads(response.body) == {
            "ok": False,
            "message": BUSY_MESSAGE,
        }

    @patch("src.handlers.sync_api.demo_mode", return_value=False)
    @patch("src.handlers.sync_api.is_running", return_value=False)
    @patch("src.handlers.sync_api.get_task")
    @patch("src.handlers.sync_api.run_copilot")
    def test_invalid_present_ids_never_fall_through_to_global(
        self, run_copilot, get_task, _is_running, _demo_mode
    ):
        invalid = [None, True, False, 0, -1, 2.5, "2693", "abc", ""]
        for value in invalid:
            with self.subTest(value=value):
                response = self.post({
                    "suggestion_check": True,
                    "task_id": value,
                })
                assert response.code == 400
                assert "positive integer" in json.loads(response.body)["error"]
        get_task.assert_not_called()
        run_copilot.assert_not_called()

    @patch("src.handlers.sync_api.demo_mode", return_value=False)
    @patch("src.handlers.sync_api.is_running", return_value=False)
    @patch("src.handlers.sync_api.get_task", return_value=None)
    @patch("src.handlers.sync_api.run_copilot")
    def test_missing_task_is_404_without_launch(
        self, run_copilot, _get_task, _is_running, _demo_mode
    ):
        response = self.post({"suggestion_check": True, "task_id": 999999})

        assert response.code == 404
        run_copilot.assert_not_called()

    @patch("src.handlers.sync_api.demo_mode", return_value=False)
    @patch("src.handlers.sync_api.is_running", return_value=False)
    @patch(
        "src.handlers.sync_api.get_task",
        return_value={"id": 2693, "status": "active"},
    )
    @patch("src.handlers.sync_api.run_copilot")
    def test_non_suggested_task_is_409_without_launch(
        self, run_copilot, _get_task, _is_running, _demo_mode
    ):
        response = self.post({"suggestion_check": True, "task_id": 2693})

        assert response.code == 409
        assert "no longer suggested" in json.loads(response.body)["error"].lower()
        run_copilot.assert_not_called()

    @patch("src.handlers.sync_api.demo_mode", return_value=False)
    @patch("src.handlers.sync_api.is_running", return_value=False)
    @patch(
        "src.handlers.sync_api.get_task",
        return_value={"id": 2693, "status": "suggested"},
    )
    @patch("src.handlers.sync_api.run_copilot")
    def test_duplicate_targeted_request_returns_same_accepted_job(
        self, run_copilot, _get_task, _is_running, _demo_mode
    ):
        first = self.post({"suggestion_check": True, "task_id": 2693})
        second = self.post({"suggestion_check": True, "task_id": 2693})

        assert first.code == second.code == 202
        first_job = json.loads(first.body)["suggestion_check_job"]
        second_job = json.loads(second.body)["suggestion_check_job"]
        assert first_job["job_id"] == second_job["job_id"]
        run_copilot.assert_not_called()

    @patch("src.handlers.sync_api.demo_mode", return_value=False)
    @patch("src.handlers.sync_api.is_running", return_value=False)
    @patch(
        "src.handlers.sync_api.get_task",
        return_value={"id": 2693, "status": "suggested"},
    )
    @patch("src.handlers.sync_api.run_copilot")
    def test_targeted_queue_capacity_returns_429(
        self, run_copilot, _get_task, _is_running, _demo_mode
    ):
        self._app.suggestion_check_queue = Mock()
        from src.services.suggestion_checks import QueueFull
        self._app.suggestion_check_queue.enqueue.side_effect = QueueFull

        response = self.post({"suggestion_check": True, "task_id": 2693})

        assert response.code == 429
        assert "queue is full" in json.loads(response.body)["message"].lower()
        run_copilot.assert_not_called()

    @patch("src.handlers.sync_api.get_exit_info")
    @patch("src.handlers.sync_api.get_status")
    def test_queue_snapshot_is_non_destructive_for_multiple_pollers(
        self, get_status, get_exit_info
    ):
        get_status.return_value = {"_runs": {}}
        get_exit_info.return_value = {}
        with patch("src.handlers.sync_api.get_task", return_value={
            "id": 2693, "status": "suggested"
        }):
            accepted = self.post(
                {"suggestion_check": True, "task_id": 2693}
            )
        job = json.loads(accepted.body)["suggestion_check_job"]

        first = json.loads(self.fetch("/api/runner-status").body)
        second = json.loads(self.fetch("/api/runner-status").body)
        assert first["_suggestion_check_queue"] == second["_suggestion_check_queue"]
        assert first["_suggestion_check_queue"]["pending"][0]["job_id"] == job["job_id"]

    @patch("src.handlers.sync_api.get_exit_info")
    @patch("src.handlers.sync_api.get_status")
    def test_runner_status_keeps_flat_runner_data_beside_queue_snapshot(
        self, get_status, get_exit_info
    ):
        get_status.return_value = {
            "suggestion-check": True,
            "_runs": {
                "suggestion-check": {
                    "run_id": RUN_B,
                    "started_at": START_B,
                },
            },
        }
        get_exit_info.return_value = {
            "suggestion-check": {
                "run_id": RUN_B,
                "started_at": START_B,
                "finished_at": "2026-09-10T15:02:00Z",
                "exit_code": 0,
                "error": None,
            },
        }

        payload = json.loads(self.fetch("/api/runner-status").body)

        assert payload["suggestion-check"] is True
        assert payload["_runs"]["suggestion-check"]["run_id"] == RUN_B
        assert payload["_completed"]["suggestion-check"]["run_id"] == RUN_B
        assert payload["_suggestion_check_queue"]["pending"] == []


class TestSuggestionCheckCommandContract:
    @classmethod
    def setup_class(cls):
        cls.text = COMMAND_PATH.read_text(encoding="utf-8")

    def test_uses_the_configured_workiq_alias(self):
        assert "workiq-ask" in self.text
        assert "ask_work_iq" not in self.text

    def test_validates_optional_digit_task_id_without_broad_fallback(self):
        assert "$ARGUMENTS" in self.text
        assert "isdigit()" in self.text
        assert "int(raw_id) <= 0" in self.text
        assert "do not fall back to the global query" in self.text.lower()

    def test_targeted_read_and_writes_are_status_constrained(self):
        targeted_query = self.text.split("if raw_id:", 1)[1].split("else:", 1)[0]
        assert "WHERE id = ? AND status = 'suggested'" in targeted_query
        assert (
            "UPDATE tasks SET waiting_activity = ?, updated_at = ? "
            "WHERE id = ? AND status = 'suggested'"
        ) in self.text
        assert (
            "SELECT user_notes FROM tasks WHERE id = ? AND status = 'suggested'"
        ) in self.text
        assert (
            "UPDATE tasks SET user_notes = ?, updated_at = ? "
            "WHERE id = ? AND status = 'suggested'"
        ) in self.text

    def test_global_query_keeps_unchecked_first_ordering(self):
        assert "WHERE status = 'suggested'" in self.text
        assert (
            "ORDER BY CASE WHEN waiting_activity IS NULL THEN 0 ELSE 1 END, "
            "created_at DESC"
        ) in self.text


class PostSyncHarness:
    def __init__(self):
        self.active = {}
        self.completions = {}
        self.marker = None
        self.failed_ids = []
        self.tasks = {}
        self.launches = []
        self.enabled = True

    def get_status(self):
        return {"_runs": dict(self.active)}

    def get_exit_info(self, label):
        return self.completions.get(label)

    def get_marker(self):
        return dict(self.marker) if self.marker else None

    def get_failed_ids(self):
        return list(self.failed_ids)

    def get_task(self, task_id):
        return self.tasks.get(task_id)

    def launch(self, command, *, label, timeout):
        task_id = int(command.rsplit(" ", 1)[1])
        self.launches.append(task_id)
        run = {
            "run_id": f"33333333-3333-4333-8333-{task_id:012d}",
            "started_at": START_A,
        }
        self.active[label] = run
        return {"ok": True, **run}

    def queue(self, *, max_pending=100):
        return SuggestionCheckQueue(
            launcher=self.launch,
            status_reader=self.get_status,
            completion_reader=self.get_exit_info,
            task_reader=self.get_task,
            full_scan_reader=self.get_marker,
            failed_task_reader=self.get_failed_ids,
            max_pending=max_pending,
        )

    def initialize(self, queue):
        queue.initialize_post_sync(lambda: self.enabled)

    def complete_sync(
        self,
        run_id,
        *,
        started_at=START_A,
        finished_at=FINISH_A,
        exit_code=0,
        error=None,
        marker_id=1,
        marker_time="2026-09-10T14:00:30Z",
    ):
        self.completions["sync"] = {
            "run_id": run_id,
            "started_at": started_at,
            "finished_at": finished_at,
            "exit_code": exit_code,
            "error": error,
        }
        self.marker = (
            None
            if marker_id is None
            else {
                "id": marker_id,
                "sync_type": "full_scan",
                "synced_at": marker_time,
            }
        )


def _failed_task(task_id):
    return {
        "id": task_id,
        "status": "suggested",
        "waiting_signal": {
            "activity": {
                "producer": "suggestion-check",
                "check_state": "failed",
                "status": None,
                "checked_at": "2026-09-10T13:00:00Z",
            },
        },
    }


def test_post_sync_startup_watermarks_completion_and_full_scan_without_replay():
    h = PostSyncHarness()
    h.failed_ids = [1]
    h.tasks[1] = _failed_task(1)
    h.complete_sync(RUN_A, marker_id=9)
    queue = h.queue()

    h.initialize(queue)
    queue.pump_once()
    queue.pump_once()

    assert h.launches == []
    assert queue.snapshot()["last_post_sync_recheck"] is None


def test_post_sync_fast_completion_between_ticks_enqueues_failed_rows_once():
    h = PostSyncHarness()
    h.failed_ids = [7, 8]
    h.tasks = {task_id: _failed_task(task_id) for task_id in h.failed_ids}
    queue = h.queue()
    h.initialize(queue)
    h.complete_sync(RUN_A, marker_id=1)

    queue.pump_once()
    queue.pump_once()

    snapshot = queue.snapshot()
    assert h.launches == [7]
    assert [job["task_id"] for job in snapshot["pending"]] == [8]
    assert snapshot["last_post_sync_recheck"] == {
        "run_id": RUN_A,
        "sync_finished_at": FINISH_A,
        "processed_at": snapshot["last_post_sync_recheck"]["processed_at"],
        "accepted": 2,
        "deduped": 0,
        "overflow": 0,
        "skipped": 0,
        "error": None,
    }


def test_post_sync_same_second_full_scan_requires_new_id():
    h = PostSyncHarness()
    h.failed_ids = [1]
    h.tasks[1] = _failed_task(1)
    h.complete_sync(RUN_A, marker_id=4, marker_time="2026-09-10T14:00:00Z")
    queue = h.queue()
    h.initialize(queue)

    h.complete_sync(RUN_B, marker_id=5, marker_time="2026-09-10T14:00:00Z")
    queue.pump_once()
    assert h.launches == [1]

    h.active.clear()
    h.complete_sync(
        "44444444-4444-4444-8444-444444444444",
        marker_id=5,
        marker_time="2026-09-10T14:00:00Z",
    )
    queue.pump_once()
    h.marker["id"] = 6
    queue.pump_once()

    assert h.launches == [1]
    assert queue.snapshot()["last_post_sync_recheck"]["run_id"].startswith("4444")
    assert queue.snapshot()["last_post_sync_recheck"]["accepted"] == 0


@pytest.mark.parametrize(
    "completion_overrides,marker_overrides",
    [
        ({"exit_code": 7}, {}),
        ({"error": "private failure"}, {}),
        ({}, {"marker_id": None}),
        ({}, {"marker_time": "2026-09-10T13:59:59Z"}),
        ({}, {"marker_time": "2026-09-10T14:01:01Z"}),
    ],
)
def test_post_sync_invalid_completion_or_marker_is_consumed_without_replay(
    completion_overrides, marker_overrides
):
    h = PostSyncHarness()
    h.failed_ids = [1]
    h.tasks[1] = _failed_task(1)
    queue = h.queue()
    h.initialize(queue)
    values = {
        "run_id": RUN_A,
        "exit_code": 0,
        "error": None,
        "marker_id": 1,
        "marker_time": "2026-09-10T14:00:30Z",
        **completion_overrides,
        **marker_overrides,
    }
    h.complete_sync(**values)

    queue.pump_once()
    h.complete_sync(RUN_A, marker_id=2)
    queue.pump_once()

    assert h.launches == []
    assert queue.snapshot()["last_post_sync_recheck"]["run_id"] == RUN_A


def test_post_sync_disabled_completion_is_consumed_without_later_replay():
    h = PostSyncHarness()
    h.failed_ids = [1]
    h.tasks[1] = _failed_task(1)
    queue = h.queue()
    h.initialize(queue)
    h.enabled = False
    h.complete_sync(RUN_A, marker_id=1)

    queue.pump_once()
    h.enabled = True
    queue.pump_once()

    assert h.launches == []
    report = queue.snapshot()["last_post_sync_recheck"]
    assert report["run_id"] == RUN_A
    assert report["accepted"] == report["deduped"] == report["overflow"] == 0


def test_post_sync_failed_completion_retires_marker_before_next_run():
    h = PostSyncHarness()
    h.failed_ids = [1]
    h.tasks[1] = _failed_task(1)
    queue = h.queue()
    h.initialize(queue)

    h.complete_sync(RUN_A, exit_code=7, marker_id=1)
    queue.pump_once()
    h.complete_sync(RUN_B, marker_id=1)
    queue.pump_once()
    h.marker["id"] = 2
    queue.pump_once()

    assert h.launches == []
    assert queue.snapshot()["last_post_sync_recheck"]["run_id"] == RUN_B


@pytest.mark.parametrize("reader_name", ["full_scan", "failed_tasks"])
def test_post_sync_reader_failure_is_safe_and_consumed_once(reader_name):
    h = PostSyncHarness()
    h.failed_ids = [1]
    h.tasks[1] = _failed_task(1)
    queue = h.queue()
    h.initialize(queue)
    h.complete_sync(RUN_A, marker_id=1)
    if reader_name == "full_scan":
        queue._full_scan_reader = Mock(side_effect=RuntimeError("private marker"))
    else:
        queue._failed_task_reader = Mock(side_effect=RuntimeError("private task"))

    queue.pump_once()
    if reader_name == "full_scan":
        queue._full_scan_reader = h.get_marker
    else:
        queue._failed_task_reader = h.get_failed_ids
    queue.pump_once()

    report = queue.snapshot()["last_post_sync_recheck"]
    assert h.launches == []
    assert report["run_id"] == RUN_A
    assert report["accepted"] == 0
    assert report["error"].startswith("Could not")
    assert "private" not in report["error"]


def test_post_sync_selects_all_normalized_failed_rows_without_200_cap(tmp_path):
    path = tmp_path / "selection.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE tasks (id INTEGER PRIMARY KEY, status TEXT, waiting_activity TEXT)"
    )
    success = json.dumps({
        "producer": "suggestion-check",
        "check_state": "ok",
        "status": "still_pending",
    })
    conn.executemany(
        "INSERT INTO tasks VALUES (?, 'suggested', ?)",
        [(task_id, success) for task_id in range(1, 206)],
    )
    rows = [
        (206, "suggested", json.dumps({
            "producer": "suggestion-check", "check_state": "failed",
        })),
        (207, "suggested", json.dumps({
            "check_state": "failed", "status": "unclear",
        })),
        (208, "suggested", "{bad json"),
        (209, "suggested", json.dumps({
            "producer": "waiting-check", "check_state": "failed",
        })),
        (210, "suggested", json.dumps({
            "producer": "todo-parse", "check_state": "failed",
        })),
        (211, "active", json.dumps({
            "producer": "suggestion-check", "check_state": "failed",
        })),
    ]
    conn.executemany("INSERT INTO tasks VALUES (?, ?, ?)", rows)
    conn.commit()
    conn.close()

    selected = _failed_suggestion_task_ids(
        connection_factory=lambda: sqlite3.connect(path)
    )

    assert selected == [206, 207]


def test_post_sync_distinct_successes_retry_still_failed_task_once_each():
    h = PostSyncHarness()
    h.failed_ids = [1]
    h.tasks[1] = _failed_task(1)
    queue = h.queue()
    h.initialize(queue)

    h.complete_sync(RUN_A, marker_id=1)
    queue.pump_once()
    first_job = queue.job_for_task(1)["job_id"]
    h.active.clear()
    h.completions["suggestion-check"] = {
        "run_id": f"33333333-3333-4333-8333-{1:012d}",
        "started_at": START_A,
        "finished_at": FINISH_A,
        "exit_code": 0,
        "error": None,
    }
    queue.pump_once()

    h.complete_sync(RUN_B, marker_id=2)
    queue.pump_once()
    second_job = queue.job_for_task(1)["job_id"]
    queue.pump_once()

    assert h.launches == [1, 1]
    assert second_job != first_job


def test_post_sync_batch_appends_fifo_dedupes_and_reports_overflow():
    h = PostSyncHarness()
    h.tasks = {task_id: _failed_task(task_id) for task_id in range(1, 6)}
    queue = h.queue(max_pending=3)
    h.initialize(queue)
    queue.enqueue(1)
    queue.enqueue(2)
    queue.pump_once()
    h.failed_ids = [1, 3, 4, 5, 2]
    h.complete_sync(RUN_A, marker_id=1)

    queue.pump_once()

    snapshot = queue.snapshot()
    assert snapshot["active"]["task_id"] == 1
    assert [job["task_id"] for job in snapshot["pending"]] == [2, 3, 4]
    assert snapshot["last_post_sync_recheck"]["accepted"] == 2
    assert snapshot["last_post_sync_recheck"]["deduped"] == 2
    assert snapshot["last_post_sync_recheck"]["overflow"] == 1


def test_post_sync_auto_job_skips_fixed_row_but_manual_recheck_still_launches():
    h = PostSyncHarness()
    h.failed_ids = [1]
    h.tasks[1] = _failed_task(1)
    queue = h.queue()
    h.initialize(queue)
    h.active["suggestion-check"] = {
        "run_id": "55555555-5555-4555-8555-555555555555",
        "started_at": START_A,
    }
    h.complete_sync(RUN_A, marker_id=1)

    queue.pump_once()
    h.tasks[1] = {
        "id": 1,
        "status": "suggested",
        "waiting_signal": {
            "activity": {
                "producer": "suggestion-check",
                "check_state": "ok",
                "status": "still_pending",
                "checked_at": FINISH_A,
            },
        },
    }
    h.active.clear()
    queue.pump_once()

    assert h.launches == []
    assert queue.job_for_task(1)["state"] == "skipped"
    assert queue.snapshot()["last_post_sync_recheck"]["skipped"] == 1

    queue.enqueue(1)
    queue.pump_once()
    assert h.launches == [1]


def test_post_sync_finalizes_ended_active_check_before_retry_discovery():
    h = PostSyncHarness()
    h.failed_ids = [1]
    h.tasks[1] = _failed_task(1)
    queue = h.queue()
    h.initialize(queue)
    queue.enqueue(1)
    queue.pump_once()
    suggestion_run = dict(h.active["suggestion-check"])

    h.tasks[1]["waiting_signal"]["activity"]["checked_at"] = FINISH_A
    h.active.clear()
    h.completions["suggestion-check"] = {
        **suggestion_run,
        "finished_at": FINISH_A,
        "exit_code": 0,
        "error": None,
    }
    h.complete_sync(RUN_A, marker_id=1)

    queue.pump_once()

    report = queue.snapshot()["last_post_sync_recheck"]
    assert h.launches == [1, 1]
    assert report["accepted"] == 1
    assert report["deduped"] == 0
    assert queue.job_for_task(1)["state"] == "running"


def test_post_sync_is_observed_while_active_check_is_still_running():
    h = PostSyncHarness()
    h.failed_ids = [2]
    h.tasks[1] = _failed_task(1)
    h.tasks[2] = _failed_task(2)
    queue = h.queue()
    h.initialize(queue)
    queue.enqueue(1)
    queue.pump_once()
    h.complete_sync(RUN_A, marker_id=1)

    queue.pump_once()

    snapshot = queue.snapshot()
    assert h.launches == [1]
    assert snapshot["active"]["task_id"] == 1
    assert [job["task_id"] for job in snapshot["pending"]] == [2]
    assert snapshot["last_post_sync_recheck"]["accepted"] == 1
