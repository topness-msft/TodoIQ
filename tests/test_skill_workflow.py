"""Standalone generation ordering and preservation, using synthetic data only."""

import json
import threading
from unittest.mock import Mock

import pytest

from src import db, models
from src.services import generation, skills, parsing, cowork_runner
from tests.test_parse_workflow import raw, edit, PROFILE, SELF, calendar_results


PEOPLE = [{"name": PROFILE["display_name"], "email": PROFILE["email"]}]
OUTPUTS = {
    "respond-email": {"to": 0, "subject": "Review", "body": "Please review.",
                      "tone": "Direct", "key_points": ["Review"]},
    "teams-message": {"to": 0, "message": "Please review.", "tone": "Direct", "purpose": "Review"},
    "follow-up": {"to": 0, "channel": "Teams", "subject": None, "message": "Any update?",
                  "last_interaction": "unknown", "days_since_contact": None, "urgency": "Normal"},
    "prepare": {"event": "Review", "date": None, "checklist": ["Read notes"],
                "talking_points": ["Decision"], "materials": ["Notes"], "questions": ["Ready?"],
                "estimate_minutes": 25},
}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "skills.db")
    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    conn = db.get_connection()
    db.init_db(conn)
    conn.close()
    monkeypatch.setattr(skills, "get_runtime", Mock(side_effect=AssertionError("No live runtime")))
    from src.services import claude_runner
    monkeypatch.setattr(claude_runner, "run_copilot", Mock(side_effect=AssertionError("No CLI")))

    def seed(**changes):
        task = models.create_task(title="Review release", source_type="manual",
                                  key_people=json.dumps(PEOPLE), parse_status="parsed")
        edit(task["id"], skill_output="old output", cowork_prompt="old prompt", **changes)
        return task
    return seed


def runtime():
    result = Mock()
    result.execute_ask.side_effect = lambda prompt, **kwargs: {
        "answer": json.dumps(OUTPUTS[json.loads(prompt.split("\nSOURCE_DATA_JSON:\n")[1])["skill"]]),
        "conversation_id": "PRIVATE-PROVIDER-ID",
    }
    result.read_self_profile.return_value = SELF
    result.execute_calendar.side_effect = calendar_results()
    return result


def finish(worker, admitted):
    assert admitted["ok"], admitted
    worker.join(admitted["run_id"], 5)
    completed = worker.completion(admitted["run_id"])
    assert completed is not None
    return completed


@pytest.mark.parametrize("skill", sorted(skills.VALID_SKILLS))
def test_six_browserless_paths_replace_only_bound_output(store, skill):
    task = store()
    before = raw(task["id"])
    provider = runtime()
    worker = skills.SkillService(runtime_provider=lambda: provider)
    completed = finish(worker, worker.launch(task["id"], skill))
    assert completed["state"] == "succeeded" and completed["persisted"] is True
    after = raw(task["id"])
    target = "cowork_prompt" if skill == "cowork-prompt" else "skill_output"
    assert after[target] != before[target]
    assert after["suggestion_refreshed_at"] and after["updated_at"]
    for field in before.keys() - {target, "updated_at", "suggestion_refreshed_at"}:
        assert after[field] == before[field], field
    assert "<<<" not in after[target] and "PRIVATE" not in json.dumps(completed)
    if skill in {"cowork-prompt", "schedule-meeting"}:
        provider.execute_ask.assert_not_called()
    provider.read_directory_user_by_email.assert_not_called()


@pytest.mark.parametrize("newest_fails", [False, True])
def test_reverse_completion_and_failed_newest_never_revive_old(store, newest_fails):
    task = store()
    entered, release = threading.Event(), threading.Event()
    provider = runtime()
    original = provider.execute_ask.side_effect
    def ask(prompt, **kwargs):
        if '"respond-email"' in prompt.split("SOURCE_DATA_JSON:")[1]:
            entered.set()
            assert release.wait(5)
        elif newest_fails:
            raise RuntimeError("PRIVATE")
        return original(prompt, **kwargs)
    provider.execute_ask.side_effect = ask
    worker = skills.SkillService(runtime_provider=lambda: provider)
    old = worker.launch(task["id"], "respond-email")
    assert entered.wait(5)
    busy = worker.launch(task["id"], "respond-email")
    assert busy["ok"] is False and "already running" in busy["message"]
    try:
        new = finish(worker, worker.launch(task["id"], "prepare"))
        expected = "old output" if newest_fails else raw(task["id"])["skill_output"]
        assert new["state"] == ("failed" if newest_fails else "succeeded")
    finally:
        release.set()
    assert finish(worker, old)["state"] == "superseded"
    assert raw(task["id"])["skill_output"] == expected


@pytest.mark.parametrize("field,value", [
    ("title", "Changed"), ("description", "Changed"), ("action_type", "prepare"),
    ("key_people", "[]"), ("user_notes", "Changed"), ("due_date", "2030-01-01"),
    ("related_meeting", "Changed"), ("source_type", "email"), ("source_url", "Changed"),
    ("source_snippet", "Changed"), ("source_locator", "{}"), ("status", "completed"),
    ("raw_input", "Changed"), ("skill_output", None),
])
def test_generation_input_edit_or_clear_rejects_without_timestamp_churn(store, field, value):
    task = store()
    provider = runtime()
    original = provider.execute_ask.side_effect
    snapshots = []
    def ask(prompt, **kwargs):
        edit(task["id"], **{field: value})
        snapshots.append(raw(task["id"]))
        return original(prompt, **kwargs)
    provider.execute_ask.side_effect = ask
    worker = skills.SkillService(runtime_provider=lambda: provider)
    assert finish(worker, worker.launch(task["id"], "prepare"))["state"] == "stale"
    assert raw(task["id"]) == snapshots[0]


def test_incidental_other_output_and_timestamps_are_not_dependencies(store):
    task = store()
    provider = runtime()
    original = provider.execute_ask.side_effect
    def ask(prompt, **kwargs):
        edit(task["id"], cowork_prompt="new prompt", updated_at="other stamp",
             suggestion_refreshed_at="other stamp")
        return original(prompt, **kwargs)
    provider.execute_ask.side_effect = ask
    worker = skills.SkillService(runtime_provider=lambda: provider)
    assert finish(worker, worker.launch(task["id"], "prepare"))["state"] == "succeeded"
    assert raw(task["id"])["cowork_prompt"] == "new prompt"


def test_both_columns_commit_and_parse_cancellation_is_independent(store, monkeypatch):
    task = store()
    entered, release = threading.Event(), threading.Event()
    provider = runtime()
    original = provider.execute_ask.side_effect
    def ask(prompt, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(prompt, **kwargs)
    provider.execute_ask.side_effect = ask
    worker = skills.SkillService(runtime_provider=lambda: provider)
    normal = worker.launch(task["id"], "prepare")
    assert entered.wait(5)
    try:
        parse_cancel = threading.Event()
        parse_cancel.set()
        monkeypatch.setattr(parsing, "_cancel", parse_cancel)
        assert finish(worker, worker.launch(task["id"], "cowork-prompt"))["state"] == "succeeded"
    finally:
        release.set()
    assert finish(worker, normal)["state"] == "succeeded"
    assert raw(task["id"])["cowork_prompt"] != "old prompt"
    assert raw(task["id"])["skill_output"] != "old output"


@pytest.mark.parametrize("outcome", ["failure", "malformed", "timeout", "cancel", "deleted", "settings"])
def test_failure_preserves_history_and_request_cancellation_is_local(store, monkeypatch, outcome):
    task = store()
    other = store()
    before = raw(task["id"])
    entered, release = threading.Event(), threading.Event()
    provider = runtime()
    original = provider.execute_ask.side_effect
    now = [0.]
    def ask(prompt, **kwargs):
        entered.set()
        assert release.wait(5)
        if outcome == "failure":
            raise RuntimeError("PRIVATE source secret")
        if outcome == "malformed":
            return {"answer": "<<<SKILL_OUTPUT>>>private<<<END_SKILL_OUTPUT>>>"}
        if outcome == "timeout":
            now[0] = 421.
        if outcome == "deleted":
            conn = db.get_connection()
            conn.execute("DELETE FROM tasks WHERE id=?", (task["id"],))
            conn.commit()
            conn.close()
        if outcome == "settings":
            monkeypatch.setattr(cowork_runner, "standing_instructions", lambda: "Changed")
        return original(prompt, **kwargs)
    provider.execute_ask.side_effect = ask
    worker = skills.SkillService(runtime_provider=lambda: provider, monotonic=lambda: now[0])
    admitted = worker.launch(task["id"], "prepare")
    assert entered.wait(5)
    try:
        if outcome == "cancel":
            assert worker.cancel(admitted["run_id"])
            assert not worker.cancel("unknown")
            assert finish(worker, worker.launch(other["id"], "cowork-prompt"))["state"] == "succeeded"
    finally:
        release.set()
    completed = finish(worker, admitted)
    assert completed["state"] in {"failed", "timed_out", "cancelled", "stale"}
    assert completed["persisted"] is False and "PRIVATE" not in json.dumps(completed)
    if outcome != "deleted":
        assert raw(task["id"]) == before
    assert not worker.cancel(admitted["run_id"])


def test_admission_cannot_cross_latest_check_and_sql_commit(store, monkeypatch):
    task = store()
    worker = skills.SkillService(runtime_provider=runtime)
    checked, release, admitted = threading.Event(), threading.Event(), threading.Event()
    original = worker._persist
    def persist(request, text):
        if request.skill == generation.Skill.RESPOND_EMAIL:
            checked.set()
            assert release.wait(5)
        return original(request, text)
    monkeypatch.setattr(worker, "_persist", persist)
    old = worker.launch(task["id"], "respond-email")
    assert checked.wait(5)
    next_run = []
    def start():
        next_run.append(worker.launch(task["id"], "prepare"))
        admitted.set()
    thread = threading.Thread(target=start)
    thread.start()
    try:
        assert not admitted.wait(.05)
    finally:
        release.set()
        thread.join(5)
    assert finish(worker, old)["state"] == "succeeded"
    assert finish(worker, next_run[0])["state"] == "succeeded"
    assert raw(task["id"])["skill_output"].startswith("Preparation Notes:")


@pytest.mark.parametrize("field,value", [
    ("coaching_text", "Changed"), ("skill_output", "Changed"), ("cowork_prompt", None),
])
def test_cowork_actual_inputs_and_explicit_clear_are_guarded(store, monkeypatch, field, value):
    task = store()
    original = generation.cowork_prompt
    after_edit = []
    def compose(*args):
        output = original(*args)
        edit(task["id"], **{field: value})
        after_edit.append(raw(task["id"]))
        return output
    monkeypatch.setattr(generation, "cowork_prompt", compose)
    worker = skills.SkillService(runtime_provider=runtime)
    assert finish(worker, worker.launch(task["id"], "cowork-prompt"))["state"] == "stale"
    assert raw(task["id"]) == after_edit[0]


def test_sql_failure_rolls_back_and_never_reports_persisted(store):
    task = store()
    before = raw(task["id"])
    conn = db.get_connection()
    conn.execute("""CREATE TRIGGER reject_skill BEFORE UPDATE OF skill_output ON tasks
                    BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END""")
    conn.close()
    worker = skills.SkillService(runtime_provider=runtime)
    completed = finish(worker, worker.launch(task["id"], "prepare"))
    assert completed["state"] == "failed" and completed["persisted"] is False
    assert raw(task["id"]) == before


def test_late_cleanup_and_cancel_cannot_touch_replacement_request(store, monkeypatch):
    task = store()
    worker = skills.SkillService(runtime_provider=runtime)
    old_requests = []
    original = worker._finish
    def finish_spy(request, state):
        old_requests.append(request)
        original(request, state)
    monkeypatch.setattr(worker, "_finish", finish_spy)
    first = worker.launch(task["id"], "prepare")
    assert finish(worker, first)["state"] == "succeeded"
    entered, release = threading.Event(), threading.Event()
    provider = runtime()
    ask = provider.execute_ask.side_effect
    def blocked(prompt, **kwargs):
        entered.set()
        assert release.wait(5)
        return ask(prompt, **kwargs)
    provider.execute_ask.side_effect = blocked
    worker._runtime_provider = lambda: provider
    newer = worker.launch(task["id"], "prepare")
    assert entered.wait(5)
    try:
        original(old_requests[0], "failed")
        assert not worker.cancel(first["run_id"])
        assert worker.status()["_runs"][f'skill:prepare:{task["id"]}']["run_id"] == newer["run_id"]
    finally:
        release.set()
    assert finish(worker, newer)["state"] == "succeeded"


@pytest.mark.parametrize("cancel", [True, False])
def test_concurrent_model_requests_have_independent_cancellation_and_budgets(store, monkeypatch, cancel):
    first_task, second_task = store(), store()
    entered, release = threading.Event(), threading.Event()
    provider = runtime()
    original = provider.execute_ask.side_effect
    now = [0.]
    budgets = []
    def ask(prompt, *, timeout):
        budgets.append(timeout)
        if json.loads(prompt.split("\nSOURCE_DATA_JSON:\n")[1])["skill"] == "respond-email":
            entered.set()
            assert release.wait(5)
        return original(prompt, timeout=timeout)
    provider.execute_ask.side_effect = ask
    parse_cancel = threading.Event()
    monkeypatch.setattr(parsing, "_cancel", parse_cancel)
    worker = skills.SkillService(runtime_provider=lambda: provider, monotonic=lambda: now[0])
    older = worker.launch(first_task["id"], "respond-email", timeout=10)
    assert entered.wait(5)
    try:
        if cancel:
            assert worker.cancel(older["run_id"])
        else:
            now[0] = 11.
        newer = worker.launch(second_task["id"], "prepare", timeout=420)
        assert finish(worker, newer)["state"] == "succeeded"
        assert not parse_cancel.is_set()
    finally:
        release.set()
    assert finish(worker, older)["state"] == ("cancelled" if cancel else "timed_out")
    assert budgets == [10, 420]
    provider.shutdown.assert_not_called()
    assert raw(first_task["id"])["skill_output"] == "old output"


@pytest.mark.parametrize("error_name", ["AuthRequiredError", "ConsentRequiredError", "EulaRequiredError",
                                        "CapabilityDeniedError", "NotReadyError"])
def test_provider_readiness_failure_is_bounded_and_never_persisted(store, error_name):
    from src.services import workiq_runtime
    task = store()
    before = raw(task["id"])
    provider = runtime()
    provider.execute_ask.side_effect = getattr(workiq_runtime, error_name)("PRIVATE DETAILS")
    worker = skills.SkillService(runtime_provider=lambda: provider)
    completed = finish(worker, worker.launch(task["id"], "prepare"))
    assert completed["state"] == "failed" and completed["persisted"] is False
    assert "PRIVATE" not in json.dumps(completed) and raw(task["id"]) == before
    provider.shutdown.assert_not_called()


def test_failed_thread_start_has_no_active_run_or_write(store, monkeypatch):
    task = store()
    before = raw(task["id"])
    worker = skills.SkillService(runtime_provider=runtime)
    monkeypatch.setattr(skills.threading.Thread, "start", Mock(side_effect=RuntimeError("PRIVATE")))
    assert worker.launch(task["id"], "prepare")["ok"] is False
    assert worker.status() == {"_runs": {}}
    assert raw(task["id"]) == before
