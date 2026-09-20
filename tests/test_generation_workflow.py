import json
from unittest.mock import Mock

import pytest

from src.services import generation, skills, cowork_runner
from tests.test_skill_workflow import store, runtime, finish, PEOPLE
from tests.test_parse_workflow import raw, edit, calendar_results


def test_embedded_voice_settings_and_exact_source_are_untrusted_data(store, monkeypatch):
    task = store(source_type="chat", source_locator=json.dumps({
        "kind": "teams_chat", "conversation_id": "19:synthetic@thread.v2",
        "source": "captured",
    }), user_notes="PRIVATE-NOTES")
    provider = runtime()
    provider.read_source.return_value = {
        "complete": True, "items": [{"excerpt": "PRIVATE-SOURCE", "occurred_at": "2026-09-01T12:00:00Z"}],
    }
    monkeypatch.setattr(cowork_runner, "standing_instructions", lambda: "STANDING")
    worker = skills.SkillService(runtime_provider=lambda: provider)
    assert finish(worker, worker.launch(task["id"], "respond-email"))["state"] == "succeeded"
    prompt = provider.execute_ask.call_args.args[0]
    assert "Use the skill " not in prompt and "STANDING" in prompt
    payload = json.loads(prompt.split("\nSOURCE_DATA_JSON:\n")[1])
    assert payload["people"] == PEOPLE and payload["source"]["scope"] == "thread"
    assert "PRIVATE-SOURCE" in prompt and "PRIVATE-NOTES" in prompt
    assert payload["voice"]["email"] and payload["voice"]["teams"]
    assert "untrusted" in prompt
    provider.read_source.assert_called_once()
    provider.read_directory_user_by_email.assert_not_called()


@pytest.mark.parametrize("case", ["empty", "unresolved", "duplicate", "guest", "missing", "uncertain"])
def test_calendar_blocks_without_complete_attendee_selection(store, case):
    people = {
        "empty": [], "unresolved": [{"name": "Unknown", "unresolved": True}],
        "duplicate": PEOPLE * 2, "guest": [{**PEOPLE[0], "user_type": "Guest"}],
        "missing": [{"name": "Unknown"}], "uncertain": [{**PEOPLE[0], "attendance_uncertain": True}],
    }[case]
    task = store(key_people=json.dumps(people))
    provider = runtime()
    worker = skills.SkillService(runtime_provider=lambda: provider)
    assert finish(worker, worker.launch(task["id"], "schedule-meeting"))["state"] == "succeeded"
    assert "no meeting times are suggested" in raw(task["id"])["skill_output"]
    assert json.loads(raw(task["id"])["key_people"]) == people
    provider.execute_calendar.assert_not_called()


@pytest.mark.parametrize("enough", [True, False])
def test_schedule_offset_fits_measured_block_and_no_model(store, monkeypatch, enough):
    from datetime import datetime, timedelta
    monkeypatch.setattr(cowork_runner, "meeting_preferences",
                        lambda: {"default_minutes": 25, "start_offset_minutes": 5})
    task = store()
    provider = runtime()
    measured, hours = calendar_results()
    if enough:
        for slot in measured["meetingTimeSuggestions"]:
            slot["meetingTimeSlot"]["end"]["dateTime"] = (
                datetime.fromisoformat(slot["meetingTimeSlot"]["end"]["dateTime"]) + timedelta(minutes=5)
            ).isoformat()
    provider.execute_calendar.side_effect = [measured, hours]
    worker = skills.SkillService(runtime_provider=lambda: provider)
    assert finish(worker, worker.launch(task["id"], "schedule-meeting"))["state"] == "succeeded"
    output = raw(task["id"])["skill_output"]
    assert ("9:05 AM" in output) is enough
    assert ("no meeting times are suggested" in output) is not enough
    provider.execute_ask.assert_not_called()


def test_cowork_non_schedule_is_native_handoff_with_all_context_no_model(store, monkeypatch):
    monkeypatch.setattr(cowork_runner, "standing_instructions", lambda: "STANDING RULE")
    task = store(action_type="prepare", user_notes="Reschedule the existing review for 45 minutes",
                 coaching_text="Discuss launch risks", related_meeting="Release review", due_date="2030-01-01")
    provider = runtime()
    worker = skills.SkillService(runtime_provider=lambda: provider)
    assert finish(worker, worker.launch(task["id"], "cowork-prompt"))["state"] == "succeeded"
    after = raw(task["id"])
    assert after["action_type"] == "prepare" and after["skill_output"] == "old output"
    for expected in ["Copilot Cowork prompt (copy and paste):", "FindMeetingTimes", "Participants:",
                     "Duration: 45", "Topic:", "Discuss launch risks", "Release review",
                     "2030-01-01", "old output", "STANDING RULE", "Non-negotiable"]:
        assert expected in after["cowork_prompt"]
    provider.execute_ask.assert_not_called()
    provider.execute_calendar.assert_not_called()


def test_cowork_insufficient_people_retains_history(store):
    task = store(key_people="[]")
    before = raw(task["id"])
    worker = skills.SkillService(runtime_provider=runtime)
    assert finish(worker, worker.launch(task["id"], "cowork-prompt"))["state"] == "failed"
    assert raw(task["id"]) == before


@pytest.mark.parametrize("hint", ["45 min", "1 hour"])
def test_cowork_preserves_prior_output_duration_hint(store, hint):
    task = store()
    edit(task["id"], skill_output=f"Duration: {hint}")
    worker = skills.SkillService(runtime_provider=runtime)
    assert finish(worker, worker.launch(task["id"], "cowork-prompt"))["state"] == "succeeded"
    assert f'Duration: {45 if hint == "45 min" else 60} minutes' in raw(task["id"])["cowork_prompt"]


@pytest.mark.parametrize("case", ["missing", "unsupported", "incomplete", "error"])
def test_source_failure_has_no_broad_fallback_or_output_write(store, case):
    locator = json.dumps({"kind": "teams_chat", "conversation_id": "19:fake@thread.v2", "source": "captured"})
    task = store(source_type="chat", source_locator=locator if case not in {"missing", "unsupported"} else None,
                 source_url="https://example.test/unsupported" if case == "unsupported" else None)
    before = raw(task["id"])
    provider = runtime()
    provider.read_source.side_effect = RuntimeError("PRIVATE") if case == "error" else None
    provider.read_source.return_value = {"complete": False, "items": []}
    worker = skills.SkillService(runtime_provider=lambda: provider)
    assert finish(worker, worker.launch(task["id"], "prepare"))["state"] == "failed"
    assert raw(task["id"]) == before
    provider.execute_ask.assert_not_called()
    provider.recover_chat.assert_not_called()


def test_request_budget_decreases_across_source_model_and_commit(store):
    task = store(source_type="chat", source_locator=json.dumps({
        "kind": "teams_chat", "conversation_id": "19:fake@thread.v2", "source": "captured",
    }))
    provider = runtime()
    now = [0.]
    def source(*args, timeout):
        assert timeout == 420
        now[0] = 50.
        return {"complete": True, "items": []}
    original = provider.execute_ask.side_effect
    def ask(prompt, *, timeout):
        assert timeout == 370
        now[0] = 419.
        return original(prompt, timeout=timeout)
    provider.read_source.side_effect = source
    provider.execute_ask.side_effect = ask
    worker = skills.SkillService(runtime_provider=lambda: provider, monotonic=lambda: now[0])
    assert finish(worker, worker.launch(task["id"], "prepare"))["state"] == "succeeded"


@pytest.mark.parametrize("default", [None, 50])
def test_cowork_default_and_configured_duration_without_hints(store, monkeypatch, default):
    monkeypatch.setattr(cowork_runner, "meeting_preferences",
                        lambda: {"default_minutes": default} if default else {})
    task = store()
    worker = skills.SkillService(runtime_provider=runtime)
    assert finish(worker, worker.launch(task["id"], "cowork-prompt"))["state"] == "succeeded"
    assert f'Duration: {default or 25} minutes' in raw(task["id"])["cowork_prompt"]
