import json
import copy
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from datetime import datetime, timezone
from types import SimpleNamespace

from src.services.workiq_policy import CalendarAction, build_calendar_operation
from src.services import workiq_policy as policy
from src.services import workiq_runtime as runtime_module
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


ASK_RESULT = {
    "content": [{"type": "text", "text": "private-answer"}],
    "structuredContent": {
        "answer": "private-answer",
        "conversationId": "private-conversation",
        "account": "private-account@example.test",
        "extra": "private-extra",
    },
}
ASK_DATA = {"answer": "private-answer", "conversation_id": "private-conversation"}
ASK_QUESTION = "private-question@example.test"
ASK_PROBE = (
    "Reply exactly RIVETER_PROTOCOL_PROBE. Do not search or access Microsoft 365 data."
)


def ask_trace(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def assert_ask_scrubbed(runtime):
    serialized = json.dumps(runtime.snapshot()) + repr([
        (operation.ask, operation.result, vars(operation))
        for operation in runtime._operations.values()
    ])
    for value in [
        ASK_QUESTION, "private-answer", "private-conversation",
        "private-account@example.test", "private-extra", "private-error@example.test",
    ]:
        assert value not in serialized
    assert all(operation.ask is None for operation in runtime._operations.values())


@pytest.mark.parametrize("explicit_false", [False, True])
@pytest.mark.parametrize("structured_answer", [False, True])
def test_ask_exact_wire_and_transient_consume_once(
    tmp_path, explicit_false, structured_answer
):
    trace = tmp_path / "ask.jsonl"
    envelope = copy.deepcopy(ASK_RESULT)
    if explicit_false:
        envelope["isError"] = False
    if not structured_answer:
        envelope["structuredContent"].pop("answer")
    runtime = WorkIQRuntime(command=lambda: [
        *command_for("ok", trace), "--ask-result", json.dumps(envelope)
    ])
    try:
        assert runtime.probe_ask(timeout=2) == {"ok": True}
        assert runtime.execute_ask(ASK_QUESTION, timeout=2) == ASK_DATA
        job = runtime.submit("ask", ask=policy.build_ask_operation(ASK_QUESTION), timeout=2)
        assert runtime.wait(job, 3)["data"] == ASK_DATA
        assert "data" not in runtime.wait(job, 0)
        assert runtime.execute_ask(ASK_QUESTION, timeout=2) == ASK_DATA
        calls = [m["params"] for m in ask_trace(trace) if m["method"] == "tools/call"]
        assert calls == [
            {"name": "ask", "arguments": {"question": question}}
            for question in [ASK_PROBE, ASK_QUESTION, ASK_QUESTION, ASK_QUESTION]
        ]
        assert sum(m["method"] == "initialize" for m in ask_trace(trace)) == 1
        assert runtime.snapshot()["allowed_capabilities"] == ["do_action", "fetch"]
        assert runtime.snapshot()["authenticated"] is False
        assert_ask_scrubbed(runtime)
    finally:
        runtime.shutdown()


def malformed_ask_results():
    cases = []
    for content in [None, [], {}, [{"type": "text", "text": " "}],
                    [{"type": "image", "text": "private-answer"}],
                    [{"type": "text", "text": 7}],
                    [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]]:
        cases.append(({**ASK_RESULT, "content": content}, "invalid_structured_content"))
    cases.append(({"structuredContent": ASK_RESULT["structuredContent"]}, "invalid_structured_content"))
    for value in [None, 0, "false", []]:
        cases.append(({**ASK_RESULT, "isError": value}, "invalid_structured_content"))
    cases.append(({**ASK_RESULT, "isError": True}, "tool"))
    for structured in [None, [], {}, {"conversationId": ""}, {"conversationId": " "},
                       {"conversationId": 7}, {"conversationId": "x", "answer": None},
                       {"conversationId": "x", "answer": "contradictory"}]:
        cases.append(({**ASK_RESULT, "structuredContent": structured}, "invalid_structured_content"))
    cases.append(({"content": ASK_RESULT["content"]}, "invalid_structured_content"))
    for meta in [None, [], "bad"]:
        cases.append(({**ASK_RESULT, "_meta": meta}, "invalid_structured_content"))
    return cases


@pytest.mark.parametrize(("envelope", "code"), malformed_ask_results())
def test_ask_malformed_envelopes_preserve_calendar_readiness(tmp_path, envelope, code):
    runtime = WorkIQRuntime(command=lambda: [
        *command_for("ok"), "--ask-result", json.dumps(envelope)
    ])
    try:
        assert runtime.probe(timeout=2)["ok"]
        result = runtime.probe_ask(timeout=2)
        assert result["ok"] is False
        assert result["error"]["code"] == code
        assert runtime.snapshot()["authenticated"] is True
        assert runtime.snapshot()["state"] == "ready"
        calendar = build_calendar_operation(CalendarAction.GET_SCHEDULE, SCHEDULE_BODY)
        assert runtime.execute_calendar(calendar, timeout=2) == {"value": []}
        assert runtime.read_sent_items(timeout=2) == {"value": []}
        assert_ask_scrubbed(runtime)
    finally:
        runtime.shutdown()


def test_ask_only_survives_legacy_probe_without_restart(tmp_path):
    trace = tmp_path / "ask.jsonl"
    runtime = WorkIQRuntime(command=lambda: command_for("only-ask", trace))
    try:
        assert runtime.start()["state"] == "ready"
        assert runtime.snapshot()["allowed_capabilities"] == []
        assert runtime.probe(timeout=2)["error"]["code"] == "capability_denied"
        before = runtime.snapshot()
        assert runtime.probe_ask(timeout=2) == {"ok": True}
        assert runtime.execute_ask(ASK_QUESTION, timeout=2) == ASK_DATA
        assert runtime.snapshot()["authenticated"] is False
        assert runtime.snapshot()["state"] == before["state"]
        assert runtime.snapshot()["error"] == before["error"]
        assert sum(m["method"] == "initialize" for m in ask_trace(trace)) == 1
        assert all(m["params"]["name"] == "ask" for m in ask_trace(trace)
                   if m["method"] == "tools/call")
    finally:
        runtime.shutdown()


def test_calendar_only_ask_denial_does_not_revoke_readiness(tmp_path):
    trace = tmp_path / "calendar.jsonl"
    runtime = WorkIQRuntime(command=lambda: command_for("only-action", trace))
    try:
        assert runtime.probe(timeout=2)["ok"]
        before = len(ask_trace(trace))
        assert runtime.probe_ask(timeout=2)["error"]["code"] == "capability_denied"
        with pytest.raises(runtime_module.CapabilityDeniedError):
            runtime.execute_ask(ASK_QUESTION, timeout=2)
        assert len(ask_trace(trace)) == before
        assert runtime.snapshot()["authenticated"] is True
        assert runtime.snapshot()["state"] == "ready"
        calendar = build_calendar_operation(CalendarAction.GET_SCHEDULE, SCHEDULE_BODY)
        assert runtime.execute_calendar(calendar, timeout=2) == {"value": []}
        assert_ask_scrubbed(runtime)
    finally:
        runtime.shutdown()


def test_stale_ask_alias_never_becomes_capability():
    runtime = WorkIQRuntime(command=lambda: command_for("only-stale-ask"))
    try:
        assert runtime.start()["error"]["code"] == "capability_denied"
    finally:
        runtime.shutdown()


@pytest.mark.parametrize(
    ("scenario", "code"),
    [("ask-auth-once", "auth_required"), ("ask-consent-once", "consent_required"),
     ("ask-eula-once", "eula_required")],
)
def test_ask_cooldown_boundary_and_one_serialized_recovery(tmp_path, scenario, code):
    trace = tmp_path / "cooldown.jsonl"
    now = [100.0]
    runtime = WorkIQRuntime(
        command=lambda: command_for(scenario, trace), monotonic_clock=lambda: now[0]
    )
    try:
        assert runtime.probe(timeout=2)["ok"]
        assert runtime.probe_ask(timeout=2)["error"]["code"] == code
        before = ask_trace(trace)
        for elapsed in [0.0, 59.999]:
            now[0] = 100.0 + elapsed
            assert runtime.probe_ask(timeout=2)["error"]["code"] == code
            with pytest.raises(runtime_module.WorkIQError) as error:
                runtime.execute_ask(ASK_QUESTION, timeout=2)
            assert error.value.code == code
            job = runtime.submit("ask", ask=policy.build_ask_operation(ASK_QUESTION), timeout=2)
            assert runtime.wait(job, 3)["error"]["code"] == code
            assert ask_trace(trace) == before
        assert runtime.snapshot()["authenticated"] is True
        assert runtime.snapshot()["state"] == "ready"
        now[0] = 160.0
        jobs = [runtime.submit("ask", ask=policy.build_ask_operation(q), timeout=2)
                for q in [ASK_QUESTION, "second-private-question"]]
        assert [runtime.wait(job, 3)["data"] for job in jobs] == [ASK_DATA, ASK_DATA]
        calls = [m["params"] for m in ask_trace(trace) if m["method"] == "tools/call"]
        assert calls[1:] == [
            {"name": "ask", "arguments": {"question": q}}
            for q in [ASK_PROBE, ASK_PROBE, ASK_QUESTION, "second-private-question"]
        ]
        assert sum(m["method"] == "initialize" for m in ask_trace(trace)) == 1
        assert [h["job_id"] for h in runtime.snapshot()["history"]][-2:] == jobs
        assert_ask_scrubbed(runtime)
    finally:
        runtime.shutdown()


@pytest.mark.parametrize("rpc_error", [False, True])
def test_ask_free_text_is_not_authentication_evidence(tmp_path, monkeypatch, rpc_error):
    trace = tmp_path / "text.jsonl"
    envelope = {
        "content": [{"type": "text", "text": "sign in is required private-error@example.test"}],
        "isError": True,
    }
    runtime = WorkIQRuntime(command=lambda: [
        *command_for("ok", trace), "--ask-result", json.dumps(envelope)
    ])
    try:
        runtime.start()
        if rpc_error:
            original = runtime._parse_message
            def inject(raw):
                message = original(raw)
                if "result" in message:
                    message.pop("result")
                    message["error"] = {"code": -32000, "message": envelope["content"][0]["text"]}
                return message
            monkeypatch.setattr(runtime, "_parse_message", inject)
        for _ in range(2):
            result = runtime.probe_ask(timeout=2)
            assert result["error"]["code"] == ("remote" if rpc_error else "tool")
        assert sum(m["method"] == "tools/call" for m in ask_trace(trace)) == 2
        assert_ask_scrubbed(runtime)
    finally:
        runtime.shutdown()


def test_ask_submit_requires_minted_plan_and_rejects_cross_payloads():
    runtime = WorkIQRuntime(command=lambda: command_for("ok"))
    try:
        runtime.start()
        calendar = build_calendar_operation(CalendarAction.GET_SCHEDULE, SCHEDULE_BODY)
        for kwargs in [
            {"plan": "ask"}, {"plan": "ask", "ask": {"question": ASK_QUESTION}},
            {"plan": "ask", "ask": policy.AskOperation(ASK_QUESTION, object())},
            {"plan": "ask", "ask": policy.build_ask_operation(ASK_QUESTION), "calendar": calendar},
            {"plan": "readiness", "ask": policy.build_ask_operation(ASK_QUESTION)},
            {"plan": "ask_probe", "ask": policy.build_ask_operation(ASK_QUESTION)},
            {"plan": "ask_work_iq"},
        ]:
            with pytest.raises(policy.CapabilityError):
                runtime.submit(**kwargs)
    finally:
        runtime.shutdown()


def test_ask_probe_and_business_share_one_timeout_budget(tmp_path):
    trace = tmp_path / "budget.jsonl"
    runtime = WorkIQRuntime(
        command=lambda: command_for("ask-slow", trace), terminate_grace=0.05
    )
    notifications = []
    send_notification = runtime._send_notification

    def record_notification(method, params):
        notifications.append(method)
        return send_notification(method, params)

    runtime._send_notification = record_notification
    try:
        assert runtime.probe(timeout=2)["ok"]
        with pytest.raises(WorkIQTimeoutError):
            runtime.execute_ask(ASK_QUESTION, timeout=0.23)
        assert runtime.snapshot()["authenticated"] is False
        assert runtime.snapshot()["state"] == "faulted"
        assert_ask_scrubbed(runtime)
        assert "notifications/cancelled" in notifications
    finally:
        runtime.shutdown()


@pytest.mark.parametrize("cancel", [False, True])
def test_ask_rechecks_budget_and_cancellation_after_probe(tmp_path, monkeypatch, cancel):
    trace = tmp_path / "gate.jsonl"
    now = [100.0]
    runtime = WorkIQRuntime(
        command=lambda: command_for("ok", trace),
        monotonic_clock=lambda: now[0], terminate_grace=0.05,
    )
    original = runtime._rpc
    def intercept(method, params, timeout, **kwargs):
        result = original(method, params, timeout, **kwargs)
        if method == "tools/call" and params["name"] == "ask":
            if cancel:
                runtime.cancel(runtime.snapshot()["active_job_id"])
            else:
                now[0] += 2.0
        return result
    monkeypatch.setattr(runtime, "_rpc", intercept)
    try:
        assert runtime.probe(timeout=2)["ok"]
        with pytest.raises(runtime_module.CancelledError if cancel else WorkIQTimeoutError):
            runtime.execute_ask(ASK_QUESTION, timeout=2)
        calls = [m["params"] for m in ask_trace(trace) if m["method"] == "tools/call"]
        assert [c["name"] for c in calls] == ["do_action", "ask"]
        assert calls[-1]["arguments"] == {"question": ASK_PROBE}
        assert runtime.snapshot()["authenticated"] is False
        assert_ask_scrubbed(runtime)
    finally:
        runtime.shutdown()


def test_ask_cancel_between_budget_check_and_send_emits_no_business_call(
    tmp_path, monkeypatch
):
    trace = tmp_path / "cancel-send.jsonl"
    runtime = WorkIQRuntime(command=lambda: command_for("ok", trace))
    original = runtime._rpc
    terminate = runtime._terminate_child

    def intercept(method, params, timeout, **kwargs):
        if method == "tools/call" and params.get("arguments") == {"question": ASK_QUESTION}:
            # Keep the child alive briefly, as another thread terminating it can do.
            monkeypatch.setattr(runtime, "_terminate_child", lambda *a, **k: None)
            runtime.cancel(runtime.snapshot()["active_job_id"])
            monkeypatch.setattr(runtime, "_terminate_child", terminate)
        return original(method, params, timeout, **kwargs)

    monkeypatch.setattr(runtime, "_rpc", intercept)
    try:
        with pytest.raises(runtime_module.CancelledError):
            runtime.execute_ask(ASK_QUESTION, timeout=2)
        assert wait_until(lambda: runtime._thread is None)
        calls = [m["params"] for m in ask_trace(trace) if m["method"] == "tools/call"]
        assert calls == [{"name": "ask", "arguments": {"question": ASK_PROBE}}]
        assert_ask_scrubbed(runtime)
    finally:
        runtime.shutdown()


def test_ask_facade_scrubs_success_racing_caller_timeout(monkeypatch):
    runtime = WorkIQRuntime(command=lambda: command_for("ok"))

    def expired_wait(job_id, timeout):
        assert runtime._operations[job_id].event.wait(2)
        return {"state": "running"}

    monkeypatch.setattr(runtime, "wait", expired_wait)
    try:
        with pytest.raises(WorkIQTimeoutError):
            runtime.execute_ask(ASK_QUESTION, timeout=2)
        assert_ask_scrubbed(runtime)
    finally:
        runtime.shutdown()


@pytest.mark.parametrize("end", ["queued_cancel", "active_cancel", "timeout", "shutdown"])
def test_ask_terminal_paths_scrub_active_and_queued_private_fields(tmp_path, end):
    trace = tmp_path / "terminal.jsonl"
    runtime = WorkIQRuntime(
        command=lambda: command_for("ask-hang", trace), terminate_grace=0.1
    )
    try:
        assert runtime.probe(timeout=2)["ok"]
        first = runtime.submit("ask", ask=policy.build_ask_operation(ASK_QUESTION),
                               timeout=0.3 if end == "timeout" else 10)
        assert wait_until(lambda: any(m["method"] == "tools/call"
                                     and m["params"]["name"] == "ask"
                                     for m in ask_trace(trace)))
        second = runtime.submit("ask", ask=policy.build_ask_operation(ASK_QUESTION), timeout=2)
        if end == "queued_cancel":
            process = runtime._process
            assert runtime.cancel(second)
            assert runtime.wait(second, 0)["state"] == "cancelled"
            assert runtime._operations[second].ask is None
            assert process.poll() is None
            assert runtime.snapshot()["authenticated"] is True
            runtime.cancel(first)
        elif end == "active_cancel":
            assert runtime.cancel(first)
        elif end == "shutdown":
            runtime.shutdown()
        results = [runtime.wait(job, 3) for job in [first, second]]
        assert all(result["state"] in {"cancelled", "failed", "timed_out"} for result in results)
        assert results[0]["error"]["code"] == ("timeout" if end == "timeout" else "cancelled")
        assert runtime.snapshot()["authenticated"] is False
        assert_ask_scrubbed(runtime)
        assert wait_until(lambda: runtime._incoming.empty())
    finally:
        runtime.shutdown()


@pytest.mark.parametrize(("scenario", "code"), [("ask-protocol", "protocol"), ("ask-eof", "transport")])
def test_fatal_ask_child_failure_revokes_calendar(tmp_path, scenario, code):
    runtime = WorkIQRuntime(command=lambda: command_for(scenario), terminate_grace=0.1)
    try:
        assert runtime.probe(timeout=2)["ok"]
        with pytest.raises(runtime_module.WorkIQError) as error:
            runtime.execute_ask(ASK_QUESTION, timeout=2)
        assert error.value.code == code
        assert runtime.snapshot()["authenticated"] is False
        assert runtime.snapshot()["state"] == "faulted"
        assert_ask_scrubbed(runtime)
    finally:
        runtime.shutdown()


def test_mixed_ask_calendar_fetch_queue_uses_exact_fifo_dispatch(tmp_path):
    trace = tmp_path / "mixed.jsonl"
    runtime = WorkIQRuntime(command=lambda: command_for("ok", trace))
    try:
        assert runtime.probe(timeout=2)["ok"]
        assert runtime.probe_ask(timeout=2)["ok"]
        calendar = build_calendar_operation(CalendarAction.GET_SCHEDULE, SCHEDULE_BODY)
        jobs = [
            runtime.submit("ask", ask=policy.build_ask_operation(ASK_QUESTION), timeout=2),
            runtime.submit("calendar", calendar=calendar, timeout=2),
            runtime.submit("sent_items", timeout=2),
            runtime.submit("ask", ask=policy.build_ask_operation(ASK_QUESTION), timeout=2),
        ]
        assert [runtime.wait(job, 3)["data"] for job in jobs] == [
            ASK_DATA, {"value": []}, {"value": []}, ASK_DATA
        ]
        calls = [m["params"]["name"] for m in ask_trace(trace) if m["method"] == "tools/call"]
        assert calls == ["do_action", "ask", "ask", "do_action", "fetch", "ask"]
        assert [h["job_id"] for h in runtime.snapshot()["history"]][-4:] == jobs
        assert_ask_scrubbed(runtime)
    finally:
        runtime.shutdown()


def test_ask_cold_start_consumes_caller_deadline(tmp_path):
    trace = tmp_path / "cold-budget.jsonl"
    runtime = WorkIQRuntime(
        command=lambda: command_for("hang-initialize", trace),
        startup_timeout=0.30, terminate_grace=0.01,
    )
    try:
        started = time.monotonic()
        with pytest.raises(WorkIQTimeoutError):
            runtime.execute_ask(ASK_QUESTION, timeout=0.05)
        assert time.monotonic() - started < 0.20
        assert not any(m["method"] == "tools/call" for m in ask_trace(trace))
        assert runtime.snapshot()["authenticated"] is False
        assert_ask_scrubbed(runtime)
    finally:
        runtime.shutdown()


def test_ask_start_lock_waiter_expires_without_changing_owner_state():
    runtime = WorkIQRuntime(command=lambda: command_for("ok"), terminate_grace=0.01)
    results = []
    runtime._start_lock.acquire()
    caller = threading.Thread(target=lambda: results.append(runtime.probe_ask(timeout=0.05)))
    caller.start()
    try:
        try:
            caller.join(0.20)
            assert not caller.is_alive()
            assert results[0]["error"]["code"] == "timeout"
            assert runtime.snapshot()["state"] == "stopped"
            assert not runtime._stopping.is_set()
            assert runtime._process is None
        finally:
            runtime._start_lock.release()
            caller.join(2)
        assert runtime.start()["state"] == "ready"
        assert runtime.probe_ask(timeout=2)["ok"]
    finally:
        runtime.shutdown()


def test_generic_ask_expires_during_independently_owned_startup(tmp_path):
    trace = tmp_path / "joining-startup.jsonl"
    launch_entered = threading.Event()
    release_launch = threading.Event()
    owner_results = []

    def gated_process_factory(argv, **kwargs):
        launch_entered.set()
        release_launch.wait(2)
        return subprocess.Popen(argv, **kwargs)

    runtime = WorkIQRuntime(
        command=lambda: command_for("ok", trace),
        process_factory=gated_process_factory, startup_timeout=2, terminate_grace=0.01,
    )
    owner = threading.Thread(target=lambda: owner_results.append(runtime.start()))
    owner.start()
    try:
        assert launch_entered.wait(1)
        job = runtime.submit("ask", ask=policy.build_ask_operation(ASK_QUESTION), timeout=0.04)
        result = runtime.wait(job, 0.10)
        assert result["state"] == "timed_out"
        assert result["error"]["code"] == "timeout"
        assert runtime.snapshot()["state"] == "starting"
        assert not runtime._stopping.is_set()
        assert_ask_scrubbed(runtime)
        release_launch.set()
        owner.join(2)
        assert owner_results[0]["state"] == "ready"
        assert runtime.probe(timeout=2)["ok"]
        assert not any(m.get("params", {}).get("name") == "ask" for m in ask_trace(trace))
    finally:
        release_launch.set()
        owner.join(2)
        runtime.shutdown()


def test_expired_startup_owner_cannot_abort_a_different_healthy_child():
    runtime = WorkIQRuntime(command=lambda: command_for("ok"), terminate_grace=0.01)
    stale_owner = threading.Thread()
    try:
        assert runtime.probe(timeout=2)["ok"]
        process = runtime._process
        before = runtime.snapshot()
        runtime._abort_ask_startup(stale_owner)
        assert runtime.snapshot() == before
        assert runtime._process is process and process.poll() is None
        assert not runtime._stopping.is_set()
        assert runtime.execute_ask(ASK_QUESTION, timeout=2) == ASK_DATA
        assert runtime.snapshot()["authenticated"] is True
    finally:
        runtime.shutdown()


@pytest.mark.parametrize("wait_while_queued", [False, True])
def test_generic_ask_deadline_includes_fifo_and_expires_only_that_job(
    tmp_path, wait_while_queued
):
    trace = tmp_path / "fifo-budget.jsonl"
    runtime = WorkIQRuntime(
        command=lambda: command_for("ask-slow", trace), terminate_grace=0.01
    )
    try:
        assert runtime.probe(timeout=2)["ok"]
        assert runtime.probe_ask(timeout=2)["ok"]
        first = runtime.submit(
            "ask", ask=policy.build_ask_operation("first synthetic read"), timeout=2
        )
        assert wait_until(lambda: runtime.snapshot()["active_job_id"] == first)
        second = runtime.submit(
            "ask", ask=policy.build_ask_operation(ASK_QUESTION), timeout=0.04
        )
        process = runtime._process
        assert runtime.wait(second, 0)["state"] == "queued"
        assert runtime._operations[second].ask is not None
        if not wait_while_queued:
            assert runtime.wait(first, 2)["data"] == ASK_DATA
        result = runtime.wait(second)
        assert result["state"] == "timed_out"
        assert result["error"]["code"] == "timeout"
        assert runtime._process is process and process.poll() is None
        assert runtime.snapshot()["authenticated"] is True
        if wait_while_queued:
            assert runtime.wait(first, 2)["data"] == ASK_DATA
        assert runtime.snapshot()["state"] == "ready"
        assert not any(
            m.get("params", {}).get("arguments") == {"question": ASK_QUESTION}
            for m in ask_trace(trace)
        )
        assert_ask_scrubbed(runtime)
    finally:
        runtime.shutdown()


def test_ask_facade_queue_time_is_not_extra_business_budget():
    runtime = WorkIQRuntime(
        command=lambda: command_for("ask-slow"), terminate_grace=0.01
    )
    try:
        assert runtime.probe_ask(timeout=2)["ok"]
        first = runtime.submit(
            "ask", ask=policy.build_ask_operation("first synthetic read"), timeout=2
        )
        assert wait_until(lambda: runtime.snapshot()["active_job_id"] == first)
        with pytest.raises(WorkIQTimeoutError):
            runtime.execute_ask(ASK_QUESTION, timeout=0.20)
        runtime.wait(first, 1)
        assert runtime.snapshot()["state"] == "faulted"
        assert_ask_scrubbed(runtime)
    finally:
        runtime.shutdown()


@pytest.mark.parametrize("delay_at", ["startup", "queue"])
def test_ask_absolute_deadline_uses_fake_clock_remaining_rpc_durations(
    monkeypatch, delay_at
):
    now = [100.0]
    budgets = []

    def process_factory(argv, **kwargs):
        process = subprocess.Popen(argv, **kwargs)
        if delay_at == "startup":
            now[0] += 3
        return process

    runtime = WorkIQRuntime(
        command=lambda: command_for("ok"), process_factory=process_factory,
        monotonic_clock=lambda: now[0], terminate_grace=0.01,
    )
    rpc = runtime._rpc

    def record_budget(method, params, timeout, **kwargs):
        result = rpc(method, params, timeout, **kwargs)
        if method == "tools/call" and params["name"] == "ask":
            budgets.append(timeout)
            if len(budgets) == 1:
                now[0] += 2
        return result

    monkeypatch.setattr(runtime, "_rpc", record_budget)
    try:
        if delay_at == "startup":
            assert runtime.execute_ask(ASK_QUESTION, timeout=10) == ASK_DATA
        else:
            runtime.start()
            with runtime._lock:
                job = runtime.submit(
                    "ask", ask=policy.build_ask_operation(ASK_QUESTION), timeout=10
                )
                now[0] += 3
            assert runtime.wait(job, 2)["data"] == ASK_DATA
        assert budgets == [7.0, 5.0]
        assert_ask_scrubbed(runtime)
    finally:
        runtime.shutdown()


def test_ask_queue_delay_and_probe_exhaustion_emit_no_business_call(tmp_path, monkeypatch):
    trace = tmp_path / "queue-probe-budget.jsonl"
    now = [100.0]
    runtime = WorkIQRuntime(
        command=lambda: command_for("ok", trace),
        monotonic_clock=lambda: now[0], terminate_grace=0.01,
    )
    rpc = runtime._rpc

    def consume_probe_budget(method, params, timeout, **kwargs):
        result = rpc(method, params, timeout, **kwargs)
        if method == "tools/call" and params["name"] == "ask":
            now[0] += 2
        return result

    monkeypatch.setattr(runtime, "_rpc", consume_probe_budget)
    try:
        runtime.start()
        with runtime._lock:
            job = runtime.submit("ask", ask=policy.build_ask_operation(ASK_QUESTION), timeout=5)
            now[0] += 3
        result = runtime.wait(job, 2)
        assert result["state"] == "timed_out"
        assert result["error"]["code"] == "timeout"
        assert [m["params"] for m in ask_trace(trace) if m["method"] == "tools/call"] == [
            {"name": "ask", "arguments": {"question": ASK_PROBE}}
        ]
        assert_ask_scrubbed(runtime)
    finally:
        runtime.shutdown()


def test_ask_answer_expiring_during_validation_cannot_publish_success(monkeypatch):
    now = [100.0]
    runtime = WorkIQRuntime(
        command=lambda: command_for("ok"),
        monotonic_clock=lambda: now[0], terminate_grace=0.01,
    )
    validate = runtime._validate_ask_result
    validations = []

    def late_answer(result):
        data = validate(result)
        validations.append(True)
        if len(validations) == 2:
            now[0] = 105.0
        return data

    monkeypatch.setattr(runtime, "_validate_ask_result", late_answer)
    try:
        assert runtime.probe(timeout=2)["ok"]
        with pytest.raises(WorkIQTimeoutError):
            runtime.execute_ask(ASK_QUESTION, timeout=5)
        assert runtime.snapshot()["authenticated"] is False
        assert runtime.snapshot()["state"] == "faulted"
        assert runtime.snapshot()["history"][-1]["state"] == "timed_out"
        assert_ask_scrubbed(runtime)
    finally:
        runtime.shutdown()


def test_ask_deadline_is_rechecked_at_wire_send(tmp_path, monkeypatch):
    trace = tmp_path / "send-deadline.jsonl"
    now = [100.0]
    runtime = WorkIQRuntime(
        command=lambda: command_for("ok", trace),
        monotonic_clock=lambda: now[0], terminate_grace=0.01,
    )
    rpc = runtime._rpc

    def expire_before_send(method, params, timeout, **kwargs):
        if params.get("arguments") == {"question": ASK_QUESTION}:
            now[0] = 105.0
        return rpc(method, params, timeout, **kwargs)

    monkeypatch.setattr(runtime, "_rpc", expire_before_send)
    try:
        with pytest.raises(WorkIQTimeoutError):
            runtime.execute_ask(ASK_QUESTION, timeout=5)
        assert [m["params"] for m in ask_trace(trace) if m["method"] == "tools/call"] == [
            {"name": "ask", "arguments": {"question": ASK_PROBE}}
        ]
        assert_ask_scrubbed(runtime)
    finally:
        runtime.shutdown()


def test_start_initializes_then_notifies_then_lists_tools_before_ready(tmp_path):
    trace = tmp_path / "trace.jsonl"
    runtime = WorkIQRuntime(command=lambda: command_for("ok", trace))
    try:
        snapshot = runtime.start()
        assert snapshot["state"] == "ready"
        assert snapshot["allowed_capabilities"] == ["do_action", "fetch"]
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
        assert snapshot["allowed_capabilities"] == ["do_action", "fetch"]
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
