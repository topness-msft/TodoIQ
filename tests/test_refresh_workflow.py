"""Stage 3 synthetic providers, real parser and isolated transactional SQLite."""

import importlib
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

from src import db, models
from src.services import parsing, workiq_runtime, claude_runner
from tests.test_refresh_models import candidate, envelope
from tests.test_parse_workflow import PROFILE, SELF, CAS_EDITS, raw, edit, identity_counts, delivery
from tests.test_parsing_models import result as parse_result


class Runtime:
    def __init__(self, batches=None):
        self.batches = batches or {}
        self.calls = []
        self.before = lambda operation, payload: None
        self.profile = dict(PROFILE)
        self.coach_mutation = lambda value: value
        self.parse_answer = None

    def execute_ask(self, prompt, *, timeout):
        assert timeout > 0
        payload = json.loads(prompt.split("\nSOURCE_DATA_JSON:\n")[1])
        self.calls.append(("ask", payload, timeout))
        self.before("ask", payload)
        if "batch" in payload:
            answer = self.batches.get(payload["batch"], envelope(payload["batch"]))
        elif payload.get("operation") == "semantic":
            answer = {"version": 1, "results": [
                {"ordinal": item["ordinal"], "task_id": None} for item in payload["candidates"]
            ]}
        elif payload.get("operation") == "coaching":
            answer = self.coach_mutation({"version": 1, "results": [
                {"ordinal": item["ordinal"], "result": {
                    "version": 1, "mode": "coaching_only",
                    "coaching_text": "Ask Taylor to review " + item["task"]["title"] + ".",
                    "answers": [],
                }} for item in payload["tasks"]
            ]})
        elif prompt.startswith(parsing.HINT_INSTRUCTIONS):
            answer = {"version": 1, "hints": []}
        else:
            answer = self.parse_answer or parse_result(payload["mode"])
        return {"answer": answer if isinstance(answer, str) else json.dumps(answer),
                "conversation_id": "SECRET-CONVERSATION"}

    def read_self_profile(self, *, timeout):
        self.calls.append(("self", None, timeout))
        return dict(SELF)

    def read_directory_user_by_email(self, email, *, timeout):
        self.calls.append(("email", email, timeout))
        self.before("email", email)
        if isinstance(self.profile, Exception):
            raise self.profile
        return dict(self.profile)

    def read_directory_user_by_aad(self, aad, *, timeout):
        self.calls.append(("aad", aad, timeout))
        self.before("aad", aad)
        return dict(self.profile)

    def find_directory_users_by_exact_name(self, name, *, timeout):
        self.calls.append(("name", name, timeout))
        return [dict(self.profile)]


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "refresh.db")
    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.delenv("RIVETER_DEMO_MODE", raising=False)
    conn = db.get_connection()
    db.init_db(conn)
    conn.close()
    forbidden = Mock(side_effect=AssertionError("Live/CLI forbidden"))
    monkeypatch.setattr(workiq_runtime, "get_runtime", forbidden)
    monkeypatch.setattr(parsing, "get_runtime", forbidden)
    monkeypatch.setattr(claude_runner, "run_copilot", forbidden)
    monkeypatch.setattr(claude_runner.subprocess, "Popen", forbidden)
    monkeypatch.setattr(parsing, "_completion", None)
    def seed(**changes):
        return models.create_task(**{"title": "User chosen title", "key_people": "[]", **changes})
    return seed


def tables():
    conn = db.get_connection()
    try:
        return {table: [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
                for table in ("tasks", "person", "person_alias", "task_person", "task_context", "task_actions", "sync_log")}
    finally:
        conn.close()


def worker(monkeypatch, runtime=None, **options):
    refresh = importlib.import_module("src.services.refresh")
    monkeypatch.setattr(refresh, "_completion", None)
    monkeypatch.setattr(db, "DB_DIR", Path(db.DB_PATH).parent)
    runtime = runtime or Runtime()
    parser = parsing.ParseService(runtime_provider=lambda: runtime, monotonic=options.get("monotonic"))
    monkeypatch.setattr(parsing, "_service", parser)
    service = refresh.RefreshService(runtime_provider=lambda: runtime, **options)
    monkeypatch.setattr(refresh, "_service", service)
    return service, parser, runtime


def scans(*items):
    return Runtime({"direct": envelope(candidates=list(items))})


@pytest.mark.parametrize("batch", ["direct", "awaiting", "flagged"])
@pytest.mark.parametrize("failure", ["invalid", "provider"])
def test_all_three_scan_barrier_has_zero_writes(store, monkeypatch, batch, failure):
    service, parser, runtime = worker(monkeypatch, scans(candidate()))
    if failure == "invalid":
        runtime.batches[batch] = '{"version":1}'
    else:
        def fail(operation, payload):
            if operation == "ask" and payload.get("batch") == batch:
                raise workiq_runtime.AuthRequiredError("PRIVATE")
        runtime.before = fail
    before = tables()
    outcome = service.run()
    assert outcome["exit_code"] != 0 and outcome["state"] != "succeeded"
    assert tables() == before and parser.completion() is None
    assert "PRIVATE" not in json.dumps(outcome)


def test_last_candidate_contradictory_proof_invalidates_entire_preflight(store, monkeypatch):
    second = candidate(root_topic="Other topic", people_hints=[
        {"name": "Other Example", "email": "other@example.test", "upn": None, "aad_object_id": None}])
    service, parser, runtime = worker(monkeypatch, scans(candidate(), second))
    before = tables()
    assert service.run()["exit_code"] != 0
    assert tables() == before and parser.completion() is None
    assert len([c for c in runtime.calls if c[0] == "ask" and "batch" in c[1]]) == 3


@pytest.mark.parametrize("unresolved", ["missing", "ambiguous", "guest", "primary_self"])
def test_unresolved_primary_never_links_or_fuzzy_merges(store, monkeypatch, unresolved):
    runtime = scans(candidate())
    if unresolved == "missing":
        runtime.profile = workiq_runtime.DirectoryNotFoundError("not found")
    elif unresolved == "ambiguous":
        runtime.profile = workiq_runtime.DirectoryAmbiguousError("ambiguous")
    elif unresolved == "guest":
        runtime.profile["user_type"] = "Guest"
    else:
        runtime.batches = {"direct": envelope(candidates=[candidate(people_hints=[
            {"name": "Self Example", "email": SELF["email"], "upn": None, "aad_object_id": None}])])}
        runtime.profile = dict(SELF)
    service, _, _ = worker(monkeypatch, runtime)
    assert service.run()["state"] == "succeeded"
    task = tables()["tasks"][0]
    assert task["source_id"].startswith("proposal::")
    assert identity_counts() == (0, 0, 0, 0)
    assert service.run()["created"] == 0
    assert len(tables()["tasks"]) == 1


@pytest.mark.parametrize("status", list(models.VALID_TRANSITIONS))
def test_exact_match_status_and_field_authority_matrix(store, monkeypatch, status):
    task = store(status=status, source_type="chat",
                 source_id="chat::taylor@example.test::release checklist",
                 source_snippet="[M365 2026-09-18T12:00:00Z] Older deadline.",
                 coaching_text="User coaching", skill_output="Keep draft", user_notes="Keep notes",
                 due_date="2026-10-01", priority=4)
    before = raw(task["id"])
    links = tables()["task_person"]
    service, _, _ = worker(monkeypatch, scans(candidate(base_priority=1)))
    assert service.run()["state"] == "succeeded"
    after = raw(task["id"])
    allowed = {"source_snippet", "updated_at"} if status in {"active", "in_progress", "completed", "suggested"} else set()
    if status == "suggested":
        allowed.add("priority")
    assert {k: v for k, v in before.items() if k not in allowed} == {
        k: v for k, v in after.items() if k not in allowed}
    assert after["priority"] == (1 if status == "suggested" else 4)
    assert len(tables()["tasks"]) == 1 and not tables()["task_context"]
    assert tables()["task_person"] == links


@pytest.mark.parametrize("snippet,source_date,changed", [
    ("Legacy without timestamp", None, False),
    ("Legacy with known source date", "2026-09-18T12:00:00Z", True),
    ("[M365 2026-09-20T12:00:00Z] Future", None, False),
    ("[M365 2026-09-18T12:00:00Z] Review the checklist by Friday.", None, False),
])
def test_freshness_never_uses_user_updated_at(store, monkeypatch, snippet, source_date, changed):
    task = store(source_id="chat::taylor@example.test::release checklist", source_type="chat",
                 source_snippet=snippet, source_date=source_date)
    edit(task["id"], updated_at="2099-01-01T00:00:00Z")
    service, _, _ = worker(monkeypatch, scans(candidate()))
    assert service.run()["state"] == "succeeded"
    assert (raw(task["id"])["source_snippet"] != snippet) == changed
    assert raw(task["id"])["source_date"] == source_date


def test_new_suggestions_use_only_verified_people_and_no_context_or_skill(store, monkeypatch):
    service, parser, runtime = worker(monkeypatch, scans(candidate()))
    outcome = service.run()
    assert outcome["created"] == 1 and outcome["state"] == "succeeded"
    task = tables()["tasks"][0]
    assert task["status"] == "suggested" and task["parse_status"] == "parsed"
    assert task["source_date"] is None and task["skill_output"] is None
    assert task["source_id"] == "chat::taylor@example.test::release checklist"
    assert json.loads(task["key_people"])[0]["email"] == PROFILE["email"]
    assert {r["role"] for r in tables()["task_person"]} == {"sender", "key_people"}
    assert not tables()["task_context"]
    assert parser.completion()["selected"] == 0
    marker = models.get_last_sync("full_scan")
    assert json.loads(marker["result_summary"])["run_id"] == outcome["run_id"]
    assert outcome["started_at"] <= marker["synced_at"] <= outcome["finished_at"]
    assert "SECRET-CONVERSATION" not in json.dumps(tables()) + json.dumps(outcome)


REFRESH_CAS_EDITS = {
    **CAS_EDITS, "source_type": "email", "key_people": '[{"name":"Selected"}]',
    "parse_status": "queued", "parse_intent": "full", "error_message": "User retry reason",
}


@pytest.mark.parametrize("mutate", list(REFRESH_CAS_EDITS))
def test_full_row_cas_rejects_user_edit_without_duplicate(store, monkeypatch, mutate):
    task = store(source_id="chat::taylor@example.test::release checklist", source_type="chat",
                 source_snippet="[M365 2026-09-18T12:00:00Z] Old")
    service, _, _ = worker(monkeypatch, scans(candidate()))
    original = models.write_refresh_candidate
    edited = []
    def race(snapshot, proposal, profiles, *, check_deadline):
        edit(task["id"], **{mutate: REFRESH_CAS_EDITS[mutate]})
        edited.append(raw(task["id"]))
        return original(snapshot, proposal, profiles, check_deadline=check_deadline)
    monkeypatch.setattr(models, "write_refresh_candidate", race)
    assert service.run()["state"] != "succeeded"
    assert raw(task["id"]) == edited[0] and models.get_last_sync("full_scan") is None
    assert len(tables()["tasks"]) == 1


@pytest.mark.parametrize("parse_state", ["busy", "partial", "blocked", "failed"])
def test_required_parse_failure_keeps_valid_writes_but_never_marker(store, monkeypatch, parse_state):
    service, parser, _ = worker(monkeypatch, scans(candidate()))
    join = Mock()
    monkeypatch.setattr(parser, "join", join)
    run = Mock(return_value={"state": parse_state, "failed": 1})
    monkeypatch.setattr(parser, "run", run)
    outcome = service.run()
    assert outcome["exit_code"] != 0
    assert len(tables()["tasks"]) == 1 and models.get_last_sync("full_scan") is None
    assert run.call_count == (2 if parse_state == "busy" else 1)
    assert join.call_count == (1 if parse_state == "busy" else 0)
    if parse_state == "busy":
        assert 0 < join.call_args.args[0] <= 300
        assert run.call_args_list[0].kwargs["deadline"] == run.call_args_list[1].kwargs["deadline"]


def test_parse_exact_snapshot_excludes_later_arrival_and_no_early_marker(store, monkeypatch):
    first = store(parse_status="queued", parse_intent="coaching_only")
    service, parser, runtime = worker(monkeypatch)
    later = []
    original = parser.run
    def capture(task_ids=None, *, deadline=None):
        assert task_ids == (first["id"],) and models.get_last_sync("full_scan") is None
        later.append(store(parse_status="queued", parse_intent="coaching_only"))
        return original(task_ids, deadline=deadline)
    monkeypatch.setattr(parser, "run", capture)
    assert service.run()["state"] == "succeeded"
    assert raw(first["id"])["parse_status"] == "parsed"
    assert raw(later[0]["id"])["parse_status"] == "queued"


@pytest.mark.parametrize("phase", ["scan", "directory", "write", "parse", "coaching", "marker"])
def test_one_absolute_deadline_in_every_phase(store, monkeypatch, phase):
    clock = [100.0]
    runtime = scans(candidate())
    service, parser, runtime = worker(monkeypatch, runtime, monotonic=lambda: clock[0])
    if phase in {"scan", "directory", "coaching"}:
        def advance(operation, payload):
            if ((phase == "scan" and operation == "ask" and payload.get("batch") == "flagged")
                or (phase == "directory" and operation == "email")
                or (phase == "coaching" and operation == "ask" and payload.get("operation") == "coaching")):
                clock[0] = 401.0
        runtime.before = advance
    else:
        target, method = (parser, "run") if phase == "parse" else (models, "write_refresh_candidate" if phase == "write" else "commit_refresh_marker")
        original = getattr(target, method)
        def advance(*args, **kwargs):
            clock[0] = 401.0
            return original(*args, **kwargs)
        monkeypatch.setattr(target, method, advance)
    assert service.run()["state"] != "succeeded"
    assert models.get_last_sync("full_scan") is None
    assert all(call[2] <= 300 for call in runtime.calls)


@pytest.mark.parametrize("expire_at_marker", [False, True])
def test_original_deadline_shrinks_across_successful_phases(store, monkeypatch, expire_at_marker):
    queued = store(title="Queued parser target", parse_status="queued", parse_intent="coaching_only")
    store(title="Legacy action", coaching_text="Break this down \u2014 what's the very first concrete step?")
    store(title="Original project assumption", source_type="chat",
          source_id="chat::taylor@example.test::different subject")
    clock = [100.0]
    service, parser, runtime = worker(monkeypatch, scans(candidate()), monotonic=lambda: clock[0])
    def consume(operation, payload):
        clock[0] += 20
    runtime.before = consume
    read_self = runtime.read_self_profile
    def self_profile(*, timeout):
        result = read_self(timeout=timeout)
        clock[0] += 20
        return result
    runtime.read_self_profile = self_profile
    checkpoints = []
    def timed_writer(name, cost):
        original = getattr(models, name)
        def write(*args, check_deadline, **kwargs):
            checkpoints.append((name, check_deadline()))
            clock[0] += cost
            return original(*args, check_deadline=check_deadline, **kwargs)
        monkeypatch.setattr(models, name, write)
    timed_writer("write_refresh_candidate", 20)
    timed_writer("write_refresh_coaching", 20)
    timed_writer("commit_refresh_marker", 61 if expire_at_marker else 20)
    parse = parser.run
    def timed_parse(task_ids=None, *, deadline=None):
        assert deadline == 400.0 and task_ids == (queued["id"],)
        checkpoints.append(("parse", deadline - clock[0]))
        clock[0] += 20
        return parse(task_ids, deadline=deadline)
    monkeypatch.setattr(parser, "run", timed_parse)

    outcome = service.run()
    assert outcome["state"] == ("blocked" if expire_at_marker else "succeeded")
    assert [call[2] for call in runtime.calls] == [300, 280, 260, 240, 220, 200, 180, 120, 100]
    assert checkpoints == [
        ("write_refresh_candidate", 160), ("parse", 140),
        ("write_refresh_coaching", 80), ("commit_refresh_marker", 60),
    ]
    assert raw(queued["id"])["parse_status"] == "parsed"
    assert outcome["created"] == outcome["coaching_upgraded"] == outcome["parse_completed"] == 1
    assert (models.get_last_sync("full_scan") is None) is expire_at_marker


def test_legacy_coaching_batched_preserves_notes_people_skill_and_guards_delivery(store, monkeypatch):
    old = "Break this down \u2014 what's the very first concrete step?"
    tasks = [store(title=f"Legacy {i}", coaching_text=old, skill_output="saved", user_notes="@WorkIQ question?")
             for i in range(3)]
    delivery(tasks[1]["id"], "execute_unconfirmed")
    before = [raw(t["id"]) for t in tasks]
    actions = tables()["task_actions"]
    service, _, runtime = worker(monkeypatch)
    outcome = service.run()
    assert outcome["coaching_upgraded"] == 2 and outcome["deferred"] == 1
    assert raw(tasks[1]["id"]) == before[1]
    assert raw(tasks[1]["id"])["cowork_revision"] == before[1]["cowork_revision"]
    assert tables()["task_actions"] == actions
    asks = [c for c in runtime.calls if c[0] == "ask" and c[1].get("operation") == "coaching"]
    assert len(asks) == 1 and len(asks[0][1]["tasks"]) == 2
    assert all(item["questions"] == [] for item in asks[0][1]["tasks"])
    for index in [0, 2]:
        after = raw(tasks[index]["id"])
        assert after["cowork_revision"] == before[index]["cowork_revision"] + 1
        allowed = {"coaching_text", "suggestion_refreshed_at", "updated_at", "cowork_revision"}
        assert {k: v for k, v in after.items() if k not in allowed} == {
            k: v for k, v in before[index].items() if k not in allowed}


def test_unchanged_legacy_coaching_does_not_increment_revision_or_rewrite_actions(store, monkeypatch):
    old = "Break this down \u2014 what's the very first concrete step?"
    task = store(coaching_text=old, skill_output="Saved draft", user_notes="Keep notes")
    edit(task["id"], cowork_revision=11)
    models.create_task_action(task["id"], intent="Existing reviewed action")
    before, actions = raw(task["id"]), tables()["task_actions"]
    service, _, runtime = worker(monkeypatch)
    def unchanged(value):
        for item in value["results"]:
            item["result"]["coaching_text"] = old
        return value
    runtime.coach_mutation = unchanged
    assert service.run()["state"] == "succeeded"
    after = raw(task["id"])
    assert after["cowork_revision"] == 11
    assert tables()["task_actions"] == actions
    assert {key: value for key, value in after.items() if key not in {"updated_at", "suggestion_refreshed_at"}} == {
        key: value for key, value in before.items() if key not in {"updated_at", "suggestion_refreshed_at"}
    }


def test_late_invalid_coaching_inner_has_zero_batch_writes(store, monkeypatch):
    for i in range(2):
        store(title=f"Legacy {i}", coaching_text="Repeated generic advice.")
    before = tables()
    runtime = Runtime()
    def corrupt(value):
        value["results"][-1]["result"]["answers"] = [{"question_id": 0, "answer": "Not allowed"}]
        return value
    runtime.coach_mutation = corrupt
    service, _, _ = worker(monkeypatch, runtime)
    assert service.run()["state"] != "succeeded"
    assert tables() == before


def test_shared_refresh_slot_and_marker_precedes_success(store, monkeypatch):
    service, _, runtime = worker(monkeypatch)
    entered, release = threading.Event(), threading.Event()
    def pause(operation, payload):
        if operation == "ask" and payload.get("batch") == "direct":
            entered.set()
            assert release.wait(5)
    runtime.before = pause
    admission = service.launch()
    try:
        assert entered.wait(5)
        other = type(service)(runtime_provider=lambda: runtime)
        assert other.launch()["state"] == "busy"
        assert models.get_last_sync("full_scan") is None and service.completion() is None
    finally:
        release.set()
        service.join(5)
    assert service.completion()["run_id"] == admission["run_id"]
    assert models.get_last_sync("full_scan")["id"] > 0


@pytest.mark.parametrize("stamp,days", [
    (None, 7), ("2026-09-19T00:00:00Z", 1), ("2026-09-16T00:00:00Z", 3),
    ("2026-08-01T00:00:00Z", 7),
])
def test_scan_window_overlap_and_cap(store, monkeypatch, stamp, days):
    refresh = importlib.import_module("src.services.refresh")
    assert refresh.scan_days(stamp, datetime(2026, 9, 19, 12, tzinfo=timezone.utc)) == days


def test_malformed_cursor_is_failure_before_any_provider_call(store, monkeypatch):
    marker = models.log_sync("full_scan")
    conn = db.get_connection()
    conn.execute("UPDATE sync_log SET synced_at='broken' WHERE id=?", (marker["id"],))
    conn.commit()
    conn.close()
    service, _, runtime = worker(monkeypatch)
    before = tables()
    assert service.run()["state"] != "succeeded"
    assert not runtime.calls and tables() == before


@pytest.mark.parametrize("overlap,expected", [(49, 2), (50, 1)])
def test_jaccard_exact_threshold_and_all_status_fuzzy_suppression(store, monkeypatch, overlap, expected):
    words = [f"{chr(97 + i // 10)}{i % 10}" for i in range(100)]
    old = " ".join(words)
    new = " ".join(words[:overlap])
    task = store(title=old, source_type="chat", source_id="chat::taylor@example.test::old topic",
                 status="deleted", coaching_text="Keep")
    before = raw(task["id"])
    service, _, _ = worker(monkeypatch, scans(candidate(title=new)))
    assert service.run()["state"] == "succeeded"
    assert len(tables()["tasks"]) == expected and raw(task["id"]) == before


@pytest.mark.parametrize("different", ["person", "kind", "unknown"])
def test_in_batch_dedup_never_uses_unscoped_title(store, monkeypatch, different):
    first = candidate()
    second = candidate(root_topic="Other topic")
    if different == "person":
        second["people_hints"][0]["email"] = "other@example.test"
    elif different == "kind":
        second["source_kind"] = "meeting"
    else:
        first["people_hints"][0]["name"] = "First"
        first["people_hints"][0]["email"] = None
        second["people_hints"][0]["name"] = "Other"
        second["people_hints"][0]["email"] = None
    runtime = scans(first, second)
    original = runtime.read_directory_user_by_email
    def profile(email, *, timeout):
        if email == "other@example.test":
            return {**PROFILE, "email": email, "upn": email, "display_name": "Other Example",
                    "aad_object_id": "33333333-3333-4333-8333-333333333333"}
        return original(email, timeout=timeout)
    runtime.read_directory_user_by_email = profile
    service, _, _ = worker(monkeypatch, runtime)
    assert service.run()["created"] == 2
    assert len(tables()["tasks"]) == 2


@pytest.mark.parametrize("primary", [0, 1])
def test_meeting_primary_is_explicit_not_first_resolved(store, monkeypatch, primary):
    item = candidate(source_kind="meeting", primary_person_hint_index=primary, people_hints=[
        {"name": "Unknown", "email": None, "upn": None, "aad_object_id": None},
        {"name": PROFILE["display_name"], "email": PROFILE["email"], "upn": None, "aad_object_id": None},
    ])
    service, _, _ = worker(monkeypatch, scans(item))
    assert service.run()["state"] == "succeeded"
    task = tables()["tasks"][0]
    assert task["source_id"].startswith("proposal::" if primary == 0 else "meeting::taylor@example.test::")
    if primary == 0:
        assert not tables()["task_person"]


def test_awaiting_recipient_not_self_owns_scope(store, monkeypatch):
    item = candidate(action_type="awaiting-response", primary_person_hint_index=1, people_hints=[
        {"name": SELF["display_name"], "email": SELF["email"], "upn": None, "aad_object_id": None},
        {"name": PROFILE["display_name"], "email": PROFILE["email"], "upn": None, "aad_object_id": None},
    ])
    runtime = Runtime({"awaiting": envelope("awaiting", [item])})
    runtime.read_directory_user_by_email = lambda email, *, timeout: dict(SELF if email == SELF["email"] else PROFILE)
    service, _, _ = worker(monkeypatch, runtime)
    assert service.run()["state"] == "succeeded"
    task = tables()["tasks"][0]
    assert task["source_id"] == "chat::taylor@example.test::release checklist"
    assert len(json.loads(task["key_people"])) == 1


@pytest.mark.parametrize("state", ["suggested", "completed", "waiting", "dismissed"])
def test_semantic_offers_only_explicit_eligible_same_owner_ids(store, monkeypatch, state):
    task = store(title="Entirely different wording", status=state,
                 source_id="chat::taylor@example.test::old topic", source_type="chat")
    service, _, runtime = worker(monkeypatch, scans(candidate()))
    assert service.run()["state"] == "succeeded"
    semantic = [c[1] for c in runtime.calls if c[0] == "ask" and c[1].get("operation") == "semantic"]
    if state in {"suggested", "completed"}:
        assert len(semantic) == 1
        assert semantic[0]["candidates"][0]["existing"][0]["id"] == task["id"]
    else:
        assert semantic == []


@pytest.mark.parametrize("when", ["before", "after"])
def test_delivery_deferral_preserves_matched_and_legacy_coaching_rows(store, monkeypatch, when):
    task = store(source_type="chat", source_id="chat::taylor@example.test::release checklist",
                 source_snippet="[M365 2026-09-18T12:00:00Z] Old")
    service, _, runtime = worker(monkeypatch, scans(candidate()))
    if when == "before":
        delivery(task["id"], "execute_unconfirmed")
    else:
        original = models.write_refresh_candidate
        def race(snapshot, projection, profiles, *, check_deadline):
            delivery(task["id"], "execute_unconfirmed")
            return original(snapshot, projection, profiles, check_deadline=check_deadline)
        monkeypatch.setattr(models, "write_refresh_candidate", race)
    before = raw(task["id"])
    outcome = service.run()
    assert outcome["state"] == "succeeded" and outcome["deferred"] == 1
    assert raw(task["id"]) == before


def test_concurrent_new_source_collision_never_inserts_duplicate(store, monkeypatch):
    service, _, _ = worker(monkeypatch, scans(candidate()))
    original = models.write_refresh_candidate
    def race(snapshot, projection, profiles, *, check_deadline):
        store(source_type="chat", source_id=projection["source_id"])
        return original(snapshot, projection, profiles, check_deadline=check_deadline)
    monkeypatch.setattr(models, "write_refresh_candidate", race)
    assert service.run()["state"] != "succeeded"
    assert len(tables()["tasks"]) == 1 and models.get_last_sync("full_scan") is None


def test_parse_cancellation_does_not_cancel_refresh_identity_preflight(store, monkeypatch):
    event = threading.Event()
    event.set()
    monkeypatch.setattr(parsing, "_cancel", event)
    service, _, _ = worker(monkeypatch, scans(candidate()))
    assert service.run()["created"] == 1


def test_existing_signal_consumed_only_after_success_marker(store, monkeypatch, tmp_path):
    signal = tmp_path / ".sync_requested"
    signal.write_text("existing request")
    service, _, runtime = worker(monkeypatch, Runtime({"direct": "{}"}))
    assert service.run()["state"] != "succeeded"
    assert signal.exists()
    runtime.batches = {}
    assert service.run()["state"] == "succeeded"
    assert not signal.exists() and models.get_last_sync("full_scan") is not None


def test_cached_profile_preflight_does_not_invalidate_next_candidate(store, monkeypatch):
    service, _, runtime = worker(monkeypatch, scans(candidate()))
    assert service.run()["state"] == "succeeded"
    runtime.batches = {"direct": envelope(candidates=[
        candidate(root_topic="Other topic", title="Prepare unrelated workshop", action_type="prepare"),
        candidate(root_topic="New initiative", title="Document architecture decisions", action_type="prepare"),
    ])}
    outcome = service.run()
    assert outcome["state"] == "succeeded" and outcome["created"] == 2


@pytest.mark.parametrize("name_first", [False, True], ids=["email-then-name", "name-then-email"])
def test_warm_cache_mixed_hints_merge_missing_upn_and_retain_prior_snapshot(store, monkeypatch, name_first):
    warm, _, runtime = worker(monkeypatch, scans(candidate(
        title="Confirm budget allocation", root_topic="Warm identity cache",
    )))
    assert warm.run()["state"] == "succeeded"
    before = tables()
    cached = parsing._canonical({"email": PROFILE["email"]})
    assert cached["upn"] is None and cached["_prior_identity"]
    email = candidate(title="Prepare workshop inventory", root_topic="Workshop inventory")
    name = candidate(title="Document architecture rationale", root_topic="Architecture rationale",
                     people_hints=[{"name": PROFILE["display_name"], "email": None,
                                    "upn": None, "aad_object_id": None}])
    runtime.batches = {"direct": envelope(candidates=[name, email] if name_first else [email, name])}
    runtime.calls.clear()
    proofs = []
    original = models.write_refresh_candidate
    def capture(snapshot, projection, profiles, *, check_deadline):
        proofs.extend(json.loads(json.dumps(profiles)))
        return original(snapshot, projection, profiles, check_deadline=check_deadline)
    monkeypatch.setattr(models, "write_refresh_candidate", capture)
    service, _, _ = worker(monkeypatch, runtime)
    outcome = service.run()
    assert outcome["state"] == "succeeded" and outcome["created"] == 2
    assert [call[0] for call in runtime.calls if call[0] in {"email", "name", "aad"}] == ["name"]
    assert len(proofs) == 4
    assert all(proof["upn"] == PROFILE["upn"] for proof in proofs)
    assert all(proof["_prior_identity"] == cached["_prior_identity"] for proof in proofs)
    after = tables()
    assert after["person"] == before["person"] and after["person_alias"] == before["person_alias"]
    assert {json.loads(row["key_people"])[0]["upn"] for row in after["tasks"][1:]} == {PROFILE["upn"]}
    assert json.loads(models.get_last_sync("full_scan")["result_summary"])["run_id"] == outcome["run_id"]


@pytest.mark.parametrize("field,value", [
    ("email", "other@example.test"), ("upn", "different@example.test"),
    ("aad_object_id", "33333333-3333-4333-8333-333333333333"),
])
def test_conflicting_nonempty_profile_fields_still_abort_preflight(store, monkeypatch, field, value):
    hint = {"name": PROFILE["display_name"], "email": None, "upn": None, "aad_object_id": None}
    runtime = scans(candidate(people_hints=[hint]), candidate(root_topic="Second topic", people_hints=[hint]))
    profiles = iter([dict(PROFILE), {**PROFILE, field: value}])
    runtime.find_directory_users_by_exact_name = lambda name, *, timeout: [next(profiles)]
    service, _, _ = worker(monkeypatch, runtime)
    before = tables()
    assert service.run()["state"] == "failed"
    assert tables() == before


@pytest.mark.parametrize("partial", ["field", "pagination", "contradictory_address"])
def test_partial_or_contradictory_profile_fails_before_any_candidate_write(store, monkeypatch, partial):
    runtime = scans(candidate())
    if partial == "field":
        del runtime.profile["upn"]
    elif partial == "pagination":
        runtime.profile["@odata.nextLink"] = "private"
    else:
        # A known, verified address cannot silently become a different AAD root.
        first, _, _ = worker(monkeypatch, runtime)
        assert first.run()["state"] == "succeeded"
        runtime.batches = {"direct": envelope(candidates=[candidate(people_hints=[
            {"name": "Other Example", "email": None, "upn": None,
             "aad_object_id": "33333333-3333-4333-8333-333333333333"}])])}
        runtime.profile["aad_object_id"] = "33333333-3333-4333-8333-333333333333"
    service, _, _ = worker(monkeypatch, runtime)
    before = tables()
    assert service.run()["state"] != "succeeded"
    assert tables() == before


@pytest.mark.parametrize("failed", [False, True])
def test_busy_shared_real_parser_is_joined_once_under_same_deadline(store, monkeypatch, failed):
    task = store(parse_status="queued", parse_intent="coaching_only")
    service, parser, runtime = worker(monkeypatch)
    entered, release = threading.Event(), threading.Event()
    def pause(operation, payload):
        if operation == "ask" and "mode" in payload:
            entered.set()
            assert release.wait(5)
    runtime.before = pause
    if failed:
        runtime.parse_answer = "{}"
    parser.launch((task["id"],))
    joins = []
    original = parser.join
    def join(timeout):
        assert models.get_last_sync("full_scan") is None
        joins.append(timeout)
        release.set()
        return original(timeout)
    monkeypatch.setattr(parser, "join", join)
    try:
        assert entered.wait(5)
        outcome = service.run()
        assert len(joins) == 1 and 0 < joins[0] <= 300
        assert (outcome["state"] == "succeeded") is not failed
        assert (models.get_last_sync("full_scan") is None) is failed
        assert raw(task["id"])["parse_status"] == ("error" if failed else "parsed")
    finally:
        release.set()
        original(5)


def test_actual_parse_partial_retains_new_suggestion_but_no_marker(store, monkeypatch):
    task = store(parse_status="queued", parse_intent="coaching_only")
    service, parser, runtime = worker(monkeypatch, scans(candidate()))
    runtime.parse_answer = "{}"
    outcome = service.run()
    assert outcome["state"] == "blocked" and parser.completion()["state"] == "partial"
    assert raw(task["id"])["parse_status"] == "error"
    assert len(tables()["tasks"]) == 2
    assert models.get_last_sync("full_scan") is None


def test_new_source_coaching_is_not_selected_as_legacy_in_same_run(store, monkeypatch):
    runtime = scans(candidate(), candidate(source_kind="meeting"))
    def same_text(value):
        for item in value["results"]:
            item["result"]["coaching_text"] = "Synthetic repeated model output"
        return value
    runtime.coach_mutation = same_text
    service, _, _ = worker(monkeypatch, runtime)
    result = service.run()
    assert result["state"] == "succeeded" and result["created"] == 2
    assert result["coaching_upgraded"] == 0
    assert len([c for c in runtime.calls if c[0] == "ask" and c[1].get("operation") == "coaching"]) == 1


@pytest.mark.parametrize("change", ["delivery", "user_edit"])
def test_coaching_generation_has_no_db_lock_and_late_guard_is_atomic(store, monkeypatch, change):
    task = store(coaching_text="Break this down \u2014 what's the very first concrete step?",
                 skill_output="Keep", user_notes="Keep")
    before = raw(task["id"])
    service, _, runtime = worker(monkeypatch)
    changed = []
    def race(operation, payload):
        if operation == "ask" and payload.get("operation") == "coaching":
            if change == "delivery":
                delivery(task["id"], "executing")
            else:
                edit(task["id"], user_notes="New user note")
            changed.append(tables())
    runtime.before = race
    outcome = service.run()
    assert len(changed) == 1
    assert raw(task["id"]) == changed[0]["tasks"][0]
    assert tables()["task_actions"] == changed[0]["task_actions"]
    assert outcome["state"] == ("succeeded" if change == "delivery" else "failed")
    assert (models.get_last_sync("full_scan") is None) == (change == "user_edit")
    assert raw(task["id"])["coaching_text"] == before["coaching_text"]


def test_source_url_is_only_derived_context_and_never_read_or_model_identity(store, monkeypatch):
    url = "https://teams.microsoft.com/l/message/19:synthetic@thread.v2/1234567890"
    service, _, runtime = worker(monkeypatch, scans(candidate(source_ref=url)))
    assert service.run()["state"] == "succeeded"
    task = tables()["tasks"][0]
    assert task["source_url"] == url
    assert json.loads(task["source_locator"])["source"] == "derived_from_url"
    assert task["source_id"] == "chat::taylor@example.test::release checklist"
    assert all(call[0] in {"ask", "self", "email"} for call in runtime.calls)
    assert "SECRET-CONVERSATION" not in json.dumps(tables())
