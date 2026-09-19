"""Bounded recent-chat recovery uses only the owned scripted MCP peer."""

from dataclasses import replace
import json
import threading
from urllib.parse import quote

import pytest

from src.services import workiq_policy as policy, workiq_runtime as wire
from tests.test_workiq_runtime_peer import (
    source_runtime, source_envelope, source_message, source_calls, expected_source_calls,
    assert_source_scrubbed, ask_trace,
    wait_until,
)


CHAT = "19:listed+/thread@unq.gbl.spaces"
TARGET = {"id": "target-id", "email": "target@example.test"}
PROFILE = {"id": "self-id", "mail": "self@example.test", "userPrincipalName": "self@example.test"}
MEMBERS = {"value": [
    {"id": "membership-self", "userId": "self-id", "email": "self@example.test"},
    {"id": "membership-target", "userId": "target-id", "email": "target@example.test"},
]}
LIST_PATH = "/me/chats?$select=id,topic,chatType,webUrl&$top=50"
PROFILE_PATH = "/me?$select=id,mail,userPrincipalName"


def pages(chat_type="oneOnOne", topic=None):
    return [
        {"value": [{"id": CHAT, "topic": topic, "chatType": chat_type, "webUrl": None}]},
        PROFILE, MEMBERS, {"value": [source_message(chatId=CHAT)]},
    ]


def paths():
    return [LIST_PATH, PROFILE_PATH, f"/me/chats/{quote(CHAT, safe='')}/members",
            f"/me/chats/{quote(CHAT, safe='')}/messages?$top=50"]


@pytest.mark.parametrize("kind", ["oneOnOne", "group", "meeting"])
def test_recovery_only_provider_listed_verified_chat_can_be_read(tmp_path, kind):
    runtime, trace = source_runtime(tmp_path, pages(kind, "Captured topic"))
    try:
        result = runtime.recover_chat(TARGET, topic="Captured topic", timeout=3)
        assert result["conversation_id"] == CHAT
        assert result["recovery_kind"] == "recent_chat_membership"
        assert result["locator_source"] == "recovered"
        assert result["items"][0]["excerpt"] == "private-body & answer"
        assert source_calls(trace) == expected_source_calls(paths())
        assert_source_scrubbed(runtime)
        assert all(op.recovery is None for op in runtime._operations.values())
    finally:
        runtime.shutdown()


@pytest.mark.parametrize("target", [{"email": "TARGET@example.test"}, {"id": "target-id"}])
def test_recovery_exact_ids_or_verified_emails_not_names(tmp_path, target):
    runtime, trace = source_runtime(tmp_path, pages())
    try:
        assert runtime.recover_chat(target, timeout=3)["complete"]
    finally:
        runtime.shutdown()


@pytest.mark.parametrize("bad", [
    {}, {"name": "Target"}, {"email": "Display Name"}, {"email": "/o=x/ou=y/cn=Recipients/cn=z"},
    {"email": "target@example.test", "entityUrls": ["/me/messages"]},
    {"id": "target-id", "members": {"id": "override"}}, {"id": "../escape"},
])
def test_recovery_policy_rejects_nested_overrides_and_unverified_inputs(bad):
    with pytest.raises(policy.CapabilityError):
        policy.build_recovery_operation(bad)


def test_recovery_policy_seal_rejects_dataclass_replacement():
    minted = policy.build_recovery_operation(TARGET, topic="Captured")
    for forged in [replace(minted, target_id="other"), replace(minted, topic="other")]:
        with pytest.raises(policy.CapabilityError):
            policy.require_recovery_operation(forged)


@pytest.mark.parametrize("mutation", ["ambiguous", "no-self", "wrong-target", "names-only", "members-partial",
    "list-partial", "overflow", "unsafe-id", "duplicate-id", "group-without-topic", "wrong-topic", "member-overflow"])
def test_recovery_unsafe_ambiguous_or_incomplete_always_fails_closed(tmp_path, mutation):
    data = json.loads(json.dumps(pages()))
    if mutation == "ambiguous":
        data[0]["value"].append({**data[0]["value"][0], "id": "other-chat"})
        data.insert(3, MEMBERS)
    elif mutation == "no-self":
        data[2]["value"][0]["userId"] = "other-self"
        data[2]["value"][0]["email"] = "other@example.test"
    elif mutation == "wrong-target":
        data[2]["value"][1].update(userId="different", email="other@example.test")
    elif mutation == "names-only":
        data[2] = {"value": [{"displayName": "Self"}, {"displayName": "Target"}]}
    elif mutation == "members-partial":
        data[2]["@odata.nextLink"] = "https://graph.microsoft.com/next"
    elif mutation == "list-partial":
        data[0]["@odata.nextLink"] = "https://graph.microsoft.com/next"
    elif mutation == "overflow":
        data[0]["value"] *= 51
    elif mutation == "unsafe-id":
        data[0]["value"][0]["id"] = "x?$select=secrets"
    elif mutation == "duplicate-id":
        data[0]["value"] *= 2
    elif mutation in {"group-without-topic", "wrong-topic"}:
        data[0]["value"][0].update(chatType="group", topic="Different")
    elif mutation == "member-overflow":
        data[2]["value"] *= 26
    runtime, trace = source_runtime(tmp_path, data)
    try:
        with pytest.raises(wire.SourceUnreadableError):
            runtime.recover_chat(TARGET, topic="Exact" if mutation == "wrong-topic" else None, timeout=3)
        urls = [call["arguments"]["entityUrls"][0] for call in source_calls(trace)]
        assert not any("/messages" in url for url in urls)
        assert not any("next" in url for url in urls)
        assert all(len(call["arguments"]["entityUrls"]) == 1 for call in source_calls(trace))
        assert_source_scrubbed(runtime)
    finally:
        runtime.shutdown()


def test_recovery_maximum_fifty_membership_reads_never_paginates(tmp_path):
    data = [{"value": [{"id": f"candidate-{i}", "chatType": "oneOnOne", "topic": None} for i in range(50)]}, PROFILE]
    data += [{"value": [
        MEMBERS["value"][0], {**MEMBERS["value"][1], "userId": "not-target", "email": "not-target@example.test"},
    ]}] * 50
    runtime, trace = source_runtime(tmp_path, data)
    try:
        with pytest.raises(wire.SourceUnreadableError):
            runtime.recover_chat(TARGET, timeout=10)
        calls = source_calls(trace)
        assert len(calls) == 52
        assert sum("/members" in c["arguments"]["entityUrls"][0] for c in calls) == 50
        assert all("$top" not in c["arguments"]["entityUrls"][0] for c in calls[2:])
    finally:
        runtime.shutdown()


@pytest.mark.parametrize("status,error", [(401, wire.AuthRequiredError), (403, wire.SourceForbiddenError),
                                         (404, wire.SourceNotFoundError), (503, wire.SourceHTTPError)])
def test_recovery_http_errors_are_typed_not_empty(tmp_path, status, error):
    runtime, trace = source_runtime(tmp_path, steps=[{"result": source_envelope({}, status)}])
    try:
        with pytest.raises(error):
            runtime.recover_chat(TARGET, timeout=3)
        assert len(source_calls(trace)) == 1
        assert_source_scrubbed(runtime)
    finally:
        runtime.shutdown()


def test_recovery_deadline_spans_all_hops(tmp_path):
    runtime, trace = source_runtime(tmp_path, steps=[
        {"result": source_envelope(page), "delay": .13} for page in pages()
    ])
    try:
        with pytest.raises(wire.TimeoutError):
            runtime.recover_chat(TARGET, timeout=.35)
        assert len(source_calls(trace)) < 4
        assert all(op.recovery is None for op in runtime._operations.values())
        assert_source_scrubbed(runtime)
    finally:
        runtime.shutdown()


def test_recovery_generic_wait_hides_data_and_operation_is_single_use(tmp_path):
    runtime, _ = source_runtime(tmp_path, pages())
    try:
        runtime.start()
        job = runtime.submit("recovery", recovery=policy.build_recovery_operation(TARGET), timeout=3)
        result = runtime.wait(job)
        assert result["ok"] and "data" not in result
        assert "data" not in runtime.wait(job)
        assert_source_scrubbed(runtime)
    finally:
        runtime.shutdown()


@pytest.mark.parametrize("end", ["cancel", "shutdown", "timeout"])
def test_recovery_active_and_queued_terminal_paths_are_private(tmp_path, end):
    runtime, trace = source_runtime(tmp_path, steps=[{"hang": True}])
    try:
        runtime.start()
        active = runtime.submit("recovery", recovery=policy.build_recovery_operation(TARGET),
                                timeout=.25 if end == "timeout" else 3)
        assert wait_until(lambda: bool(source_calls(trace)))
        queued = runtime.submit("recovery", recovery=policy.build_recovery_operation(TARGET), timeout=3)
        assert runtime.cancel(queued)
        if end == "cancel":
            runtime.cancel(active)
        elif end == "shutdown":
            runtime.shutdown()
        result = runtime.wait(active, 3)
        assert result["state"] == ("timed_out" if end == "timeout" else "cancelled")
        assert runtime.wait(queued)["state"] == "cancelled"
        assert len(source_calls(trace)) == 1
        assert all(op.recovery is None for op in runtime._operations.values())
        assert_source_scrubbed(runtime)
    finally:
        runtime.shutdown()


@pytest.mark.parametrize("gate", ["cancel", "expire"])
@pytest.mark.parametrize("hop", [1, 2, 3])
def test_recovery_atomic_wire_gates_between_each_hop(tmp_path, monkeypatch, gate, hop):
    runtime, trace = source_runtime(tmp_path, pages())
    original = runtime._validate_source_envelope
    count = [0]
    def gated(result):
        data = original(result)
        count[0] += 1
        if count[0] == hop:
            operation = runtime._active_operation
            if gate == "cancel":
                runtime.cancel(operation.job_id)
            else:
                operation.ask_deadline = runtime._monotonic_clock() - 1
        return data
    monkeypatch.setattr(runtime, "_validate_source_envelope", gated)
    try:
        with pytest.raises((wire.TimeoutError, wire.CancelledError)):
            runtime.recover_chat(TARGET, timeout=3)
        assert len(source_calls(trace)) == hop
        assert all(op.recovery is None and op.source is None for op in runtime._operations.values())
        assert_source_scrubbed(runtime)
    finally:
        runtime.shutdown()


def test_recovery_cold_start_consumes_its_admission_deadline(tmp_path):
    runtime, trace = source_runtime(tmp_path, pages(), scenario="hang-initialize", startup_timeout=3)
    try:
        with pytest.raises(wire.TimeoutError):
            runtime.recover_chat(TARGET, timeout=.1)
        assert source_calls(trace) == []
        assert_source_scrubbed(runtime)
    finally:
        runtime.shutdown()


def test_recovery_shared_fifo_queue_time_and_all_hops_reduce_budget(tmp_path, monkeypatch):
    now = [100.0]
    runtime, trace = source_runtime(tmp_path, pages(), monotonic_clock=lambda: now[0])
    budgets = []
    try:
        runtime.start()
        enqueue, rpc = runtime._enqueue_operation, runtime._rpc
        def delayed(operation):
            now[0] += 3
            return enqueue(operation)
        def timed(method, params, timeout, **kw):
            budgets.append(timeout)
            result = rpc(method, params, timeout, **kw)
            now[0] += 1
            return result
        monkeypatch.setattr(runtime, "_enqueue_operation", delayed)
        monkeypatch.setattr(runtime, "_rpc", timed)
        assert runtime.recover_chat(TARGET, timeout=10)["complete"]
        assert budgets == [7, 6, 5, 4]
        assert_source_scrubbed(runtime)
    finally:
        runtime.shutdown()


def test_recovery_late_projection_cannot_publish_result(tmp_path, monkeypatch):
    now = [100.0]
    runtime, trace = source_runtime(tmp_path, pages(), monotonic_clock=lambda: now[0])
    original = runtime._project_source_page
    def late(*args):
        result = original(*args)
        now[0] = 200
        return result
    monkeypatch.setattr(runtime, "_project_source_page", late)
    try:
        with pytest.raises(wire.TimeoutError):
            runtime.recover_chat(TARGET, timeout=10)
        assert all(op.recovery is None and op.source is None for op in runtime._operations.values())
        assert_source_scrubbed(runtime)
    finally:
        runtime.shutdown()


@pytest.mark.parametrize("mutation", ["third-member", "x500-email", "nullable", "self-is-target", "spacing-topic", "case-topic"])
def test_recovery_identity_and_topic_near_matches_do_not_prove_chat(tmp_path, mutation):
    data = json.loads(json.dumps(pages("group" if "topic" in mutation else "oneOnOne", "Captured Topic")))
    target = TARGET
    if mutation == "third-member":
        data[2]["value"].append({"userId": "third-id", "email": "third@example.test"})
    elif mutation == "x500-email":
        data[2]["value"][1]["email"] = "/o=Org/ou=Unit/cn=Recipients/cn=target"
    elif mutation == "nullable":
        data[2]["value"][1].update(email=None, userId=None)
    elif mutation == "self-is-target":
        target = {"id": "self-id"}
    elif mutation == "spacing-topic":
        data[0]["value"][0]["topic"] = "Captured Topic "
    else:
        data[0]["value"][0]["topic"] = "captured topic"
    runtime, trace = source_runtime(tmp_path, data)
    try:
        with pytest.raises(wire.SourceUnreadableError):
            runtime.recover_chat(target, topic="Captured Topic", timeout=3)
        assert all("/messages" not in call["arguments"]["entityUrls"][0] for call in source_calls(trace))
        assert_source_scrubbed(runtime)
    finally:
        runtime.shutdown()
