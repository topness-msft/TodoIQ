"""Direct waiting checks use synthetic rows and fake facades only."""

from datetime import datetime, timedelta, timezone
import json
import threading
from unittest.mock import Mock

import pytest

from src import app, db, models
from src.services import checks, claude_runner, source_locator, waiting_activity
from src.services import workiq_runtime as wire
from tests.test_suggestion_check_workflow import raw, edit, finish


def presence(ooo=False, **over):
    return json.dumps({
        "version": 1, "out_of_office": ooo, "summary": "Synthetic presence",
        "return_date": "2026-10-01" if ooo else None, "answers": [], **over,
    })


def finding(status="activity_detected", **over):
    return json.dumps({
        "version": 1, "status": status, "summary": "Synthetic finding",
        "return_date": None, "evidence": [], "answers": [], **over,
    })


def source(*, items=None):
    return {
        "source_kind": "teams_chat", "locator_source": "captured",
        "source_identity": {"conversation_id": "real-thread"},
        "conversation_id": "real-thread", "complete": True,
        "items": items if items is not None else [{
            "source_item_id": "real-message", "occurred_at": "2026-09-19T18:00:00Z",
            "sender": {"id": "target-id", "display_name": "Person",
                       "address": "person@example.test", "address_kind": "smtp"},
            "excerpt": "The real quote", "web_url": "https://teams.microsoft.com/l/message/real-thread/1",
        }],
    }


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "waiting.db")
    conn = db.get_connection()
    db.init_db(conn)
    conn.close()
    forbidden = Mock(side_effect=AssertionError("CLI/live forbidden"))
    monkeypatch.setattr(claude_runner, "run_copilot", forbidden)
    monkeypatch.setattr(app, "run_copilot", forbidden)
    monkeypatch.setattr(wire, "get_runtime", forbidden)
    def seed(**over):
        values = {
            "title": "Synthetic task", "description": "Synthetic description",
            "status": "waiting", "source_type": "chat",
            "key_people": '[{"email":"person@example.test","id":"target-id"}]',
            "source_locator": json.dumps({"kind": "teams_chat", "source": "captured",
                                         "conversation_id": "saved-thread"}),
            "user_notes": "", **over,
        }
        stored = {key: values.pop(key) for key in ("source_locator", "snoozed_until", "waiting_activity") if key in values}
        task = models.create_task(**values)
        for key, value in stored.items():
            edit(task["id"], key, value)
        edit(task["id"], "created_at", "2026-09-18T12:00:00Z")
        return raw(task["id"])
    return seed


def worker(monkeypatch, answers=None, **options):
    runtime = Mock()
    replies = iter(answers or [presence(), finding(evidence=[0])])
    runtime.execute_ask.side_effect = lambda *a, **k: {
        "answer": next(replies), "conversation_id": "TRANSIENT-MODEL-ID",
    }
    runtime.read_source.return_value = source()
    runtime.recover_chat.side_effect = wire.SourceUnreadableError("private")
    service = checks.WaitingChecks(runtime_provider=lambda: runtime, **options)
    monkeypatch.setattr(checks, "get_waiting_checks", lambda: service)
    return service, runtime


def activity(task):
    return json.loads(raw(task["id"])["waiting_activity"])


@pytest.mark.parametrize("status", ["active", "in_progress", "waiting", "snoozed", "completed", "deleted", "dismissed", "suggested"])
def test_target_checks_any_existing_status_without_status_change(store, monkeypatch, status):
    task = store(status=status)
    service, runtime = worker(monkeypatch)
    launch = service.launch(task["id"])
    assert launch["ok"]
    assert finish(service)["outcome"] == "succeeded"
    assert raw(task["id"])["status"] == status
    assert activity(task)["status"] == "activity_detected"
    assert runtime.execute_ask.call_count == 2


def test_global_selection_waiting_and_only_due_snoozed_ooo(store, monkeypatch):
    service, _ = worker(monkeypatch)
    now = datetime.now(timezone.utc)
    def ooo(age, state="ok"):
        return json.dumps({"status": "out_of_office", "check_state": state,
                           "checked_at": (now - timedelta(hours=age)).isoformat()})
    selected = [store()["id"], store(status="snoozed", waiting_activity=ooo(21))["id"],
                store(status="snoozed", waiting_activity='{"status":"out_of_office"}')["id"]]
    store(status="snoozed", waiting_activity=ooo(19))
    store(status="active", waiting_activity=ooo(21))
    store(status="snoozed", waiting_activity='{"status":"no_activity"}')
    store(status="snoozed", waiting_activity="malformed")
    assert {row["id"] for row in service._select(None)} == set(selected)


def test_snoozed_ooo_selection_uses_strict_twenty_hour_boundary(store, monkeypatch):
    now = datetime(2026, 9, 19, 22, 0, tzinfo=timezone.utc)
    cutoff = now - timedelta(hours=20)
    def at(stamp):
        return store(status="snoozed", waiting_activity=json.dumps({
            "status": "out_of_office", "checked_at": stamp,
        }))
    older = at((cutoff - timedelta(microseconds=1)).isoformat())
    at(cutoff.isoformat())
    at(cutoff.astimezone(timezone(timedelta(hours=2))).isoformat())
    at((cutoff + timedelta(microseconds=1)).isoformat())
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)
    monkeypatch.setattr(checks, "datetime", FrozenDatetime)
    service, runtime = worker(monkeypatch)
    assert [task["id"] for task in service._select(None)] == [older["id"]]
    assert not runtime.mock_calls


@pytest.mark.parametrize("people", [None, "[]", "bad", '[{"name":"Display only"}]'])
def test_unresolved_people_deterministic_no_query(store, monkeypatch, people):
    task = store(key_people=people)
    service, runtime = worker(monkeypatch)
    service.launch(task["id"])
    finish(service)
    assert activity(task)["status"] == "no_activity"
    assert activity(task)["summary"] == "No key people to check"
    assert activity(task).get("presence_verified_available") is not True
    assert not runtime.mock_calls


def test_ooo_first_wins_without_source_or_recovery(store, monkeypatch):
    task = store(user_notes="@WorkIQ return?")
    service, runtime = worker(monkeypatch, [presence(True, answers=[{"question_id": 0, "answer": "October"}])])
    service.launch(task["id"])
    finish(service)
    assert activity(task)["status"] == "out_of_office"
    assert activity(task)["return_date"] == "2026-10-01"
    assert activity(task)["source_scope"] == "person"
    assert activity(task).get("presence_verified_available") is not True
    assert raw(task["id"])["user_notes"].endswith("  → October")
    assert [call[0] for call in runtime.mock_calls] == ["execute_ask"]


def test_source_evidence_uses_local_utc_cursor_and_exact_server_values(store, monkeypatch):
    task = store(waiting_activity='{"status":"no_activity","checked_at":"2026-09-19T17:00:00Z"}')
    service, runtime = worker(monkeypatch, [presence(), finding("may_be_resolved", evidence=[0])])
    old = {**source()["items"][0], "source_item_id": "old", "occurred_at": "2026-09-19T19:00:00+02:00"}
    runtime.read_source.return_value = source(items=[old, source()["items"][0]])
    service.launch(task["id"])
    finish(service)
    result = activity(task)
    assert result["check_since"] == "2026-09-19T17:00:00Z"
    assert result["source_scope"] == "thread"
    assert result["conversation_id"] == "real-thread"
    assert result["evidence"] == [{"excerpt": "The real quote", "when": "2026-09-19T18:00:00Z",
                                   "where": "Teams", "url": source()["items"][0]["web_url"]}]
    payload = json.loads(runtime.execute_ask.call_args.args[0].split("\nSOURCE_DATA_JSON:\n")[1])
    assert [item["source_item_id"] for item in payload["items"]] == ["real-message"]
    assert raw(task["id"])["status"] == "waiting"
    assert "TRANSIENT-MODEL-ID" not in json.dumps(raw(task["id"]))
    runtime.read_source.assert_called_once()
    assert runtime.read_source.call_args.args[0] == source_locator.resolve(task["source_locator"], None)


@pytest.mark.parametrize("error", [wire.SourceUnreadableError, wire.SourcePartialError,
                                   wire.SourceForbiddenError, wire.SourceNotFoundError])
def test_unreadable_source_and_recovery_use_honest_all_channel_person_fallback(store, monkeypatch, error):
    task = store()
    quote = {"excerpt": "Actual reported reply", "when": "2026-09-19T18:00:00Z", "where": "Email", "url": None}
    service, runtime = worker(monkeypatch, [presence(), finding(evidence=[quote])])
    runtime.read_source.side_effect = error("private")
    service.launch(task["id"])
    finish(service)
    result = activity(task)
    assert result["source_scope"] == "person"
    assert result.get("conversation_id") is None
    assert result["evidence"] == [quote]
    assert "emails, Teams messages, and chats" in runtime.execute_ask.call_args.args[0]
    assert runtime.recover_chat.call_count == 1
    assert raw(task["id"])["source_locator"] == task["source_locator"]


def test_proven_recovery_records_distinct_provenance_without_rewriting_saved_id(store, monkeypatch):
    task = store()
    service, runtime = worker(monkeypatch)
    runtime.read_source.side_effect = wire.SourceNotFoundError("private")
    runtime.recover_chat.side_effect = None
    runtime.recover_chat.return_value = {**source(), "recovery_kind": "recent_chat_membership",
                                        "locator_source": "recovered"}
    service.launch(task["id"])
    finish(service)
    assert activity(task)["recovery_kind"] == "recent_chat_membership"
    assert activity(task)["conversation_id"] == "real-thread"
    assert raw(task["id"])["source_locator"] == task["source_locator"]


@pytest.mark.parametrize("bad", ["{}", "null", "```json\n{}\n```", finding(evidence=[True]),
    finding(evidence=[99]), finding(evidence=[0, 0]), finding(evidence=[{"url": "https://evil.test"}]),
    finding(conversation_id="forged"), finding(answers=[{"question_id": 99, "answer": "x"}]),
    finding(status="completed"), finding(return_date="2026-10-01")])
def test_invalid_business_result_preserves_prior_cursor_notes_and_snooze(store, monkeypatch, bad):
    prior = {"status": "out_of_office", "checked_at": "2026-09-18T14:00:00Z", "summary": "Earlier"}
    task = store(status="snoozed", snoozed_until="2026-10-01", waiting_activity=json.dumps(prior))
    service, _ = worker(monkeypatch, [presence(), bad])
    service.launch(task["id"])
    assert finish(service)["outcome"] == "failed"
    after = raw(task["id"])
    result = activity(task)
    assert result["check_state"] == "failed" and "status" not in result
    assert result["previous"]["summary"] == "Earlier"
    assert waiting_activity.next_check_since(result, task["created_at"]) == prior["checked_at"]
    assert after["status"] == "snoozed" and after["snoozed_until"] == task["snoozed_until"]
    assert after["user_notes"] == task["user_notes"]


@pytest.mark.parametrize("field,value", [
    ("status", "completed"), ("title", "edited"), ("description", "edited"), ("key_people", "[]"),
    ("source_type", "email"), ("source_id", "email::other@example.test::x"), ("source_url", "https://teams.microsoft.com"),
    ("source_locator", "{}"), ("user_notes", "edited"), ("waiting_activity", "{}"),
    ("snoozed_until", "2027-01-01"), ("created_at", "2026-09-19T01:00:00Z"),
])
@pytest.mark.parametrize("failed", [False, True])
def test_all_prompt_and_unsnooze_inputs_are_atomic_cas(store, monkeypatch, field, value, failed):
    task = store(status="snoozed", waiting_activity='{"status":"out_of_office"}')
    service, runtime = worker(monkeypatch)
    original = runtime.execute_ask.side_effect
    saved = {}
    def mutate(*args, **kw):
        edit(task["id"], field, value)
        saved.update(raw(task["id"]))
        if failed:
            raise wire.AuthRequiredError("private")
        return original(*args, **kw)
    runtime.execute_ask.side_effect = mutate
    service.launch(task["id"])
    assert finish(service)["outcome"] == "skipped"
    assert raw(task["id"]) == saved


def test_success_notes_and_ooo_unsnooze_commit_together(store, monkeypatch):
    task = store(status="snoozed", snoozed_until="2026-10-01", waiting_activity='{"status":"out_of_office"}',
                 user_notes="@WorkIQ first?\n  → old\n@WORKIQ second?")
    service, _ = worker(monkeypatch, [presence(), finding(evidence=[0], answers=[{"question_id": 2, "answer": "New"}])])
    service.launch(task["id"])
    finish(service)
    after = raw(task["id"])
    assert after["status"] == "waiting" and after["snoozed_until"] is None
    assert after["user_notes"] == task["user_notes"] + "\n  → New"
    assert activity(task)["check_state"] == "ok"
    assert activity(task)["presence_verified_available"] is True


def test_writer_rolls_back_all_three_outputs(store, monkeypatch):
    task = store(status="snoozed", waiting_activity='{"status":"out_of_office"}', user_notes="@WorkIQ q?")
    conn = db.get_connection()
    conn.execute("CREATE TRIGGER reject_notes BEFORE UPDATE OF user_notes ON tasks BEGIN SELECT RAISE(ABORT,'private'); END")
    conn.commit()
    conn.close()
    service, _ = worker(monkeypatch, [presence(), finding(evidence=[0], answers=[{"question_id": 0, "answer": "New"}])])
    service.launch(task["id"])
    assert finish(service)["outcome"] == "failed"
    assert raw(task["id"]) == task


@pytest.mark.parametrize("error", [wire.AuthRequiredError, wire.ConsentRequiredError, wire.EulaRequiredError,
                                   wire.InvalidResponseError, wire.CapabilityDeniedError, wire.SourceHTTPError])
def test_fatal_batch_stops_and_unprocessed_rows_unchanged(store, monkeypatch, error):
    tasks = [store(), store()]
    service, runtime = worker(monkeypatch)
    runtime.execute_ask.side_effect = error("private")
    service.launch()
    assert finish(service)["outcome"] == "failed"
    assert runtime.execute_ask.call_count == 1
    assert sum(raw(task["id"]) == task for task in tasks) == 1


def test_manual_two_day_pre_window_only_before_first_success(store, monkeypatch):
    task = store(source_type="manual", key_people="[]", source_locator=None)
    service, _ = worker(monkeypatch)
    service.launch(task["id"])
    finish(service)
    first = activity(task)
    assert first["check_since"] == "2026-09-16T12:00:00Z"
    service.launch(task["id"])
    finish(service)
    assert activity(task)["check_since"] == first["checked_at"]


def test_deadline_counts_presence_source_and_persistence_without_reset(store, monkeypatch):
    task = store()
    clock = [100.0]
    service, runtime = worker(monkeypatch, monotonic_clock=lambda: clock[0])
    original = runtime.execute_ask.side_effect
    def ask(*a, **kw):
        clock[0] += 100
        return original(*a, **kw)
    runtime.execute_ask.side_effect = ask
    def read(*a, **kw):
        clock[0] += 150
        return source()
    runtime.read_source.side_effect = read
    service.launch(task["id"])
    finish(service)
    assert [call.kwargs["timeout"] for call in runtime.execute_ask.call_args_list] == [420, 170]
    assert runtime.read_source.call_args.kwargs["timeout"] == 320
    assert activity(task)["check_state"] == "ok"


def test_periodic_uses_direct_global_and_four_hour_cadence(store, monkeypatch):
    service, runtime = worker(monkeypatch)
    store(key_people="[]")
    app._check_waiting()
    finish(service)
    assert app.WAITING_CHECK_INTERVAL_MS == 4 * 60 * 60 * 1000
    assert not runtime.mock_calls


def test_suggestion_invalid_response_is_shared_fatal_blocker(store, monkeypatch):
    tasks = [store(status="suggested"), store(status="suggested")]
    runtime = Mock()
    runtime.execute_ask.side_effect = wire.InvalidResponseError("private")
    service = checks.SuggestionChecks(runtime_provider=lambda: runtime)
    service.launch()
    finish(service)
    assert runtime.execute_ask.call_count == 1
    assert sum(raw(task["id"]) == task for task in tasks) == 1


def test_bad_manual_cursor_is_one_failed_item_not_batch_abort(store, monkeypatch):
    first = store(source_type="manual", key_people="[]")
    edit(first["id"], "created_at", "bad")
    second = store(key_people="[]")
    service, _ = worker(monkeypatch)
    service.launch()
    finish(service)
    assert activity(first)["check_state"] == "failed"
    assert activity(second)["check_state"] == "ok"


def test_persistence_expiry_rolls_back_activity_notes_and_unsnooze(store, monkeypatch):
    task = store(status="snoozed", waiting_activity='{"status":"out_of_office"}',
                 user_notes="@WorkIQ q?")
    clock = [0.0]
    original = models.get_connection
    def connection():
        conn = original()
        def expire():
            clock[0] = 421
            return 0
        conn.create_function("expire", 0, expire)
        return conn
    monkeypatch.setattr(models, "get_connection", connection)
    conn = connection()
    conn.execute("CREATE TRIGGER expire_write AFTER UPDATE OF waiting_activity ON tasks BEGIN SELECT expire(); END")
    conn.commit()
    conn.close()
    service, _ = worker(monkeypatch, [presence(), finding(evidence=[0], answers=[{"question_id": 0, "answer": "New"}])],
                        monotonic_clock=lambda: clock[0])
    service.launch(task["id"])
    assert finish(service)["outcome"] == "failed"
    assert raw(task["id"]) == task


@pytest.mark.parametrize("phase", ["presence", "source", "recovery", "classification"])
@pytest.mark.parametrize("error", [wire.AuthRequiredError, wire.InvalidResponseError, wire.SourceHTTPError])
def test_readiness_fatal_at_each_phase_never_falls_back_or_queries_next(store, monkeypatch, phase, error):
    tasks = [store(), store()]
    service, runtime = worker(monkeypatch)
    if phase == "presence":
        runtime.execute_ask.side_effect = error("private")
    elif phase in {"source", "recovery"}:
        runtime.read_source.side_effect = error("private") if phase == "source" else wire.SourceNotFoundError("private")
        runtime.recover_chat.side_effect = error("private")
    else:
        runtime.execute_ask.side_effect = [
            {"answer": presence(), "conversation_id": "private"}, error("private"),
        ]
    service.launch()
    assert finish(service)["outcome"] == "failed"
    assert raw(tasks[1]["id"]) == tasks[1]
    assert runtime.execute_ask.call_count == (2 if phase == "classification" else 1)
    if phase in {"presence", "source", "classification"}:
        runtime.recover_chat.assert_not_called()


@pytest.mark.parametrize("verdict", ["no_activity", "activity_detected", "may_be_resolved"])
def test_person_fallback_validates_all_four_business_fields_and_citations(store, monkeypatch, verdict):
    task = store(source_locator=None, source_url=None)
    evidence = [] if verdict == "no_activity" else [
        {"excerpt": "Bounded quote", "when": "2026-09-19T14:00:00-04:00", "where": "Teams",
         "url": "https://teams.microsoft.com/l/message/example/1"},
    ]
    service, runtime = worker(monkeypatch, [presence(), finding(verdict, evidence=evidence)])
    service.launch(task["id"])
    finish(service)
    assert activity(task)["status"] == verdict
    assert activity(task)["source_scope"] == "person"
    assert "conversation_id" not in activity(task)
    runtime.read_source.assert_not_called()
    runtime.recover_chat.assert_not_called()


@pytest.mark.parametrize("changes", [{"url": "javascript:alert(1)"}, {"url": "https://evil.test/a"},
                                     {"when": "yesterday"}, {"where": "fabricated"},
                                     {"excerpt": "a" * 513}, {"person": "invented"}])
def test_person_fallback_rejects_invalid_quotes_and_citations(store, monkeypatch, changes):
    task = store(source_locator=None)
    evidence = {"excerpt": "quote", "when": "2026-09-19T18:00:00Z", "where": "Email", "url": None, **changes}
    service, _ = worker(monkeypatch, [presence(), finding(evidence=[evidence])])
    service.launch(task["id"])
    finish(service)
    assert activity(task)["check_state"] == "failed"


def test_x500_display_only_source_is_not_resolution_authority(store, monkeypatch):
    task = store()
    service, runtime = worker(monkeypatch, [presence(), finding("may_be_resolved", evidence=[0])])
    data = source()
    data["items"][0]["sender"] = {"id": None, "display_name": "Person", "address": None, "address_kind": "exchange_dn"}
    runtime.read_source.return_value = data
    service.launch(task["id"])
    finish(service)
    assert activity(task)["status"] == "activity_detected"
    assert activity(task)["conversation_id"] == "real-thread"
    assert raw(task["id"])["key_people"] == task["key_people"]


def test_complete_empty_source_is_quiet_only_after_presence(store, monkeypatch):
    task = store()
    service, runtime = worker(monkeypatch, [presence()])
    runtime.read_source.return_value = source(items=[])
    service.launch(task["id"])
    finish(service)
    assert activity(task)["status"] == "no_activity"
    assert [call[0] for call in runtime.mock_calls] == ["execute_ask", "read_source"]


def test_malformed_presence_is_failure_not_a_source_or_person_search(store, monkeypatch):
    task = store()
    service, runtime = worker(monkeypatch, ["{}"])
    service.launch(task["id"])
    finish(service)
    assert activity(task)["check_state"] == "failed"
    assert [call[0] for call in runtime.mock_calls] == ["execute_ask"]


@pytest.mark.parametrize("prior_failed", [False, True])
def test_unknown_presence_preserves_ooo_cursor_notes_and_snooze_without_activity_reads(store, monkeypatch, prior_failed):
    previous = {
        "version": 2, "producer": "waiting-check", "check_state": "ok",
        "status": "out_of_office", "summary": "Earlier verified OOO",
        "checked_at": "2026-09-18T15:00:00Z", "return_date": "2026-10-01",
    }
    prior = {
        "version": 2, "producer": "waiting-check", "check_state": "failed",
        "check_since": "2026-09-18T15:00:00Z", "checked_at": "2026-09-19T15:00:00Z",
        "previous": previous,
    } if prior_failed else previous
    task = store(status="snoozed", snoozed_until="2026-10-01",
                 waiting_activity=json.dumps(prior), user_notes="@WorkIQ when are they back?")
    cursor = waiting_activity.next_check_since(waiting_activity.normalise(task["waiting_activity"]), task["created_at"])
    service, runtime = worker(monkeypatch, [presence(
        None, summary="Current presence cannot be verified.",
        answers=[{"question_id": 0, "answer": "An unverified answer"}],
    )])
    service.launch(task["id"])
    assert finish(service)["outcome"] == "failed"
    after = raw(task["id"])
    result = activity(task)
    assert result["check_state"] == "failed" and "status" not in result
    assert result["previous"]["status"] == "out_of_office"
    assert result["previous"]["summary"] == previous["summary"]
    assert result["previous"]["return_date"] == previous["return_date"]
    assert result["check_since"] == cursor
    assert waiting_activity.next_check_since(result, task["created_at"]) == cursor
    for field in task.keys() - {"waiting_activity", "updated_at"}:
        assert after[field] == task[field], field
    assert [call[0] for call in runtime.mock_calls] == ["execute_ask"]
    prompt = runtime.execute_ask.call_args.args[0]
    assert '"out_of_office":null' in prompt
    assert '"return_date":null' in prompt
    assert "true or false only when verified" in prompt


@pytest.mark.parametrize("source_read", [True, False])
def test_unknown_presence_without_prior_ooo_allows_valid_activity_with_visible_uncertainty(store, monkeypatch, source_read):
    prior = {"check_state": "ok", "status": "no_activity", "checked_at": "2026-09-19T17:00:00Z"}
    task = store(waiting_activity=json.dumps(prior), user_notes="@WorkIQ question?")
    if not source_read:
        edit(task["id"], "source_locator", None)
    evidence = [0] if source_read else [
        {"excerpt": "Actual quoted reply", "when": "2026-09-19T18:00:00Z", "where": "Email", "url": None},
    ]
    service, runtime = worker(monkeypatch, [
        presence(None), finding(evidence=evidence, answers=[{"question_id": 0, "answer": "Verified activity answer"}]),
    ])
    service.launch(task["id"])
    assert finish(service)["outcome"] == "succeeded"
    result = activity(task)
    assert result["check_state"] == "ok" and result["status"] == "activity_detected"
    assert result["presence_unverified"] is True
    assert result.get("presence_verified_available") is not True
    assert result["summary"].startswith("Presence unverified; out-of-office status is unknown.")
    assert "Synthetic finding" in result["summary"]
    public = models.get_task(task["id"])["waiting_signal"]["activity"]
    assert public["presence_unverified"] is True and public["summary"] == result["summary"]
    assert raw(task["id"])["status"] == "waiting"
    assert raw(task["id"])["user_notes"] == task["user_notes"] + "\n  → Verified activity answer"
    assert raw(task["id"])["cowork_revision"] == task["cowork_revision"] + 1
    assert result["check_since"] == prior["checked_at"]
    assert waiting_activity.next_check_since(result, task["created_at"]) == result["checked_at"]
    assert runtime.execute_ask.call_count == 2
    assert runtime.read_source.call_count == int(source_read)
    runtime.recover_chat.assert_not_called()


def test_unknown_presence_failed_activity_preserves_retry_cursor_and_notes(store, monkeypatch):
    prior = {"check_state": "ok", "status": "no_activity", "summary": "Earlier activity finding",
             "checked_at": "2026-09-19T17:00:00Z"}
    task = store(waiting_activity=json.dumps(prior), user_notes="@WorkIQ question?")
    service, runtime = worker(monkeypatch, [presence(None), "{}"])
    service.launch(task["id"])
    assert finish(service)["outcome"] == "failed"
    result = activity(task)
    assert result["check_state"] == "failed" and "status" not in result
    assert result["check_since"] == prior["checked_at"]
    assert waiting_activity.next_check_since(result, task["created_at"]) == prior["checked_at"]
    assert result["previous"]["summary"] == prior["summary"]
    assert result.get("presence_verified_available") is not True
    for field in task.keys() - {"waiting_activity", "updated_at"}:
        assert raw(task["id"])[field] == task[field]
    assert runtime.execute_ask.call_count == 2
    runtime.read_source.assert_called_once()


@pytest.mark.parametrize("prior_failed", [False, True])
def test_prior_ooo_without_authoritative_person_fails_without_unsnoozing_or_query(store, monkeypatch, prior_failed):
    previous = {"status": "out_of_office", "summary": "Earlier verified OOO",
                "checked_at": "2026-09-18T15:00:00Z", "return_date": "2026-10-01"}
    prior = {"check_state": "failed", "check_since": previous["checked_at"],
             "previous": previous} if prior_failed else previous
    task = store(status="snoozed", snoozed_until="2026-10-01", waiting_activity=json.dumps(prior),
                 key_people="[]", user_notes="@WorkIQ question?")
    service, runtime = worker(monkeypatch)
    service.launch(task["id"])
    assert finish(service)["outcome"] == "failed"
    result = activity(task)
    assert result["check_state"] == "failed" and "status" not in result
    assert result["previous"]["status"] == "out_of_office"
    assert result["check_since"] == previous["checked_at"]
    assert result.get("presence_verified_available") is not True
    for field in task.keys() - {"waiting_activity", "updated_at"}:
        assert raw(task["id"])[field] == task[field]
    assert not runtime.mock_calls


@pytest.mark.parametrize("proof", ["missing", None, False, 1, "true", True])
def test_waiting_writer_unsnoozes_only_with_explicit_true_availability_proof(store, proof):
    task = store(status="snoozed", snoozed_until="2026-10-01",
                 waiting_activity='{"status":"out_of_office"}')
    result = {"check_state": "ok", "status": "no_activity"}
    if proof != "missing":
        result["presence_verified_available"] = proof
    assert models.write_waiting_check(task, result, task["user_notes"])
    after = raw(task["id"])
    assert after["status"] == ("waiting" if proof is True else "snoozed")
    assert after["snoozed_until"] == (None if proof is True else "2026-10-01")


@pytest.mark.parametrize("error", [wire.AuthRequiredError, wire.SourceHTTPError])
def test_unknown_presence_does_not_hide_source_blockers_or_continue_batch(store, monkeypatch, error):
    first, second = store(), store()
    service, runtime = worker(monkeypatch, [presence(None)])
    runtime.read_source.side_effect = error("PRIVATE")
    service.launch()
    assert finish(service)["outcome"] == "failed"
    assert activity(first)["check_state"] == "failed"
    assert raw(second["id"]) == second
    assert runtime.execute_ask.call_count == 1
    runtime.read_source.assert_called_once()
    runtime.recover_chat.assert_not_called()


def test_model_activity_cannot_mint_verified_availability_proof(store, monkeypatch):
    task = store()
    service, runtime = worker(monkeypatch, [
        presence(None), finding(evidence=[0], presence_verified_available=True),
    ])
    service.launch(task["id"])
    assert finish(service)["outcome"] == "failed"
    result = activity(task)
    assert result["check_state"] == "failed"
    assert result.get("presence_verified_available") is not True
    assert raw(task["id"])["status"] == task["status"]
    assert runtime.execute_ask.call_count == 2


@pytest.mark.parametrize("value", [0, 1, "false", {}, []])
def test_presence_tristate_does_not_coerce_other_values(store, monkeypatch, value):
    task = store()
    service, runtime = worker(monkeypatch, [presence(value)])
    service.launch(task["id"])
    assert finish(service)["outcome"] == "failed"
    assert activity(task).get("presence_verified_available") is not True
    assert [call[0] for call in runtime.mock_calls] == ["execute_ask"]


def test_empty_global_and_periodic_never_call_provider(store, monkeypatch):
    service, runtime = worker(monkeypatch)
    result = service.launch(skip_empty=True)
    assert result["ok"] and "run_id" not in result
    assert service._thread is None
    assert service.launch()["ok"]
    assert finish(service)["outcome"] == "succeeded"
    assert not runtime.mock_calls


def test_waiting_and_suggestion_have_independent_slots_and_thread_off_caller(store, monkeypatch):
    task = store()
    entered, release = threading.Event(), threading.Event()
    service, runtime = worker(monkeypatch)
    caller = threading.get_ident()
    def slow(*a, **kw):
        assert threading.get_ident() != caller
        entered.set()
        assert release.wait(5)
        return {"answer": presence(True), "conversation_id": "private"}
    runtime.execute_ask.side_effect = slow
    try:
        service.launch(task["id"])
        assert entered.wait(5)
        assert service.status()["waiting-check"]
        assert service.launch(task["id"])["ok"] is False
        suggested = store(status="suggested", key_people="[]")
        other = checks.SuggestionChecks(runtime_provider=lambda: runtime)
        assert other.launch(suggested["id"])["ok"]
        assert finish(other)["outcome"] == "succeeded"
    finally:
        release.set()
        finish(service)


def test_deletion_during_presence_never_recreates_task(store, monkeypatch):
    task = store()
    service, runtime = worker(monkeypatch)
    def deleted(*a, **k):
        conn = db.get_connection()
        conn.execute("DELETE FROM tasks WHERE id=?", (task["id"],))
        conn.commit()
        conn.close()
        return {"answer": presence(True), "conversation_id": "private"}
    runtime.execute_ask.side_effect = deleted
    service.launch(task["id"])
    assert finish(service)["outcome"] == "skipped"
    assert raw(task["id"]) is None


def test_global_deadline_exhaustion_leaves_rest_unchanged(store, monkeypatch):
    tasks = [store(), store()]
    clock = [0.0]
    service, runtime = worker(monkeypatch, monotonic_clock=lambda: clock[0])
    def late(*args, **kwargs):
        assert kwargs["timeout"] == 300
        clock[0] = 301
        return {"answer": presence(True), "conversation_id": "private"}
    runtime.execute_ask.side_effect = late
    service.launch()
    assert finish(service)["outcome"] == "failed"
    assert raw(tasks[1]["id"]) == tasks[1]
    assert runtime.execute_ask.call_count == 1


def test_same_second_waiting_runs_are_distinct_microsecond_results(store, monkeypatch):
    task = store(key_people="[]")
    class FastDatetime:
        tick = 0
        @classmethod
        def now(cls, tz):
            cls.tick += 1
            return datetime(2026, 9, 19, 20, 0, 0, cls.tick, tzinfo=timezone.utc)
    monkeypatch.setattr(checks, "datetime", FastDatetime)
    service, _ = worker(monkeypatch)
    service.launch(task["id"])
    first_run = finish(service)
    first = activity(task)
    service.launch(task["id"])
    second_run = finish(service)
    second = activity(task)
    assert first_run["run_id"] != second_run["run_id"]
    assert first_run["started_at"] < first["checked_at"] < first_run["finished_at"]
    assert second_run["started_at"] < second["checked_at"] < second_run["finished_at"]
    assert first["checked_at"] < second["checked_at"]


def test_live_shaped_two_character_email_id_mismatch_never_becomes_an_alias(store, monkeypatch, tmp_path):
    from tests.test_workiq_runtime_peer import source_runtime, source_calls, SOURCE_SELECT
    saved, returned = "AAMkStoredAA=", "AAMkStoredBB="
    task = store(source_type="email", source_locator=json.dumps({
        "kind": "email", "source": "captured", "message_id": saved,
    }))
    runtime, trace = source_runtime(tmp_path, [
        {"id": returned, "conversationId": "MUST-NOT-READ"}, {"value": []},
    ])
    runtime.execute_ask = Mock(side_effect=[
        {"answer": presence(), "conversation_id": "MODEL"},
        {"answer": finding("no_activity"), "conversation_id": "MODEL"},
    ])
    service = checks.WaitingChecks(runtime_provider=lambda: runtime)
    try:
        service.launch(task["id"])
        assert finish(service)["outcome"] == "succeeded"
        result = activity(task)
        assert result["source_scope"] == "person" and "conversation_id" not in result
        assert raw(task["id"])["source_locator"] == task["source_locator"]
        assert source_calls(trace)[0]["arguments"]["entityUrls"] == [f"/me/messages/AAMkStoredAA%3D?$select={SOURCE_SELECT}"]
        assert len(source_calls(trace)) == 2
        assert "MUST-NOT-READ" not in json.dumps(source_calls(trace))
    finally:
        runtime.shutdown()


@pytest.mark.parametrize("source_type,source_id,people,expected", [
    ("email", "email::origin@example.test::subject", '[{"email":"person@example.test"}]', "origin@example.test"),
    ("manual", "email::ignore@example.test::subject", '[{"upn":"person@example.test"}]', "person@example.test"),
    ("chat", "chat::Display Name::subject", '[{"email":"person@example.test"}]', "person@example.test"),
    ("email", "email::/o=Org/ou=Unit/cn=Recipients/cn=user@example.test::subject", "[]", None),
    ("manual", None, '[{"email":"/o=Org/ou=Unit/cn=Recipients/cn=user@example.test"}]', None),
])
def test_only_exact_smtp_or_upn_establishes_authoritative_waiting_person(store, monkeypatch, source_type, source_id, people, expected):
    task = store(source_type=source_type, source_id=source_id, key_people=people)
    service, runtime = worker(monkeypatch, [presence(True)])
    service.launch(task["id"])
    finish(service)
    if expected:
        payload = json.loads(runtime.execute_ask.call_args.args[0].split("\nSOURCE_DATA_JSON:\n")[1])
        assert payload["person"] == expected
    else:
        assert not runtime.mock_calls
        assert activity(task)["status"] == "no_activity"


def test_repeated_failures_keep_last_success_without_growing_chain(store, monkeypatch, caplog):
    prior = {"version": 2, "producer": "waiting-check", "check_state": "ok", "status": "no_activity",
             "summary": "Earlier successful finding", "checked_at": "2026-09-18T13:00:00Z"}
    task = store(waiting_activity=json.dumps(prior))
    service, runtime = worker(monkeypatch)
    runtime.execute_ask.side_effect = ValueError("PRIVATE PROVIDER PAYLOAD")
    for _ in range(2):
        service.launch(task["id"])
        finish(service)
    result = activity(task)
    assert result["previous"]["check_state"] == "ok"
    assert result["previous"]["summary"] == prior["summary"]
    assert result["previous"].get("previous") is None
    assert result["check_since"] == prior["checked_at"]
    assert "PRIVATE" not in json.dumps(service.completion()) + json.dumps(result) + caplog.text


@pytest.mark.parametrize("changes", [
    {"version": True}, {"summary": ""}, {"summary": "x" * 2001}, {"answers": None},
    {"answers": [{"question_id": 0, "answer": "x\ninjected line"}]},
    {"answers": [{"question_id": 0, "answer": "x" * 1001}]},
    {"answers": [{"question_id": 0, "answer": "first"}, {"question_id": 0, "answer": "duplicate"}]},
    {"evidence": [0, 1, 2, 3]}, {"evidence": [0], "answers": []},
])
def test_partial_or_invalid_notes_response_never_writes_any_answers(store, monkeypatch, changes):
    task = store(user_notes="@WorkIQ question?")
    service, runtime = worker(monkeypatch)
    runtime.execute_ask.side_effect = iter([
        {"answer": presence(), "conversation_id": "private"},
        {"answer": finding(**{"evidence": [0], "answers": [{"question_id": 0, "answer": "Good"}], **changes}),
         "conversation_id": "private"},
    ])
    service.launch(task["id"])
    finish(service)
    assert activity(task)["check_state"] == "failed"
    assert raw(task["id"])["user_notes"] == task["user_notes"]


def test_global_ordinary_failure_continues_next_task(store, monkeypatch):
    first, second = store(), store()
    service, runtime = worker(monkeypatch, ["{}", presence(True)])
    service.launch()
    assert finish(service)["outcome"] == "failed"
    assert activity(first)["check_state"] == "failed"
    assert activity(second)["status"] == "out_of_office"
    assert runtime.execute_ask.call_count == 2


def test_recovery_has_only_explicit_captured_topic_not_task_prose(store, monkeypatch):
    for located, expected in [
        ({"kind": "teams_chat", "source": "captured", "conversation_id": "old", "topic": "Exact captured topic"}, "Exact captured topic"),
        ({"kind": "teams_chat", "source": "captured", "conversation_id": "old"}, None),
        ({"kind": "teams_chat", "source": "derived_from_url", "conversation_id": "old", "topic": "Not captured"}, None),
    ]:
        task = store(title="Must not guess a group topic from this title", source_locator=json.dumps(located))
        service, runtime = worker(monkeypatch, [presence(), finding("no_activity")])
        runtime.read_source.side_effect = wire.SourceNotFoundError("private")
        service.launch(task["id"])
        finish(service)
        assert runtime.recover_chat.call_args.kwargs["topic"] == expected
        assert activity(task)["source_scope"] == "person"


def test_recovery_provenance_survives_failed_recheck_and_public_reader(store, monkeypatch):
    task = store(waiting_activity=json.dumps({
        "version": 2, "producer": "waiting-check", "status": "activity_detected", "check_state": "ok",
        "summary": "Recovered source finding", "checked_at": "2026-09-19T18:00:00Z",
        "source_scope": "thread", "conversation_id": "real-recovered-thread",
        "recovery_kind": "recent_chat_membership",
    }))
    assert models.get_task(task["id"])["waiting_signal"]["activity"]["recovery_kind"] == "recent_chat_membership"
    service, runtime = worker(monkeypatch)
    runtime.execute_ask.side_effect = ValueError("private")
    service.launch(task["id"])
    finish(service)
    assert activity(task)["previous"]["recovery_kind"] == "recent_chat_membership"
    assert activity(task)["previous"]["conversation_id"] == "real-recovered-thread"


def _ready_delivery(task_id):
    preview = models.create_task_action(
        task_id, conversation_id="synthetic-conversation",
        destination_ref="synthetic-target", destination_display="Synthetic person",
        delivery_channel="teams", destination_confirmed_at="2026-09-19T18:00:00Z",
    )
    preview = models.update_task_action(
        preview["id"], frozenset({"state", "draft"}), state="ready", draft="Synthetic reviewed draft",
    )
    approved = {
        "parent_action_id": preview["id"], "draft": preview["draft"],
        "destination_ref": preview["destination_ref"], "destination_display": preview["destination_display"],
        "delivery_channel": preview["delivery_channel"], "destination_confirmed_at": preview["destination_confirmed_at"],
    }
    return preview, approved


@pytest.mark.parametrize("family", ["suggestion", "waiting"])
@pytest.mark.parametrize("state", ["executing", "execute_unconfirmed"])
def test_check_note_writers_guard_old_revision_delivery_and_rollback_every_output(store, family, state):
    task = store(status="suggested" if family == "suggestion" else "snoozed",
                 waiting_activity='{"status":"out_of_office"}', snoozed_until="2026-10-01",
                 user_notes="@WorkIQ question?")
    older, _ = _ready_delivery(task["id"])
    edit(task["id"], "cowork_revision", 3)
    _ready_delivery(task["id"])
    conn = db.get_connection()
    conn.execute("UPDATE task_actions SET state=? WHERE id=?", (state, older["id"]))
    conn.commit()
    actions_before = [dict(row) for row in conn.execute("SELECT * FROM task_actions ORDER BY id")]
    conn.close()
    before = raw(task["id"])
    writer = models.write_suggestion_check if family == "suggestion" else models.write_waiting_check
    output = {"check_state": "ok", "status": "still_pending" if family == "suggestion" else "no_activity"}
    if family == "waiting":
        output["presence_verified_available"] = True
    with pytest.raises(ValueError, match=models.DELIVERY_CONFLICT_MESSAGE):
        writer(before, output, task["user_notes"] + "\n  → New answer")
    assert raw(task["id"]) == before
    conn = db.get_connection()
    assert [dict(row) for row in conn.execute("SELECT * FROM task_actions ORDER BY id")] == actions_before
    conn.close()


@pytest.mark.parametrize("family", ["suggestion", "waiting"])
def test_successful_note_write_bumps_once_and_old_preview_is_unexecutable(store, family):
    task = store(status="suggested" if family == "suggestion" else "waiting", user_notes="@WorkIQ question?")
    preview, approved = _ready_delivery(task["id"])
    before = raw(task["id"])
    writer = models.write_suggestion_check if family == "suggestion" else models.write_waiting_check
    output = {"check_state": "ok", "status": "still_pending" if family == "suggestion" else "no_activity"}
    notes = task["user_notes"] + "\n  → New answer"
    assert writer(before, output, notes)
    after = raw(task["id"])
    assert after["cowork_revision"] == before["cowork_revision"] + 1
    assert models.create_execution_action(preview["id"], approved) is None
    # Retrying an already answered snapshot is activity-only, not a second revision.
    assert writer(after, output, notes)
    assert raw(task["id"])["cowork_revision"] == after["cowork_revision"]


@pytest.mark.parametrize("family", ["suggestion", "waiting"])
@pytest.mark.parametrize("state", ["ready", "executing", "execute_unconfirmed"])
def test_activity_only_check_preserves_revision_and_delivery_state(store, family, state):
    task = store(status="suggested" if family == "suggestion" else "waiting")
    preview, approved = _ready_delivery(task["id"])
    conn = db.get_connection()
    conn.execute("UPDATE task_actions SET state=? WHERE id=?", (state, preview["id"]))
    conn.commit()
    conn.close()
    before = raw(task["id"])
    writer = models.write_suggestion_check if family == "suggestion" else models.write_waiting_check
    assert writer(before, {"check_state": "ok", "status": "no_activity"}, before["user_notes"])
    assert raw(task["id"])["cowork_revision"] == before["cowork_revision"]
    if state == "ready":
        assert models.create_execution_action(preview["id"], approved) is not None
    else:
        conn = db.get_connection()
        assert conn.execute("SELECT state FROM task_actions WHERE id=?", (preview["id"],)).fetchone()[0] == state
        conn.close()


@pytest.mark.parametrize("family", ["suggestion", "waiting"])
def test_note_write_cas_miss_is_skipped_before_delivery_guard(store, family):
    task = store(status="suggested" if family == "suggestion" else "waiting", user_notes="@WorkIQ question?")
    preview, _ = _ready_delivery(task["id"])
    before = raw(task["id"])
    edit(task["id"], "title", "User corrected title")
    conn = db.get_connection()
    conn.execute("UPDATE task_actions SET state='execute_unconfirmed' WHERE id=?", (preview["id"],))
    conn.commit()
    conn.close()
    after_edit = raw(task["id"])
    writer = models.write_suggestion_check if family == "suggestion" else models.write_waiting_check
    assert writer(before, {"check_state": "ok", "status": "no_activity"}, "@WorkIQ question?\n  → New") is False
    assert raw(task["id"]) == after_edit


@pytest.mark.parametrize("family", ["suggestion", "waiting"])
def test_workflow_surfaces_only_safe_delivery_conflict_and_preserves_snapshot(store, monkeypatch, family, caplog):
    from tests.test_suggestion_check_workflow import service as suggestion_service, answer
    task = store(status="suggested" if family == "suggestion" else "snoozed",
                 waiting_activity='{"status":"out_of_office"}', snoozed_until="2026-10-01",
                 user_notes="@WorkIQ question?")
    preview, _ = _ready_delivery(task["id"])
    conn = db.get_connection()
    conn.execute("UPDATE task_actions SET state='execute_unconfirmed' WHERE id=?", (preview["id"],))
    conn.commit()
    conn.close()
    before = raw(task["id"])
    answers = [{"question_id": 0, "answer": "PRIVATE NEW ANSWER"}]
    if family == "suggestion":
        service, _ = suggestion_service(monkeypatch, lambda *a, **k: {"answer": answer(answers=answers), "conversation_id": "PRIVATE"})
    else:
        service, _ = worker(monkeypatch, [presence(), finding(evidence=[0], answers=answers)])
    service.launch(task["id"])
    completion = finish(service)
    assert completion["outcome"] == "failed"
    assert completion["error"] == models.DELIVERY_CONFLICT_MESSAGE
    assert raw(task["id"]) == before
    assert "PRIVATE" not in json.dumps(completion) + caplog.text


@pytest.mark.parametrize("family", ["suggestion", "waiting"])
def test_note_revision_transaction_prevents_racing_old_preview_execution(store, monkeypatch, family):
    task = store(status="suggested" if family == "suggestion" else "waiting", user_notes="@WorkIQ question?")
    preview, approved = _ready_delivery(task["id"])
    entered, release, claiming, claimed = threading.Event(), threading.Event(), threading.Event(), threading.Event()
    original = models._raise_if_unresolved_delivery
    def guarded(conn, task_id, **kw):
        entered.set()
        assert release.wait(5)
        return original(conn, task_id, **kw)
    monkeypatch.setattr(models, "_raise_if_unresolved_delivery", guarded)
    results = {}
    writer = models.write_suggestion_check if family == "suggestion" else models.write_waiting_check
    def write():
        results["write"] = writer(task, {"check_state": "ok", "status": "no_activity"}, "@WorkIQ question?\n  → New")
    def execute():
        claiming.set()
        results["execution"] = models.create_execution_action(preview["id"], approved)
        claimed.set()
    write_thread, execute_thread = threading.Thread(target=write), threading.Thread(target=execute)
    try:
        write_thread.start()
        assert entered.wait(2), "writer never applied the unresolved-delivery guard"
        execute_thread.start()
        assert claiming.wait(2)
        assert not claimed.wait(.05), "execution bypassed the writer's transaction lock"
    finally:
        release.set()
        write_thread.join(5)
        if execute_thread.ident is not None:
            execute_thread.join(5)
    assert results == {"write": True, "execution": None}
    assert raw(task["id"])["cowork_revision"] == task["cowork_revision"] + 1
