"""Focused contracts for the process-lifetime suggestion re-check FIFO."""

from copy import deepcopy

import pytest

from src.services.suggestion_checks import QueueFull, SuggestionCheckQueue


START = "2026-09-10T14:00:00Z"
FINISH = "2026-09-10T14:01:00Z"


def activity(
    *,
    checked_at=FINISH,
    producer="suggestion-check",
    check_state="ok",
    status="still_pending",
    error=None,
):
    return {
        "producer": producer,
        "check_state": check_state,
        "status": status,
        "checked_at": checked_at,
        "error": error,
    }


class Harness:
    def __init__(self):
        self.tasks = {}
        self.active = None
        self.completed = None
        self.launches = []
        self.launch_failures = set()
        self.next_run = 0

    def add_task(self, task_id, *, status="suggested", waiting_activity=None):
        self.tasks[task_id] = {
            "id": task_id,
            "status": status,
            "waiting_signal": {"activity": waiting_activity},
        }

    def get_task(self, task_id):
        task = self.tasks.get(task_id)
        return deepcopy(task) if task else None

    def get_status(self):
        if not self.active:
            return {"_runs": {}}
        return {
            "suggestion-check": True,
            "_runs": {"suggestion-check": dict(self.active)},
        }

    def get_exit_info(self, label):
        assert label == "suggestion-check"
        return deepcopy(self.completed)

    def launch(self, command, *, label, timeout):
        assert label == "suggestion-check"
        task_id = int(command.rsplit(" ", 1)[1])
        self.launches.append(task_id)
        if task_id in self.launch_failures:
            return {"ok": False, "message": "sensitive launcher detail"}
        self.next_run += 1
        self.active = {
            "run_id": f"run-{self.next_run}",
            "started_at": START,
        }
        return {"ok": True, **self.active, "message": "started"}

    def finish(self, task_id, *, exit_code=0, error=None, result=None):
        run = dict(self.active)
        self.active = None
        self.completed = {
            **run,
            "finished_at": FINISH,
            "exit_code": exit_code,
            "error": error,
        }
        if task_id in self.tasks and result is not ...:
            self.tasks[task_id]["waiting_signal"]["activity"] = (
                activity() if result is None else result
            )

    def queue(self, *, max_pending=100, max_terminal=100):
        return SuggestionCheckQueue(
            launcher=self.launch,
            status_reader=self.get_status,
            completion_reader=self.get_exit_info,
            task_reader=self.get_task,
            max_pending=max_pending,
            max_terminal=max_terminal,
        )


def drive_success(harness, queue, task_id):
    queue.pump_once()
    harness.finish(task_id)
    queue.pump_once()


def test_twelve_tasks_drain_fifo_without_browser_and_concurrency_is_one():
    h = Harness()
    queue = h.queue()
    for task_id in range(1, 13):
        h.add_task(task_id)
        queue.enqueue(task_id)

    for task_id in range(1, 13):
        queue.pump_once()
        queue.pump_once()
        assert h.launches == list(range(1, task_id + 1))
        assert queue.snapshot()["active"]["task_id"] == task_id
        h.finish(task_id)
        queue.pump_once()

    assert h.launches == list(range(1, 13))
    assert queue.snapshot()["pending"] == []
    assert queue.snapshot()["active"] is None


def test_dedupe_returns_same_job_while_queued_and_running():
    h = Harness()
    h.add_task(7)
    queue = h.queue()

    first = queue.enqueue(7)
    queued_duplicate = queue.enqueue(7)
    queue.pump_once()
    running_duplicate = queue.enqueue(7)

    assert first["job_id"] == queued_duplicate["job_id"]
    assert first["job_id"] == running_duplicate["job_id"]
    assert len(h.launches) == 1


def test_queue_positions_decrement_when_head_starts():
    h = Harness()
    for task_id in (1, 2, 3):
        h.add_task(task_id)
    queue = h.queue()
    assert queue.enqueue(1)["queue_position"] == 1
    assert queue.enqueue(2)["queue_position"] == 2
    assert queue.enqueue(3)["queue_position"] == 3

    queue.pump_once()
    snapshot = queue.snapshot()

    assert snapshot["active"]["task_id"] == 1
    assert [job["queue_position"] for job in snapshot["pending"]] == [1, 2]


def test_capacity_is_100_pending_excluding_running_and_dedupe_wins():
    h = Harness()
    for task_id in range(1, 103):
        h.add_task(task_id)
    queue = h.queue(max_pending=100)

    running = queue.enqueue(1)
    queue.pump_once()
    for task_id in range(2, 102):
        queue.enqueue(task_id)

    assert len(queue.snapshot()["pending"]) == 100
    assert queue.enqueue(1)["job_id"] == running["job_id"]
    with pytest.raises(QueueFull):
        queue.enqueue(102)


def test_new_job_allowed_after_terminal():
    h = Harness()
    h.add_task(1)
    queue = h.queue()
    first = queue.enqueue(1)
    drive_success(h, queue, 1)
    second = queue.enqueue(1)
    assert second["job_id"] != first["job_id"]


def test_global_run_blocks_target_and_launch_failure_does_not_block_next():
    h = Harness()
    h.add_task(1)
    h.add_task(2)
    queue = h.queue()
    queue.enqueue(1)
    queue.enqueue(2)
    h.active = {"run_id": "global", "started_at": START}

    queue.pump_once()
    assert h.launches == []

    h.active = None
    h.launch_failures.add(1)
    queue.pump_once()
    assert queue.job_for_task(1)["state"] == "failed"
    assert "sensitive" not in queue.job_for_task(1)["error"]
    queue.pump_once()
    assert h.launches == [1, 2]


@pytest.mark.parametrize("replacement", [None, {"id": 1, "status": "active"}])
def test_deleted_or_promoted_head_is_skipped_and_next_launches(replacement):
    h = Harness()
    h.add_task(1)
    h.add_task(2)
    queue = h.queue()
    queue.enqueue(1)
    queue.enqueue(2)
    if replacement is None:
        del h.tasks[1]
    else:
        h.tasks[1] = replacement

    queue.pump_once()

    assert queue.job_for_task(1)["state"] == "skipped"
    assert h.launches == [2]


@pytest.mark.parametrize(
    "result",
    [
        ...,
        activity(checked_at="2026-09-10T13:59:59Z"),
        activity(checked_at="2026-09-10T14:01:01Z"),
        activity(producer="waiting-check"),
        activity(check_state="ok", status="not-valid"),
    ],
)
def test_missing_stale_foreign_or_malformed_result_never_succeeds(result):
    h = Harness()
    h.add_task(1, waiting_activity=activity(checked_at="2026-09-10T13:00:00Z"))
    queue = h.queue()
    queue.enqueue(1)
    queue.pump_once()
    h.finish(1, result=result)
    queue.pump_once()
    assert queue.job_for_task(1)["state"] == "failed"


def test_wrong_run_completion_never_gets_attributed_and_later_jobs_wait():
    h = Harness()
    h.add_task(1)
    h.add_task(2)
    queue = h.queue()
    queue.enqueue(1)
    queue.enqueue(2)
    queue.pump_once()
    h.active = None
    h.completed = {
        "run_id": "wrong-run",
        "started_at": START,
        "finished_at": FINISH,
        "exit_code": 0,
        "error": None,
    }

    queue.pump_once()

    assert queue.job_for_task(1)["state"] == "failed"
    assert h.launches == [1, 2]


@pytest.mark.parametrize(
    ("prior", "checked", "expected"),
    [
        ("2026-09-10T13:00:00", FINISH, "succeeded"),
        ("2026-09-10T13:00:00Z", "2026-09-10T14:01:00", "succeeded"),
        ("2026-09-10T13:00:00", "2026-09-10T13:59:59", "failed"),
        ("2026-09-10T13:00:00", "invalid", "failed"),
    ],
)
def test_offset_free_or_invalid_timestamps_do_not_stall_queue(prior, checked, expected):
    h = Harness()
    h.add_task(1, waiting_activity=activity(checked_at=prior))
    h.add_task(2)
    queue = h.queue()
    queue.enqueue(1)
    queue.enqueue(2)
    queue.pump_once()
    h.finish(1, result=activity(checked_at=checked))

    queue.pump_once()

    assert queue.job_for_task(1)["state"] == expected
    assert queue.snapshot()["active"]["task_id"] == 2
    assert h.launches == [1, 2]


def test_persisted_failure_and_process_failure_continue_to_later_jobs():
    h = Harness()
    for task_id in (1, 2, 3):
        h.add_task(task_id)
    queue = h.queue()
    for task_id in (1, 2, 3):
        queue.enqueue(task_id)

    queue.pump_once()
    h.finish(1, result=activity(check_state="failed", status=None, error="private"))
    queue.pump_once()
    assert queue.job_for_task(1)["state"] == "failed"
    h.finish(2, exit_code=7, error="private stdout")
    queue.pump_once()
    assert queue.job_for_task(2)["state"] == "failed"
    h.finish(3)
    queue.pump_once()

    assert queue.job_for_task(3)["state"] == "succeeded"
    assert h.launches == [1, 2, 3]


def test_status_reader_error_is_safe_and_next_tick_recovers():
    h = Harness()
    h.add_task(1)
    reads = 0

    def status_reader():
        nonlocal reads
        reads += 1
        if reads == 1:
            raise RuntimeError("private status detail")
        return h.get_status()

    queue = SuggestionCheckQueue(
        launcher=h.launch,
        status_reader=status_reader,
        completion_reader=h.get_exit_info,
        task_reader=h.get_task,
    )
    queue.enqueue(1)

    queue.pump_once()
    assert h.launches == []
    assert queue.snapshot()["last_error"] == (
        "Could not read suggestion check status."
    )
    queue.pump_once()
    assert h.launches == [1]


def test_terminal_history_is_oldest_first_bounded_and_schema_is_safe():
    h = Harness()
    queue = h.queue(max_terminal=100)
    for task_id in range(1, 102):
        h.add_task(task_id)
        queue.enqueue(task_id)
        drive_success(h, queue, task_id)

    snapshot = queue.snapshot()
    terminals = snapshot["terminal"]
    assert len(terminals) == 100
    assert terminals[0]["task_id"] == 2
    assert terminals[-1]["task_id"] == 101
    allowed = {
        "job_id", "task_id", "state", "queue_position", "run_id",
        "queued_at", "started_at", "finished_at", "error",
    }
    for job in snapshot["pending"] + terminals + (
        [snapshot["active"]] if snapshot["active"] else []
    ):
        assert set(job) <= allowed


def test_post_sync_manual_recheck_of_successful_suggestion_still_launches():
    h = Harness()
    h.add_task(1, waiting_activity=activity())
    queue = h.queue()

    queue.enqueue(1)
    queue.pump_once()

    assert h.launches == [1]
    assert queue.job_for_task(1)["state"] == "running"
