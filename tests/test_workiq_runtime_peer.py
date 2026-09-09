import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from datetime import datetime, timezone
from types import SimpleNamespace

from src.services.workiq_policy import CalendarAction, build_calendar_operation
from src.services.workiq_runtime import (
    ActionHTTPError,
    AuthRequiredError,
    ConsentRequiredError,
    EulaRequiredError,
    InvalidStructuredContentError,
    NotReadyError,
    ProtocolError,
    ToolError,
    TimeoutError as WorkIQTimeoutError,
    TransportError,
    SetupUnavailableError,
    WorkIQRuntime,
    _Operation,
)


FAKE_PEER = Path(__file__).parent / "fakes" / "fake_workiq_mcp_peer.py"


@pytest.fixture(autouse=True)
def configured_readiness_account(monkeypatch):
    setup = SimpleNamespace(
        configured_account=lambda timeout=None: "ada@example.com"
    )
    monkeypatch.setattr("src.services.workiq_runtime.get_setup", lambda: setup)


def command_for(scenario, trace=None):
    command = [sys.executable, str(FAKE_PEER), "--scenario", scenario]
    if trace:
        command += ["--trace", str(trace)]
    return command


def wait_until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_start_initializes_then_notifies_then_lists_tools_before_ready(tmp_path):
    trace = tmp_path / "trace.jsonl"
    runtime = WorkIQRuntime(command=lambda: command_for("ok", trace))
    try:
        snapshot = runtime.start()
        assert snapshot["state"] == "ready"
        assert snapshot["allowed_capabilities"] == ["do_action"]
        messages = [json.loads(line) for line in trace.read_text().splitlines()]
        assert [message["method"] for message in messages[:3]] == [
            "initialize",
            "notifications/initialized",
            "tools/list",
        ]
        assert "id" in messages[0]
        assert "id" not in messages[1]
        assert messages[0]["id"] != messages[2]["id"]
    finally:
        runtime.shutdown()


def test_handshake_passes_remaining_cold_start_budget_to_each_rpc(monkeypatch):
    runtime = WorkIQRuntime(command=lambda: command_for("ok"), startup_timeout=120)
    calls = []
    moments = iter([100.0, 112.0])
    monkeypatch.setattr("src.services.workiq_runtime.time.monotonic", lambda: next(moments))

    def rpc(method, params, timeout):
        calls.append((method, timeout))
        if method == "initialize":
            return {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "WorkIQ", "version": "1.0.0"},
            }
        return {
            "tools": [
                {"name": "ask_work_iq"},
                {"name": "do_action"},
            ]
        }

    monkeypatch.setattr(runtime, "_rpc", rpc)
    monkeypatch.setattr(runtime, "_send_notification", lambda *args: None)
    runtime._handshake(220.0)
    assert calls == [("initialize", 120.0), ("tools/list", 108.0)]


@pytest.mark.parametrize("scenario", ["hang-initialize", "hang-list"])
def test_startup_hang_preserves_true_timeout_after_cleanup(scenario):
    runtime = WorkIQRuntime(
        command=lambda: command_for(scenario),
        startup_timeout=0.15,
        terminate_grace=0.1,
    )
    snapshot = runtime.start()
    try:
        assert snapshot["state"] == "faulted"
        assert snapshot["error"]["code"] == "timeout"
        assert snapshot["authenticated"] is False
        assert snapshot["allowed_capabilities"] == []
        time.sleep(0.05)
        assert runtime.snapshot()["error"]["code"] == "timeout"
    finally:
        runtime.shutdown()


def test_startup_timeout_refuses_replacement_until_old_launch_exits():
    release_launch = threading.Event()
    launch_entered = threading.Event()
    launch_count = 0

    def delayed_factory(argv, **kwargs):
        nonlocal launch_count
        launch_count += 1
        if launch_count == 1:
            launch_entered.set()
            release_launch.wait(5)
        return subprocess.Popen(argv, **kwargs)

    runtime = WorkIQRuntime(
        command=lambda: command_for("ok"),
        process_factory=delayed_factory,
        startup_timeout=0.05,
        terminate_grace=0.05,
    )
    try:
        first = runtime.start()
        assert launch_entered.is_set()
        assert first["state"] == "faulted"
        assert first["error"]["code"] == "timeout"
        old_worker = runtime._thread

        second = runtime.start()
        assert second["state"] == "faulted"
        assert launch_count == 1
        assert runtime._thread is old_worker

        release_launch.set()
        assert wait_until(lambda: not old_worker.is_alive())
        # This assertion tests restart ownership, not cold-start performance.
        # Leave headroom for Windows process scheduling under the full suite.
        runtime._startup_timeout = 5
        third = runtime.start()
        assert third["state"] == "ready"
        assert launch_count == 2
    finally:
        release_launch.set()
        runtime.shutdown()


def test_concurrent_start_calls_share_one_child():
    launches = 0
    launch_lock = threading.Lock()

    def command():
        nonlocal launches
        with launch_lock:
            launches += 1
        return command_for("ok")

    runtime = WorkIQRuntime(command=command)
    barrier = threading.Barrier(3)
    snapshots = []

    def start():
        barrier.wait()
        snapshots.append(runtime.start())

    threads = [threading.Thread(target=start) for _ in range(2)]
    try:
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=20)
            assert not thread.is_alive()
        assert launches == 1
        assert [snapshot["state"] for snapshot in snapshots] == ["ready", "ready"]
    finally:
        runtime.shutdown()


@pytest.mark.parametrize(
    ("scenario", "code", "state"),
    [
        ("readiness-eula", "eula_required", "eula_required"),
        ("readiness-auth", "auth_required", "auth_required"),
        ("readiness-consent", "consent_required", "consent_required"),
        ("readiness-tool-error", "tool", "faulted"),
    ],
)
def test_readiness_classifies_remote_failures(scenario, code, state):
    runtime = WorkIQRuntime(command=lambda: command_for(scenario))
    try:
        runtime.start()
        result = runtime.probe(timeout=2)
        assert result["ok"] is False
        assert result["error"]["code"] == code
        assert runtime.snapshot()["state"] == state
    finally:
        runtime.shutdown()


def test_notification_does_not_break_call_correlation():
    runtime = WorkIQRuntime(command=lambda: command_for("notification-first"))
    try:
        runtime.start()
        assert runtime.probe(timeout=2)["ok"] is True
    finally:
        runtime.shutdown()


def test_shutdown_cleanup_does_not_detach_a_concurrent_restart():
    runtime = WorkIQRuntime(command=lambda: command_for("ok"))
    runtime.start()
    old_thread = runtime._thread
    original_terminate = runtime._terminate_child
    old_terminated = threading.Event()
    release_shutdown = threading.Event()
    start_finished = threading.Event()
    started = {}

    def paused_terminate(*args, **kwargs):
        original_terminate(*args, **kwargs)
        if threading.current_thread().name == "shutdown-test":
            old_terminated.set()
            release_shutdown.wait(5)

    runtime._terminate_child = paused_terminate
    shutdown_thread = threading.Thread(
        target=runtime.shutdown,
        name="shutdown-test",
    )

    def restart():
        started["snapshot"] = runtime.start()
        start_finished.set()

    restart_thread = threading.Thread(target=restart, name="restart-test")
    try:
        shutdown_thread.start()
        assert old_terminated.wait(2)
        assert wait_until(lambda: not old_thread.is_alive())
        restart_thread.start()
        assert start_finished.wait(5)
        release_shutdown.set()
        shutdown_thread.join(2)
        restart_thread.join(5)

        assert start_finished.is_set()
        assert started["snapshot"]["state"] == "ready"
        assert runtime._thread is not None
        assert runtime._thread.is_alive()
    finally:
        release_shutdown.set()
        shutdown_thread.join(2)
        restart_thread.join(2)
        runtime._terminate_child = original_terminate
        runtime.shutdown()


def test_shutdown_can_interrupt_startup_without_waiting_for_its_deadline():
    runtime = WorkIQRuntime(
        command=lambda: command_for("hang-initialize"),
        startup_timeout=5,
        terminate_grace=0.1,
    )
    start_thread = threading.Thread(target=runtime.start, name="startup-test")
    start_thread.start()
    try:
        assert wait_until(
            lambda: runtime.snapshot()["state"] in {"initializing", "starting"}
        )
        started = time.monotonic()
        runtime.shutdown()
        elapsed = time.monotonic() - started

        assert elapsed < 2
        start_thread.join(2)
        assert not start_thread.is_alive()
    finally:
        runtime.shutdown()
        start_thread.join(2)


def test_shutdown_releases_start_waiting_in_command_resolution():
    command_entered = threading.Event()
    release_command = threading.Event()
    start_finished = threading.Event()

    def blocked_command():
        command_entered.set()
        release_command.wait(5)
        return command_for("ok")

    runtime = WorkIQRuntime(
        command=blocked_command,
        startup_timeout=5,
        terminate_grace=0.05,
    )
    start_thread = threading.Thread(
        target=lambda: (runtime.start(), start_finished.set())
    )
    try:
        start_thread.start()
        assert command_entered.wait(1)
        runtime.shutdown()
        assert start_finished.wait(1)
    finally:
        release_command.set()
        start_thread.join(2)
        runtime.shutdown()


def test_shutdown_finishes_active_operation_without_unscoped_cancel():
    runtime = WorkIQRuntime(command=lambda: command_for("ok"))
    runtime.start()
    operation = _Operation(job_id="active-old-generation", timeout=5, state="running")
    runtime._operations[operation.job_id] = operation
    runtime._active_operation = operation
    runtime.cancel = lambda _job_id: (_ for _ in ()).throw(
        AssertionError("shutdown must not call unscoped cancel")
    )

    runtime.shutdown()

    assert operation.event.is_set()
    assert operation.state == "cancelled"
    assert operation.calendar is None


def test_shutdown_revokes_authenticated_readiness_state():
    runtime = WorkIQRuntime(command=lambda: command_for("ok"))
    runtime.start()
    assert runtime.probe(timeout=2)["ok"] is True
    assert runtime.snapshot()["authenticated"] is True

    runtime.shutdown()

    assert runtime.snapshot()["authenticated"] is False


def test_termination_ignores_reader_thread_that_never_started():
    runtime = WorkIQRuntime(command=lambda: command_for("ok"))
    process = subprocess.Popen(
        [sys.executable, "-c", ""],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    process.wait(2)
    reader = threading.Thread()

    runtime._terminate_child(
        process=process,
        incoming=runtime._incoming,
        reader_thread=reader,
        stderr_thread=None,
    )


def test_incoming_mcp_queue_is_bounded():
    runtime = WorkIQRuntime(
        command=lambda: command_for("ok"),
        max_incoming_messages=2,
    )

    assert runtime._incoming.maxsize == 2


def test_cancel_terminates_before_waiting_on_a_blocked_writer():
    runtime = WorkIQRuntime(command=lambda: command_for("ok"))
    operation = _Operation(job_id="blocked-write", timeout=5, state="running")
    runtime._operations[operation.job_id] = operation
    runtime._active_operation = operation
    runtime._active_request_id = 42
    write_locked = threading.Event()
    release_write = threading.Event()
    terminated = threading.Event()

    def hold_write_lock():
        with runtime._write_lock:
            write_locked.set()
            release_write.wait(5)

    holder = threading.Thread(target=hold_write_lock)
    holder.start()
    assert write_locked.wait(1)
    runtime._terminate_child = lambda *args, **kwargs: terminated.set()
    cancel_thread = threading.Thread(target=lambda: runtime.cancel(operation.job_id))
    try:
        cancel_thread.start()
        assert terminated.wait(0.5)
    finally:
        release_write.set()
        holder.join(2)
        cancel_thread.join(2)


def test_server_ping_is_answered_without_breaking_call_correlation(tmp_path):
    trace = tmp_path / "trace.jsonl"
    runtime = WorkIQRuntime(command=lambda: command_for("server-ping", trace))
    try:
        runtime.start()
        assert runtime.probe(timeout=2)["ok"] is True
        assert wait_until(
            lambda: any(
                message.get("id") == 9001 and message.get("result") == {}
                for message in (
                    json.loads(line)
                    for line in trace.read_text().splitlines()
                )
            )
        )
    finally:
        runtime.shutdown()


def test_server_ping_is_answered_while_runtime_is_idle(tmp_path):
    trace = tmp_path / "trace.jsonl"
    runtime = WorkIQRuntime(command=lambda: command_for("idle-server-ping", trace))
    try:
        runtime.start()
        assert wait_until(
            lambda: any(
                message.get("id") == 9002 and message.get("result") == {}
                for message in (
                    json.loads(line)
                    for line in trace.read_text().splitlines()
                )
            )
        )
    finally:
        runtime.shutdown()


@pytest.mark.parametrize(
    ("scenario", "code"),
    [
        ("unsupported-protocol", "protocol"),
        ("missing-tools-capability", "protocol"),
        ("wrong-server", "version_mismatch"),
        ("wrong-version", "version_mismatch"),
    ],
)
def test_handshake_rejects_incompatible_protocol_or_capabilities(scenario, code):
    runtime = WorkIQRuntime(command=lambda: command_for(scenario))
    try:
        snapshot = runtime.start()
        assert snapshot["state"] == "faulted"
        assert snapshot["error"]["code"] == code
        assert snapshot["authenticated"] is False
    finally:
        runtime.shutdown()


def test_readiness_state_is_published_before_completion_event(monkeypatch):
    runtime = WorkIQRuntime()
    runtime._allowed = ("do_action",)
    observed = []
    operation = runtime._operations["probe-order"] = _Operation("probe-order", 2)
    runtime._active_operation = operation
    runtime._active_request_id = 41

    class EventSpy:
        def __init__(self):
            self._set = False

        def is_set(self):
            return self._set

        def set(self):
            observed.append((operation.state, runtime.snapshot()))
            self._set = True

    operation.event = EventSpy()
    monkeypatch.setattr(
        runtime,
        "_rpc",
        lambda *args, **kwargs: {
            "content": [],
            "isError": False,
            "structuredContent": {
                "statusCode": 200,
                "data": {"value": [{"scheduleId": "ada@example.com"}]},
            },
        },
    )
    runtime._run_readiness(operation)
    state, snapshot = observed[0]
    assert state == "succeeded"
    assert snapshot["state"] == "ready"
    assert snapshot["error"] is None
    assert snapshot["authenticated"] is True
    assert snapshot["active_job_id"] is None
    assert runtime._active_request_id is None


def test_failed_reprobe_clears_previous_authenticated_state(monkeypatch):
    runtime = WorkIQRuntime(command=lambda: command_for("ok"))
    try:
        runtime.start()
        assert runtime.probe(timeout=2)["ok"] is True
        assert runtime.snapshot()["authenticated"] is True
        monkeypatch.setattr(
            runtime,
            "_rpc",
            lambda *args, **kwargs: {
                "content": [],
                "isError": True,
            },
        )

        result = runtime.probe(timeout=2)

        assert result["ok"] is False
        assert result["error"]["code"] == "tool"
        snapshot = runtime.snapshot()
        assert snapshot["authenticated"] is False
        assert snapshot["state"] == "faulted"
    finally:
        runtime.shutdown()


@pytest.mark.parametrize(
    ("error", "expected_state"),
    [
        (EulaRequiredError("eula"), "eula_required"),
        (AuthRequiredError("auth"), "auth_required"),
        (ConsentRequiredError("consent"), "consent_required"),
        (SetupUnavailableError("config"), "faulted"),
        (ToolError("tool"), "faulted"),
        (ActionHTTPError("http"), "faulted"),
        (InvalidStructuredContentError("shape"), "faulted"),
        (WorkIQTimeoutError("timeout"), "faulted"),
        (ProtocolError("protocol"), "faulted"),
        (TransportError("transport"), "faulted"),
    ],
)
def test_every_failed_reprobe_clears_auth_before_completion(
    monkeypatch, error, expected_state
):
    runtime = WorkIQRuntime()
    runtime._authenticated = True
    runtime._state = "busy"
    operation = _Operation("failed-reprobe", 2)
    runtime._active_operation = operation
    observed = []

    class EventSpy:
        def __init__(self):
            self._set = False

        def is_set(self):
            return self._set

        def set(self):
            observed.append(runtime.snapshot())
            self._set = True

    operation.event = EventSpy()
    monkeypatch.setattr(
        runtime,
        "_run_readiness",
        lambda _operation: (_ for _ in ()).throw(error),
    )
    monkeypatch.setattr(runtime, "_terminate_child", lambda *args, **kwargs: None)

    runtime._run_operation(operation)

    assert observed[0]["authenticated"] is False
    assert observed[0]["state"] == expected_state


def test_readiness_uses_one_deadline_for_account_lookup_and_mcp(monkeypatch):
    runtime = WorkIQRuntime(
        clock=lambda: datetime(
            2026, 9, 9, 15, 27, 47, tzinfo=timezone.utc
        )
    )
    runtime._allowed = ("do_action",)
    operation = _Operation("deadline", 45)
    account_timeouts = []
    rpc_timeouts = []
    moments = iter([100.0, 100.0, 112.0])
    monkeypatch.setattr(
        "src.services.workiq_runtime.time.monotonic",
        lambda: next(moments),
    )
    monkeypatch.setattr(
        "src.services.workiq_runtime.get_setup",
        lambda: SimpleNamespace(
            configured_account=lambda timeout: (
                account_timeouts.append(timeout) or "ada@example.com"
            )
        ),
    )
    monkeypatch.setattr(
        runtime,
        "_rpc",
        lambda method, params, timeout: (
            rpc_timeouts.append(timeout)
            or {
                "content": [],
                "isError": False,
                "structuredContent": {
                    "statusCode": 200,
                    "data": {"value": [{"scheduleId": "ada@example.com"}]},
                },
            }
        ),
    )

    runtime._run_readiness(operation)

    assert account_timeouts == [45.0]
    assert rpc_timeouts == [33.0]


def test_cancelled_reprobe_clears_previous_authentication():
    runtime = WorkIQRuntime()
    runtime._authenticated = True
    runtime._state = "busy"
    operation = _Operation("cancel-readiness", 45, state="running")
    runtime._operations[operation.job_id] = operation
    runtime._active_operation = operation
    runtime._terminate_child = lambda *args, **kwargs: None

    assert runtime.cancel(operation.job_id) is True

    snapshot = runtime.snapshot()
    assert snapshot["authenticated"] is False
    assert snapshot["state"] == "faulted"


def test_readiness_uses_exact_self_schedule_action_and_retains_no_payload(tmp_path):
    trace = tmp_path / "trace.jsonl"
    runtime = WorkIQRuntime(
        command=lambda: command_for("ok", trace),
        clock=lambda: datetime(
            2026, 9, 9, 15, 27, 47, tzinfo=timezone.utc
        ),
    )
    try:
        runtime.start()
        assert runtime.probe(timeout=2)["ok"] is True
        messages = [json.loads(line) for line in trace.read_text().splitlines()]
        calls = [
            message for message in messages
            if message["method"] == "tools/call"
        ]
        assert len(calls) == 1
        call = calls[0]
        assert call["params"] == {
            "name": "do_action",
            "arguments": {
                "actionUrl": "/me/calendar/getSchedule",
                "jsonBody": {
                    "schedules": ["ada@example.com"],
                    "startTime": {
                        "dateTime": "2026-09-09T15:27:00",
                        "timeZone": "UTC",
                    },
                    "endTime": {
                        "dateTime": "2026-09-09T15:57:00",
                        "timeZone": "UTC",
                    },
                },
            },
        }
        snapshot = json.dumps(runtime.snapshot())
        assert "ada@example.com" not in snapshot
        assert '"value"' not in snapshot
    finally:
        runtime.shutdown()


def test_readiness_discards_private_calendar_response_and_account():
    runtime = WorkIQRuntime(command=lambda: command_for("readiness-private"))
    try:
        runtime.start()
        assert runtime.probe(timeout=2)["ok"] is True
        serialized = json.dumps(runtime.snapshot())
        assert "ada@example.com" not in serialized
        assert "private@example.com" not in serialized
        assert "Private calendar subject" not in serialized
        assert '"value"' not in serialized
    finally:
        runtime.shutdown()


@pytest.mark.parametrize(
    "scenario",
    [
        "readiness-empty",
        "readiness-error-row",
        "readiness-empty-error",
        "readiness-unrelated",
        "readiness-multiple",
    ],
)
def test_readiness_requires_one_matching_error_free_schedule(scenario):
    runtime = WorkIQRuntime(command=lambda: command_for(scenario))
    try:
        runtime.start()
        result = runtime.probe(timeout=2)

        assert result["ok"] is False
        assert result["error"]["code"] == "invalid_structured_content"
        assert runtime.snapshot()["authenticated"] is False
    finally:
        runtime.shutdown()


def test_cancelling_active_calendar_read_revokes_authentication():
    runtime = WorkIQRuntime(
        command=lambda: command_for("hang-action"),
        terminate_grace=0.1,
    )
    try:
        runtime.start()
        assert runtime.probe(timeout=2)["ok"] is True
        operation = build_calendar_operation(
            CalendarAction.GET_SCHEDULE, SCHEDULE_BODY
        )
        job_id = runtime.submit("calendar", timeout=10, calendar=operation)
        assert wait_until(lambda: runtime.snapshot()["active_job_id"] == job_id)

        assert runtime.cancel(job_id) is True

        snapshot = runtime.snapshot()
        assert snapshot["authenticated"] is False
        assert snapshot["state"] == "faulted"
    finally:
        runtime.shutdown()


@pytest.mark.parametrize("scenario", ["malformed", "wrong-id"])
def test_protocol_failures_are_honest_and_bounded(scenario):
    runtime = WorkIQRuntime(command=lambda: command_for(scenario))
    try:
        runtime.start()
        result = runtime.probe(timeout=1)
        assert result["ok"] is False
        assert result["error"]["code"] == "protocol"
        assert "ready" not in json.dumps(runtime.snapshot().get("diagnostics", []))
    finally:
        runtime.shutdown()


def test_oversized_response_is_rejected():
    runtime = WorkIQRuntime(
        command=lambda: command_for("oversized"),
        max_payload_bytes=256 * 1024,
    )
    try:
        runtime.start()
        result = runtime.probe(timeout=10)
        assert result["ok"] is False
        assert result["error"]["code"] == "invalid_response"
    finally:
        runtime.shutdown()


def test_timeout_cancels_and_next_start_restarts(tmp_path):
    trace = tmp_path / "trace.jsonl"
    scenarios = iter(["hang", "ok"])
    runtime = WorkIQRuntime(
        command=lambda: command_for(next(scenarios), trace),
        terminate_grace=0.5,
    )
    notifications = []
    send_notification = runtime._send_notification

    def record_notification(method, params):
        notifications.append(method)
        return send_notification(method, params)

    runtime._send_notification = record_notification
    try:
        runtime.start()
        result = runtime.probe(timeout=0.1)
        assert result["ok"] is False
        assert result["error"]["code"] == "timeout"
        runtime.start()
        assert runtime.probe(timeout=2)["ok"] is True
        methods = [json.loads(line)["method"] for line in trace.read_text().splitlines()]
        assert methods.count("initialize") == 2
        assert "notifications/cancelled" in notifications
    finally:
        runtime.shutdown()


def test_eof_during_startup_cleans_up_and_can_restart():
    scenarios = iter(["eof-list", "ok"])
    runtime = WorkIQRuntime(command=lambda: command_for(next(scenarios)))
    try:
        first = runtime.start()
        assert first["state"] == "faulted"
        assert first["error"]["code"] == "transport"
        second = runtime.start()
        assert second["state"] == "ready"
    finally:
        runtime.shutdown()


def test_missing_capability_fails_closed():
    runtime = WorkIQRuntime(command=lambda: command_for("missing-capability"))
    try:
        snapshot = runtime.start()
        assert snapshot["state"] == "faulted"
        assert snapshot["error"]["code"] == "capability_denied"
        assert snapshot["allowed_capabilities"] == []
    finally:
        runtime.shutdown()


def test_transient_missing_capability_retries_tools_list_once(tmp_path):
    trace = tmp_path / "trace.jsonl"
    runtime = WorkIQRuntime(
        command=lambda: command_for("missing-capability-once", trace)
    )
    try:
        snapshot = runtime.start()

        assert snapshot["state"] == "ready"
        assert snapshot["allowed_capabilities"] == ["do_action"]
        methods = [
            json.loads(line)["method"] for line in trace.read_text().splitlines()
        ]
        assert methods.count("tools/list") == 2
    finally:
        runtime.shutdown()


def test_queue_boundary_cancel_and_shutdown_cleanup():
    runtime = WorkIQRuntime(
        command=lambda: command_for("hang"),
        max_queue=16,
        terminate_grace=0.1,
    )
    runtime.start()
    active_id = runtime.submit(timeout=10)
    assert wait_until(lambda: runtime.snapshot()["active_job_id"] == active_id)
    queued = [runtime.submit(timeout=10) for _ in range(16)]
    from src.services.workiq_runtime import QueueFullError
    with pytest.raises(QueueFullError):
        runtime.submit(timeout=10)
    assert runtime.cancel(queued[0]) is True
    assert runtime.wait(queued[0], 1)["state"] == "cancelled"
    runtime.shutdown()
    assert runtime.wait(active_id, 1)["state"] == "cancelled"
    assert runtime.snapshot()["state"] == "stopped"
    assert all(operation.calendar is None for operation in runtime._operations.values())


def test_shutdown_discards_unconsumed_mcp_payloads():
    runtime = WorkIQRuntime(command=lambda: command_for("ok"))
    runtime.start()
    runtime._incoming.put(b'{"private":"calendar payload"}')

    runtime.shutdown()

    assert runtime._incoming.empty()
    assert all(
        "data" not in (operation.result or {})
        for operation in runtime._operations.values()
    )


def test_fatal_action_timeout_finishes_and_scrubs_queued_calendar_jobs():
    runtime = WorkIQRuntime(
        command=lambda: command_for("hang-action"),
        terminate_grace=0.1,
    )
    try:
        runtime.start()
        assert runtime.probe(timeout=2)["ok"] is True
        operation = build_calendar_operation(
            CalendarAction.GET_SCHEDULE, SCHEDULE_BODY
        )
        active_id = runtime.submit("calendar", timeout=0.1, calendar=operation)
        assert wait_until(lambda: runtime.snapshot()["active_job_id"] == active_id)
        queued_id = runtime.submit("calendar", timeout=5, calendar=operation)

        assert runtime.wait(active_id, 2)["state"] == "timed_out"
        queued_result = runtime.wait(queued_id, 2)

        assert queued_result["state"] == "failed"
        assert queued_result["error"]["code"] == "transport"
        assert runtime._operations[queued_id].calendar is None
        assert runtime._operations[queued_id].event.is_set()
    finally:
        runtime.shutdown()


def test_history_is_bounded_to_newest_twenty():
    runtime = WorkIQRuntime(command=lambda: command_for("ok"))
    try:
        runtime.start()
        ids = []
        for _ in range(21):
            job_id = runtime.submit(timeout=2)
            ids.append(job_id)
            assert runtime.wait(job_id, 3)["state"] == "succeeded"
        history = runtime.snapshot()["history"]
        assert len(history) == 20
        assert ids[0] not in {item["job_id"] for item in history}
        assert ids[-1] in {item["job_id"] for item in history}
    finally:
        runtime.shutdown()


def test_stderr_is_bounded_and_redacted():
    runtime = WorkIQRuntime(command=lambda: command_for("stderr-secret"))
    try:
        runtime.start()
        time.sleep(0.05)
        snapshot = json.dumps(runtime.snapshot())
        assert "synthetic-secret-token" not in snapshot
        assert "synthetic-refresh" not in snapshot
        assert "synthetic-cookie" not in snapshot
        assert "redacted diagnostic" in snapshot
        assert len(snapshot) < 40 * 1024
    finally:
        runtime.shutdown()


def test_calendar_shaped_stderr_is_never_retained():
    runtime = WorkIQRuntime(command=lambda: command_for("ok"))
    try:
        runtime.start()
        runtime._append_diagnostic(
            'structuredContent={"data":{"scheduleId":"ada@example.com",'
            '"scheduleItems":[]}}'
        )
        snapshot = json.dumps(runtime.snapshot())
        assert "ada@example.com" not in snapshot
        assert "scheduleItems" not in snapshot
        assert "redacted diagnostic" in snapshot
    finally:
        runtime.shutdown()


def test_unstructured_personal_stderr_is_never_retained():
    runtime = WorkIQRuntime(command=lambda: command_for("ok"))
    try:
        runtime.start()
        runtime._append_diagnostic("subject: Confidential Board Review")
        snapshot = json.dumps(runtime.snapshot())
        assert "Confidential Board Review" not in snapshot
        assert "redacted diagnostic" in snapshot
    finally:
        runtime.shutdown()


def test_shutdown_is_idempotent_and_reaps_child():
    runtime = WorkIQRuntime(command=lambda: command_for("ok"))
    runtime.start()
    process = runtime._process
    worker = runtime._thread
    reader = runtime._reader_thread
    stderr = runtime._stderr_thread
    runtime.shutdown()
    runtime.shutdown()
    assert runtime.snapshot()["state"] == "stopped"
    assert process.poll() is not None
    assert all(not thread.is_alive() for thread in (worker, reader, stderr))
    assert runtime._process is None
    assert runtime._thread is None
    assert runtime._reader_thread is None
    assert runtime._stderr_thread is None


FIND_BODY = {
    "attendees": [{
        "type": "required",
        "emailAddress": {"name": "Ada", "address": "ada@example.com"},
    }],
    "timeConstraint": {
        "activityDomain": "work",
        "timeSlots": [{
            "start": {"dateTime": "2026-09-08T09:00:00", "timeZone": "UTC"},
            "end": {"dateTime": "2026-09-08T17:00:00", "timeZone": "UTC"},
        }],
    },
    "meetingDuration": "PT30M",
    "maxCandidates": 10,
    "returnSuggestionReasons": True,
    "minimumAttendeePercentage": 100,
}
SCHEDULE_BODY = {
    "schedules": ["ada@example.com"],
    "startTime": {"dateTime": "2026-09-08T09:00:00", "timeZone": "UTC"},
    "endTime": {"dateTime": "2026-09-08T17:00:00", "timeZone": "UTC"},
}


@pytest.mark.parametrize(
    ("action", "body", "expected"),
    [
        (
            CalendarAction.FIND_MEETING_TIMES,
            FIND_BODY,
            {"meetingTimeSuggestions": [], "emptySuggestionsReason": ""},
        ),
        (CalendarAction.GET_SCHEDULE, SCHEDULE_BODY, {"value": []}),
    ],
)
def test_calendar_actions_parse_typed_structured_content(
    tmp_path, action, body, expected
):
    trace = tmp_path / "trace.jsonl"
    runtime = WorkIQRuntime(command=lambda: command_for("ok", trace))
    try:
        runtime.start()
        assert runtime.probe(timeout=2)["ok"] is True
        operation = build_calendar_operation(action, body)
        assert runtime.execute_calendar(operation, timeout=2) == expected
        messages = [json.loads(line) for line in trace.read_text().splitlines()]
        action_call = [
            message for message in messages
            if message.get("method") == "tools/call"
            and message["params"]["name"] == "do_action"
            and message["params"]["arguments"]["jsonBody"] == body
        ]
        assert len(action_call) == 1
        assert action_call[0]["params"] == {
            "name": "do_action",
            "arguments": {"actionUrl": action.value, "jsonBody": body},
        }
        snapshot = json.dumps(runtime.snapshot())
        assert "ada@example.com" not in snapshot
        assert "meetingTimeSuggestions" not in snapshot
    finally:
        runtime.shutdown()


def test_calendar_action_requires_authenticated_readiness(tmp_path):
    trace = tmp_path / "trace.jsonl"
    runtime = WorkIQRuntime(command=lambda: command_for("ok", trace))
    try:
        assert runtime.start()["state"] == "ready"
        operation = build_calendar_operation(CalendarAction.GET_SCHEDULE, SCHEDULE_BODY)
        with pytest.raises(NotReadyError):
            runtime.execute_calendar(operation, timeout=2)
        messages = [json.loads(line) for line in trace.read_text().splitlines()]
        assert not any(
            message.get("method") == "tools/call"
            and message["params"]["name"] == "do_action"
            for message in messages
        )
    finally:
        runtime.shutdown()


@pytest.mark.parametrize(
    ("scenario", "error_type"),
    [
        ("action-tool-error", ToolError),
        ("action-http", ActionHTTPError),
        ("action-no-structured", InvalidStructuredContentError),
        ("action-status-string", InvalidStructuredContentError),
        ("action-no-data", InvalidStructuredContentError),
        ("action-wrong-list", InvalidStructuredContentError),
    ],
)
def test_calendar_actions_reject_invalid_typed_results(scenario, error_type):
    runtime = WorkIQRuntime(command=lambda: command_for(scenario))
    try:
        runtime.start()
        assert runtime.probe(timeout=2)["ok"] is True
        operation = build_calendar_operation(CalendarAction.GET_SCHEDULE, SCHEDULE_BODY)
        with pytest.raises(error_type):
            runtime.execute_calendar(operation, timeout=2)
        snapshot = runtime.snapshot()
        assert snapshot["state"] == "ready"
        assert snapshot["authenticated"] is True
        serialized = json.dumps(snapshot)
        assert "ada@example.com" not in serialized
        assert '"value"' not in serialized
    finally:
        runtime.shutdown()


@pytest.mark.parametrize(
    ("scenario", "error_type", "code", "state"),
    [
        ("action-eula", EulaRequiredError, "eula_required", "eula_required"),
        ("action-auth", AuthRequiredError, "auth_required", "auth_required"),
        ("action-consent", ConsentRequiredError, "consent_required", "consent_required"),
    ],
)
def test_calendar_actions_classify_setup_blockers_without_retaining_payload(
    scenario, error_type, code, state
):
    runtime = WorkIQRuntime(command=lambda: command_for(scenario))
    try:
        runtime.start()
        assert runtime.probe(timeout=2)["ok"] is True
        operation = build_calendar_operation(CalendarAction.GET_SCHEDULE, SCHEDULE_BODY)
        with pytest.raises(error_type):
            runtime.execute_calendar(operation, timeout=2)
        snapshot = runtime.snapshot()
        assert snapshot["state"] == state
        assert snapshot["error"]["code"] == code
        assert snapshot["authenticated"] is False
        serialized = json.dumps(snapshot)
        assert "ada@example.com" not in serialized
        assert '"value"' not in serialized
    finally:
        runtime.shutdown()


def test_queued_calendar_request_does_not_hide_authentication_blocker():
    runtime = WorkIQRuntime(command=lambda: command_for("action-auth"))
    try:
        runtime.start()
        assert runtime.probe(timeout=2)["ok"] is True
        operation = build_calendar_operation(
            CalendarAction.GET_SCHEDULE, SCHEDULE_BODY
        )
        first_id = runtime.submit("calendar", timeout=2, calendar=operation)
        second_id = runtime.submit("calendar", timeout=2, calendar=operation)

        assert runtime.wait(first_id, 2)["error"]["code"] == "auth_required"
        assert runtime.wait(second_id, 2)["error"]["code"] == "not_ready"
        snapshot = runtime.snapshot()
        assert snapshot["state"] == "auth_required"
        assert snapshot["error"]["code"] == "auth_required"
    finally:
        runtime.shutdown()


def test_terminal_transport_marker_evicts_an_incoming_message_when_full():
    runtime = WorkIQRuntime(
        command=lambda: command_for("ok"),
        max_incoming_messages=2,
    )
    runtime._incoming.put_nowait(
        b'{"jsonrpc":"2.0","method":"notifications/progress"}'
    )
    runtime._incoming.put_nowait(
        b'{"jsonrpc":"2.0","method":"notifications/progress"}'
    )

    runtime._queue_terminal(
        runtime._incoming,
        TransportError("Work IQ MCP overwhelmed its response queue."),
    )

    queued = [runtime._incoming.get_nowait(), runtime._incoming.get_nowait()]
    assert any(isinstance(item, TransportError) for item in queued)
