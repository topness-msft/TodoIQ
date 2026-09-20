"""Saved exact Teams participants share one FIFO operation and caller deadline."""

from dataclasses import replace
import subprocess
import threading
from urllib.parse import quote

import pytest

from src.services import workiq_policy as policy, workiq_runtime as wire
from tests.test_workiq_runtime_peer import (
    source_runtime, source_envelope, source_message, source_calls,
    expected_source_calls, source_locator, wait_until,
    ASK_QUESTION, SCHEDULE_BODY, ask_trace, configured_readiness_account,
)
from tests.test_workiq_directory_profiles import (
    aad, profile, projected, aad_path, SELF_PATH, assert_directory_scrubbed,
)


CHAT = "19:directory-chat-private+/thread@unq.gbl.spaces"
MEMBERS_PATH = f"/me/chats/{quote(CHAT, safe='')}/members"
CONTEXT_PATH = (
    f"/me/chats/{quote(CHAT, safe='')}/messages?"
    "$select=id,chatId,createdDateTime,from,body,webUrl&$top=20"
)


def locator(**changes):
    return {**source_locator(conversation_id=CHAT), **changes}


def member(index=1, **changes):
    return {"id": f"membership-private-{index}", "userId": aad(index),
            "email": f"person{index}@example.test", "displayName": "Not identity", **changes}


def pages():
    return [profile(), {"value": [member(), member(2)]}, profile(2),
            {"value": [source_message(chatId=CHAT)]}]


def paths():
    return [SELF_PATH, MEMBERS_PATH, aad_path(2), CONTEXT_PATH]


def test_saved_policy_only_exact_resolved_chat_and_value_bound_nested_seal():
    sealed = policy.build_saved_teams_operation(locator())
    assert policy.require_saved_teams_operation(sealed) == sealed
    other = policy.build_source_operation(locator(conversation_id="foreign-chat"))
    for forged in [replace(sealed, source=other), replace(sealed, _mint=object()),
                   replace(sealed, source=replace(sealed.source, identifiers=(("conversation_id", "foreign"),)))]:
        with pytest.raises(policy.CapabilityError):
            policy.require_saved_teams_operation(forged)


@pytest.mark.parametrize("bad", [
    CHAT, {"conversation_id": CHAT}, locator(kind="meeting"), locator(kind="email"),
    locator(kind="teams_channel"), locator(source="model"), locator(source_id=CHAT),
    locator(entityUrl="/me/chats/foreign"), locator(team_id="foreign"),
    locator(conversation_id="x?$top=50"), locator(conversation_id=None),
])
def test_saved_bad_input_rejected_before_any_startup(bad):
    runtime = wire.WorkIQRuntime(command=lambda: pytest.fail("must not launch"))
    with pytest.raises(policy.CapabilityError):
        runtime.read_saved_teams_chat_participants(bad, timeout=3)


def test_saved_exact_sequence_projection_and_all_pii_transient(tmp_path):
    runtime, trace = source_runtime(tmp_path, pages())
    try:
        result = runtime.read_saved_teams_chat_participants(locator(), timeout=3)
        assert set(result) == {"conversation_id", "self", "participants", "recent_context"}
        assert result["conversation_id"] == CHAT
        assert result["self"] == projected()
        assert result["participants"] == [{
            "membership_id": "membership-private-2", "profile": projected(2),
            "resolution": "confirmed_internal",
        }]
        assert result["recent_context"] == {
            "context_only": True, "complete": True,
            "items": [{
                "source_item_id": "private-message", "occurred_at": "2026-09-19T18:30:00Z",
                "sender": {"id": "private-sender-id", "display_name": "Private Sender",
                           "address": None, "address_kind": None},
                "excerpt": "private-body & answer",
                "web_url": "https://teams.microsoft.com/l/message/private-web-link-token",
            }],
        }
        assert source_calls(trace) == expected_source_calls(paths())
        assert_directory_scrubbed(runtime)
    finally:
        runtime.shutdown()


@pytest.mark.parametrize("uppercase", ["self-profile", "self-member", "member", "member-profile", "all"])
def test_saved_aad_case_variants_preserve_self_exclusion_and_profile_identity(tmp_path, uppercase):
    data = pages()
    if uppercase in {"self-profile", "all"}:
        data[0]["id"] = aad().upper()
    if uppercase in {"self-member", "all"}:
        data[1]["value"][0]["userId"] = aad().upper()
    if uppercase in {"member", "all"}:
        data[1]["value"][1]["userId"] = aad(2).upper()
    if uppercase in {"member-profile", "all"}:
        data[2]["id"] = aad(2).upper()
    runtime, trace = source_runtime(tmp_path, data)
    try:
        result = runtime.read_saved_teams_chat_participants(locator(), timeout=3)
        assert result["self"] == projected()
        assert result["participants"] == [{
            "membership_id": "membership-private-2", "profile": projected(2),
            "resolution": "confirmed_internal",
        }]
        assert source_calls(trace) == expected_source_calls(paths())
        assert_directory_scrubbed(runtime)
    finally:
        runtime.shutdown()


def test_saved_opaque_chat_and_message_identifiers_keep_their_case(tmp_path):
    chat = CHAT.upper()
    data = pages()
    data[-1]["value"][0].update(chatId=chat, id="Private-Message-MiXeD")
    runtime, trace = source_runtime(tmp_path, data)
    try:
        result = runtime.read_saved_teams_chat_participants(locator(conversation_id=chat), timeout=3)
        assert result["conversation_id"] == chat
        assert result["recent_context"]["items"][0]["source_item_id"] == "Private-Message-MiXeD"
        assert source_calls(trace) == expected_source_calls([
            path.replace(quote(CHAT, safe=""), quote(chat, safe="")) for path in paths()
        ])
        assert_directory_scrubbed(runtime)
    finally:
        runtime.shutdown()


@pytest.mark.parametrize("mutation", [
    "missing-self", "duplicate-self", "duplicate-user", "duplicate-membership",
    "case-duplicate-self", "case-duplicate-user",
    "partial", "overflow", "malformed", "missing-userid", "encoded-arbitrary-id",
    "bad-guid", "conflicting-self-email", "other-self-email", "bad-email",
])
def test_membership_failure_is_whole_read_failure_before_profile_or_context(tmp_path, mutation):
    data = pages()
    rows = data[1]["value"]
    if mutation == "missing-self":
        rows[0] = member(3)
    elif mutation in {"duplicate-self", "duplicate-user"}:
        rows.append(member(1 if mutation == "duplicate-self" else 2, id="different-membership"))
    elif mutation in {"case-duplicate-self", "case-duplicate-user"}:
        index = 1 if mutation == "case-duplicate-self" else 2
        rows.append(member(index, id="different-membership", userId=aad(index).upper(), email=None))
    elif mutation == "duplicate-membership":
        rows[1]["id"] = rows[0]["id"]
    elif mutation == "partial":
        data[1]["@odata.nextLink"] = "https://graph.microsoft.com/next"
    elif mutation == "overflow":
        data[1]["value"] = [member(i) for i in range(1, 52)]
    elif mutation == "malformed":
        rows.append(None)
    elif mutation in {"missing-userid", "encoded-arbitrary-id"}:
        rows[1].pop("userId")
        if mutation == "encoded-arbitrary-id":
            import base64
            rows[1]["id"] = base64.b64encode(f"unproven-prefix:{aad(2)}".encode()).decode()
    elif mutation == "bad-guid":
        rows[1]["userId"] = "not-a-guid"
    elif mutation == "conflicting-self-email":
        rows[0]["email"] = "different@example.test"
    elif mutation == "other-self-email":
        rows[1]["email"] = "person1@example.test"
    else:
        rows[1]["email"] = "/o=Exchange/cn=someone"
    runtime, trace = source_runtime(tmp_path, data)
    try:
        with pytest.raises(wire.SourceUnreadableError):
            runtime.read_saved_teams_chat_participants(locator(), timeout=3)
        assert source_calls(trace) == expected_source_calls(paths()[:2])
        assert_directory_scrubbed(runtime)
    finally:
        runtime.shutdown()


def test_complete_fifty_members_is_allowed_without_top_or_pagination(tmp_path):
    data = [profile(), {"value": [member(i) for i in range(1, 51)]}]
    data += [profile(i) for i in range(2, 51)] + [{"value": []}]
    runtime, trace = source_runtime(tmp_path, data)
    try:
        result = runtime.read_saved_teams_chat_participants(locator(), timeout=15)
        assert [row["profile"]["aad_object_id"] for row in result["participants"]] == [aad(i) for i in range(2, 51)]
        assert source_calls(trace) == expected_source_calls(
            [SELF_PATH, MEMBERS_PATH] + [aad_path(i) for i in range(2, 51)] + [CONTEXT_PATH],
        )
        assert_directory_scrubbed(runtime)
    finally:
        runtime.shutdown()


@pytest.mark.parametrize("changes", [
    {"id": aad(3)}, {"id": aad(3).upper()}, {"userType": "Other"},
    {"mail": None, "userPrincipalName": None}, {"mail": "other@example.test"},
    {"displayName": ""}, {"value": []},
    {"@odata.nextLink": "https://graph.microsoft.com/next"},
])
def test_invalid_profile_never_publishes_partial_people_or_reads_context(tmp_path, changes):
    data = pages()
    data[1]["value"].append(member(3))
    data[2].update(changes)
    runtime, trace = source_runtime(tmp_path, data)
    try:
        with pytest.raises(wire.SourceUnreadableError):
            runtime.read_saved_teams_chat_participants(locator(), timeout=3)
        assert source_calls(trace) == expected_source_calls(paths()[:3])
        assert_directory_scrubbed(runtime)
    finally:
        runtime.shutdown()


def test_self_exclusion_uses_only_exact_id_and_guest_remains_unresolved(tmp_path):
    data = pages()
    data[1]["value"][0]["displayName"] = "Different Self Display Name"
    data[2].update(displayName="Synthetic Person 1", userType="Guest")
    runtime, trace = source_runtime(tmp_path, data)
    try:
        result = runtime.read_saved_teams_chat_participants(locator(), timeout=3)
        assert result["participants"] == [{
            "membership_id": "membership-private-2",
            "profile": projected(2, display_name="Synthetic Person 1", user_type="Guest", resolution="external_unresolved"),
            "resolution": "external_unresolved",
        }]
        assert_directory_scrubbed(runtime)
    finally:
        runtime.shutdown()


@pytest.mark.parametrize("pagination", ["none", "next", "link", "both"])
@pytest.mark.parametrize("count", [0, 20])
def test_recent_context_partial_label_bounds_and_no_absence_claim(tmp_path, pagination, count):
    data = pages()
    data[-1]["value"] = [source_message(chatId=CHAT, id=f"private-message-{i}") for i in range(count)]
    steps = [{"result": source_envelope(page)} for page in data]
    if pagination in {"next", "both"}:
        data[-1]["@odata.nextLink"] = "https://graph.microsoft.com/next"
    if pagination in {"link", "both"}:
        steps[-1]["result"]["structuredContent"]["results"][0]["headers"] = {"Link": "<https://graph.microsoft.com/next>; rel=next"}
    runtime, trace = source_runtime(tmp_path, steps=steps)
    try:
        context = runtime.read_saved_teams_chat_participants(locator(), timeout=3)["recent_context"]
        assert context["context_only"] is True
        assert context["complete"] is (pagination == "none")
        assert set(context) == {"context_only", "complete", "items"}
        assert len(context["items"]) == count
        assert source_calls(trace) == expected_source_calls(paths())
        assert_directory_scrubbed(runtime)
    finally:
        runtime.shutdown()


@pytest.mark.parametrize("mutation", ["overflow", "foreign-chat", "chat-case", "missing-chat", "duplicate",
                                     "bad-time", "bad-url", "bad-next", "bad-header"])
def test_recent_context_still_rejects_invalid_or_foreign_messages(tmp_path, mutation):
    data = pages()
    message = data[-1]["value"][0]
    if mutation in {"overflow", "duplicate"}:
        data[-1]["value"] = [source_message(chatId=CHAT, id=f"m{i}" if mutation == "overflow" else "same")
                              for i in range(21 if mutation == "overflow" else 2)]
    elif mutation == "foreign-chat":
        message["chatId"] = "foreign"
    elif mutation == "chat-case":
        message["chatId"] = CHAT.upper()
    elif mutation == "missing-chat":
        message.pop("chatId")
    elif mutation == "bad-time":
        message["createdDateTime"] = "yesterday"
    elif mutation == "bad-url":
        message["webUrl"] = "https://untrusted.example.test/private"
    elif mutation == "bad-next":
        data[-1]["@odata.nextLink"] = "https://untrusted.example.test/next"
    steps = [{"result": source_envelope(page)} for page in data]
    if mutation == "bad-header":
        steps[-1]["result"]["structuredContent"]["results"][0]["headers"] = {"Link": "bad\nheader"}
    runtime, trace = source_runtime(tmp_path, steps=steps)
    try:
        with pytest.raises(wire.SourceUnreadableError):
            runtime.read_saved_teams_chat_participants(locator(), timeout=3)
        assert source_calls(trace) == expected_source_calls(paths())
        assert_directory_scrubbed(runtime)
    finally:
        runtime.shutdown()


@pytest.mark.parametrize("pagination", ["next", "link"])
def test_context_opt_in_does_not_weaken_existing_strict_source(tmp_path, pagination):
    data = {"value": [source_message(chatId=CHAT)]}
    result = source_envelope(data)
    if pagination == "next":
        data["@odata.nextLink"] = "https://graph.microsoft.com/next"
    else:
        result["structuredContent"]["results"][0]["headers"] = {"Link": "<https://graph.microsoft.com/next>; rel=next"}
    runtime, trace = source_runtime(tmp_path, steps=[{"result": result}])
    try:
        with pytest.raises(wire.SourcePartialError):
            runtime.read_source(locator(), timeout=3)
        assert len(source_calls(trace)) == 1
        assert_directory_scrubbed(runtime)
    finally:
        runtime.shutdown()


@pytest.mark.parametrize("gate", ["cancel", "expire"])
@pytest.mark.parametrize("hop", [1, 2, 3, 4])
def test_cancellation_and_deadline_at_every_hop_stop_next_wire_or_publication(tmp_path, monkeypatch, gate, hop):
    runtime, trace = source_runtime(tmp_path, pages())
    original = runtime._source_fetch
    count = [0]
    def gated(operation, *args, **kwargs):
        result = original(operation, *args, **kwargs)
        count[0] += 1
        if count[0] == hop:
            if gate == "cancel":
                runtime.cancel(operation.job_id)
            else:
                operation.ask_deadline = runtime._monotonic_clock() - 1
        return result
    monkeypatch.setattr(runtime, "_source_fetch", gated)
    try:
        with pytest.raises(wire.CancelledError if gate == "cancel" else wire.TimeoutError):
            runtime.read_saved_teams_chat_participants(locator(), timeout=3)
        assert source_calls(trace) == expected_source_calls(paths()[:hop])
        assert_directory_scrubbed(runtime)
    finally:
        runtime.shutdown()


def test_one_admission_deadline_spans_queue_self_members_profile_context(tmp_path, monkeypatch):
    now = [100.0]
    runtime, trace = source_runtime(tmp_path, pages(), monotonic_clock=lambda: now[0])
    budgets = []
    try:
        runtime.start()
        enqueue, rpc = runtime._enqueue_operation, runtime._rpc
        def queued(operation):
            now[0] += 3
            return enqueue(operation)
        def timed(method, params, timeout, **kwargs):
            budgets.append(timeout)
            result = rpc(method, params, timeout, **kwargs)
            now[0] += 1
            return result
        monkeypatch.setattr(runtime, "_enqueue_operation", queued)
        monkeypatch.setattr(runtime, "_rpc", timed)
        assert runtime.read_saved_teams_chat_participants(locator(), timeout=10)["participants"]
        assert budgets == [7, 6, 5, 4]
        assert_directory_scrubbed(runtime)
    finally:
        runtime.shutdown()


@pytest.mark.parametrize("read", ["read_self_profile", "read_saved_teams_chat_participants"])
def test_cold_start_deadline_owns_abort_and_never_fetches(tmp_path, read):
    runtime, trace = source_runtime(tmp_path, pages(), scenario="hang-initialize", startup_timeout=3)
    try:
        with pytest.raises(wire.TimeoutError):
            getattr(runtime, read)(*([locator()] if "participants" in read else []), timeout=.1)
        assert source_calls(trace) == []
        assert_directory_scrubbed(runtime)
    finally:
        runtime.shutdown()


@pytest.mark.parametrize("end", ["cancel", "shutdown", "timeout"])
def test_active_terminal_paths_scrub_private_operations(tmp_path, end):
    runtime, trace = source_runtime(tmp_path, steps=[{"hang": True}])
    errors = []
    def read():
        try:
            runtime.read_saved_teams_chat_participants(locator(), timeout=.4 if end == "timeout" else 3)
        except wire.WorkIQError as error:
            errors.append(error.code)
    thread = threading.Thread(target=read)
    try:
        runtime.start()
        thread.start()
        assert wait_until(lambda: bool(source_calls(trace)))
        active = runtime._active_operation
        if end == "cancel":
            runtime.cancel(active.job_id)
        elif end == "shutdown":
            runtime.shutdown()
        thread.join(4)
        assert not thread.is_alive()
        assert errors == ["timeout" if end == "timeout" else "cancelled"]
        assert len(source_calls(trace)) == 1
        assert_directory_scrubbed(runtime)
    finally:
        runtime.shutdown()
        thread.join(4)


def test_one_late_bad_profile_invalidates_every_preceding_participant(tmp_path):
    data = pages()
    data[1]["value"].append(member(3))
    data.insert(3, profile(3, mail=None, userPrincipalName=None))
    runtime, trace = source_runtime(tmp_path, data)
    try:
        with pytest.raises(wire.SourceUnreadableError):
            runtime.read_saved_teams_chat_participants(locator(), timeout=3)
        assert source_calls(trace) == expected_source_calls(paths()[:3] + [aad_path(3)])
        assert_directory_scrubbed(runtime)
    finally:
        runtime.shutdown()


def test_guest_without_addresses_is_retained_only_as_unresolved(tmp_path):
    data = pages()
    data[1]["value"][1]["email"] = None
    data[2].update(userType="Guest", mail=None, userPrincipalName=None)
    runtime, _ = source_runtime(tmp_path, data)
    try:
        result = runtime.read_saved_teams_chat_participants(locator(), timeout=3)
        assert result["participants"][0]["profile"] == projected(
            2, email=None, upn=None, user_type="Guest", resolution="external_unresolved",
        )
        assert result["participants"][0]["resolution"] == "external_unresolved"
        assert_directory_scrubbed(runtime)
    finally:
        runtime.shutdown()


@pytest.mark.parametrize("method", ["read_self_profile", "read_saved_teams_chat_participants"])
@pytest.mark.parametrize("gate", ["cancel", "expire"])
def test_queued_directory_expiry_or_cancel_does_not_revoke_active_calendar(tmp_path, monkeypatch, method, gate):
    now = [100.0]
    runtime, trace = source_runtime(tmp_path, pages(), monotonic_clock=lambda: now[0])
    entered, release = threading.Event(), threading.Event()
    try:
        assert runtime.probe(timeout=3)["ok"]
        original_calendar = runtime._run_calendar
        def blocked(operation):
            entered.set()
            assert release.wait(3)
            original_calendar(operation)
        monkeypatch.setattr(runtime, "_run_calendar", blocked)
        calendar = runtime.submit("calendar", timeout=3, calendar=policy.build_calendar_operation(
            policy.CalendarAction.GET_SCHEDULE, SCHEDULE_BODY,
        ))
        assert entered.wait(1)
        child = runtime._process
        enqueue = runtime._enqueue_operation
        def gated(operation):
            job = enqueue(operation)
            if gate == "cancel":
                runtime.cancel(job)
            else:
                now[0] += 4
            return job
        monkeypatch.setattr(runtime, "_enqueue_operation", gated)
        with pytest.raises(wire.CancelledError if gate == "cancel" else wire.TimeoutError):
            getattr(runtime, method)(*([locator()] if "participants" in method else []), timeout=3)
        assert runtime._process is child and child.poll() is None
        assert runtime.snapshot()["authenticated"] is True
        release.set()
        assert runtime.wait(calendar, 3)["ok"]
        assert source_calls(trace) == []
        assert_directory_scrubbed(runtime)
    finally:
        release.set()
        runtime.shutdown()


@pytest.mark.parametrize("method", ["read_self_profile", "read_saved_teams_chat_participants"])
def test_start_lock_waiter_cannot_abort_independently_owned_startup(tmp_path, method):
    entered, release = threading.Event(), threading.Event()
    owner_results = []
    def launch(argv, **kwargs):
        entered.set()
        assert release.wait(3)
        return subprocess.Popen(argv, **kwargs)
    runtime, trace = source_runtime(tmp_path, pages(), process_factory=launch, startup_timeout=3)
    owner = threading.Thread(target=lambda: owner_results.append(runtime.start()))
    try:
        owner.start()
        assert entered.wait(1)
        with pytest.raises(wire.TimeoutError):
            getattr(runtime, method)(*([locator()] if "participants" in method else []), timeout=.05)
        assert runtime.snapshot()["state"] == "starting"
        assert not runtime._stopping.is_set()
        assert not source_calls(trace)
        release.set()
        owner.join(3)
        assert owner_results[0]["state"] == "ready"
        assert runtime.read_self_profile(timeout=3) == projected()
        assert_directory_scrubbed(runtime)
    finally:
        release.set()
        owner.join(3)
        runtime.shutdown()


def test_mixed_fifo_keeps_all_saved_chat_hops_adjacent(tmp_path, monkeypatch):
    runtime, trace = source_runtime(tmp_path, pages())
    entered, release, queued = threading.Event(), threading.Event(), threading.Event()
    results = []
    reader = threading.Thread(target=lambda: results.append(
        runtime.read_saved_teams_chat_participants(locator(), timeout=5),
    ))
    try:
        assert runtime.probe(timeout=3)["ok"]
        assert runtime.probe_ask(timeout=3)["ok"]
        original_ask, enqueue = runtime._run_ask_operation, runtime._enqueue_operation
        def blocked(operation, legacy_state):
            entered.set()
            assert release.wait(3)
            original_ask(operation, legacy_state)
        def admitted(operation):
            job = enqueue(operation)
            if operation.plan == "saved_teams":
                queued.set()
            return job
        monkeypatch.setattr(runtime, "_run_ask_operation", blocked)
        monkeypatch.setattr(runtime, "_enqueue_operation", admitted)
        ask = runtime.submit("ask", ask=policy.build_ask_operation(ASK_QUESTION), timeout=5)
        assert entered.wait(1)
        reader.start()
        assert queued.wait(1)
        calendar = runtime.submit("calendar", timeout=5, calendar=policy.build_calendar_operation(
            policy.CalendarAction.GET_SCHEDULE, SCHEDULE_BODY,
        ))
        sent = runtime.submit("sent_items", timeout=5)
        release.set()
        assert all(runtime.wait(job, 5)["ok"] for job in [ask, calendar, sent])
        reader.join(5)
        assert results[0]["participants"][0]["profile"] == projected(2)
        calls = [message["params"] for message in ask_trace(trace) if message["method"] == "tools/call"]
        assert calls[2:] == [
            {"name": "ask", "arguments": {"question": ASK_QUESTION}},
            *expected_source_calls(paths()),
            {"name": "do_action", "arguments": {"actionUrl": "/me/calendar/getSchedule", "jsonBody": SCHEDULE_BODY}},
            {"name": "fetch", "arguments": {"entityUrls": [policy.SENT_ITEMS_URL]}},
        ]
        assert_directory_scrubbed(runtime)
    finally:
        release.set()
        runtime.shutdown()
        if reader.ident:
            reader.join(5)


@pytest.mark.parametrize("late", ["projection", "caller", "caller-race"])
def test_late_result_publication_never_escapes_deadline_or_scrubbing(tmp_path, monkeypatch, late):
    now = [100.0]
    runtime, _ = source_runtime(tmp_path, pages(), monotonic_clock=lambda: now[0])
    if late == "projection":
        original = runtime._project_source_page
        def expired(*args, **kwargs):
            result = original(*args, **kwargs)
            now[0] += 20
            return result
        monkeypatch.setattr(runtime, "_project_source_page", expired)
    else:
        original = runtime._wait_operation
        def expired(job_id, *args, **kwargs):
            result = original(job_id, *args, **kwargs)
            now[0] += 20
            return {"state": "running"} if late == "caller-race" else result
        monkeypatch.setattr(runtime, "_wait_operation", expired)
    try:
        with pytest.raises(wire.TimeoutError):
            runtime.read_saved_teams_chat_participants(locator(), timeout=10)
        assert_directory_scrubbed(runtime)
    finally:
        runtime.shutdown()


def test_generic_wait_before_facade_consumption_hides_and_discards_data(tmp_path, monkeypatch):
    runtime, _ = source_runtime(tmp_path, [profile()])
    original = runtime._wait_operation
    observed = []
    def consuming(job_id, *args, **kwargs):
        if kwargs.get("source_data"):
            observed.append(original(job_id))
        return original(job_id, *args, **kwargs)
    monkeypatch.setattr(runtime, "_wait_operation", consuming)
    try:
        with pytest.raises(wire.SourceUnreadableError):
            runtime.read_self_profile(timeout=3)
        assert observed[0]["ok"] is True and "data" not in observed[0]
        assert_directory_scrubbed(runtime)
    finally:
        runtime.shutdown()


def test_partial_context_opt_in_rejects_other_operation_or_path_before_io(tmp_path):
    runtime, trace = source_runtime(tmp_path, pages())
    sealed = policy.build_saved_teams_operation(locator())
    operation = wire._Operation(job_id="private-test", timeout=3, plan="source")
    for path in [MEMBERS_PATH, CONTEXT_PATH]:
        with pytest.raises(wire.CapabilityDeniedError):
            runtime._source_fetch(operation, path, context=sealed)
    operation.plan, operation.saved_teams = "saved_teams", sealed
    with pytest.raises(wire.CapabilityDeniedError):
        runtime._source_fetch(operation, MEMBERS_PATH, context=sealed)
    assert not source_calls(trace)


def test_generic_wait_never_exposes_sealed_directory_data(tmp_path, monkeypatch):
    runtime, _ = source_runtime(tmp_path, [profile()])
    original = runtime._wait_operation
    observed = []
    def wait(job_id, *args, **kwargs):
        result = original(job_id, *args, **kwargs)
        observed.append(runtime.wait(job_id) if kwargs.get("source_data") else result)
        return result
    monkeypatch.setattr(runtime, "_wait_operation", wait)
    try:
        assert runtime.read_self_profile(timeout=3) == projected()
        assert all("data" not in result for result in observed)
        assert_directory_scrubbed(runtime)
    finally:
        runtime.shutdown()
