"""Synthetic schema oracles, not recorded model answers or production goldens."""

import json

import pytest

from src.services import parsing


def result(mode="full", **changes):
    value = {
        "version": 1, "mode": mode,
        "coaching_text": "Review the synthetic release checklist.",
        "answers": [],
    }
    if mode == "full":
        value.update(
            title="Review release checklist", description="Check the release.",
            priority=3, due_date=None, related_meeting=None,
            action_type="general", is_quick_hit=False,
            work_kind="other", skill_output=None,
        )
    value.update(changes)
    return json.dumps(value)


@pytest.mark.parametrize("mode", ["full", "coaching_only"])
def test_closed_versioned_result(mode):
    assert parsing.validate_result(result(mode), mode, [])["mode"] == mode


@pytest.mark.parametrize("bad", [
    "null", "[]", "{}", "", "```json\n{}\n```", '{"version":1,',
    result(version=True), result(version=2), result(mode="other"),
    result(title=""), result(coaching_text=None), result(answers=None),
    result(priority=True), result(priority=0), result(priority=6),
    result(priority="3"), result(priority=None), result(due_date="tomorrow"),
    result(due_date="2026-02-30"), result(is_quick_hit=1),
    result(action_type="send"), result(work_kind="anything"),
    result().replace('"version": 1', '"version": 1, "version": 1'),
])
def test_invalid_results_fail_closed(bad):
    with pytest.raises(ValueError):
        parsing.validate_result(bad, "full", [])


@pytest.mark.parametrize("field", [
    "key_people", "person_id", "conversation_id", "source_id", "source_url",
    "source_type", "source_snippet", "user_notes", "status", "parse_status",
    "updated_at", "waiting_activity", "cowork_prompt", "cowork_revision",
])
def test_model_fields_never_become_mutations(field):
    with pytest.raises(ValueError):
        parsing.validate_result(result(**{field: "PRIVATE"}), "full", [])


@pytest.mark.parametrize("field,value", [
    ("title", "Changed"), ("priority", 1), ("skill_output", None),
    ("key_people", []), ("action_type", "general"),
])
def test_coaching_schema_cannot_replace_structured_fields(field, value):
    with pytest.raises(ValueError):
        parsing.validate_result(result("coaching_only", **{field: value}), "coaching_only", [])


@pytest.mark.parametrize("action", sorted(parsing.ACTION_TYPES))
def test_all_eight_action_types(action):
    skill = None if action in {"general", "review-document"} else {
        "blocked": "Insufficient verified context."
    }
    assert parsing.validate_result(
        result(action_type=action, skill_output=skill), "full", []
    )["action_type"] == action


@pytest.mark.parametrize("work", ["research", "preparation", "coordination", "review", "scheduling"])
def test_quick_hit_exclusions(work):
    with pytest.raises(ValueError):
        parsing.validate_result(result(is_quick_hit=True, work_kind=work), "full", [])


def test_questions_are_exact_bounded_line_ids():
    notes = "private context\n@WorkIQ same?\n@WorkIQ same?\n@WorkIQ done?\n  → already"
    questions = parsing.questions(notes)
    assert [q["question_id"] for q in questions] == [1, 2]
    answers = [{"question_id": i, "answer": f"Answer {i}"} for i in [1, 2]]
    parsing.validate_result(result(answers=answers), "full", questions)
    assert len(parsing.questions("\n".join("@workiq Q?" for _ in range(21)))) == 20
    for bad in [answers[:1], answers + [answers[0]], [{"question_id": 9, "answer": "No"}],
                [{"question_id": 1, "answer": "Bad\nline"}, answers[1]],
                [{"question_id": 1, "answer": "Bad\x00text"}, answers[1]],
                [{"question_id": True, "answer": "No"}, answers[1]],
                [{"question_id": 1, "answer": "x" * 1001}, answers[1]]]:
        with pytest.raises(ValueError):
            parsing.validate_result(result(answers=bad), "full", questions)


@pytest.mark.parametrize("extra", ["confirmed", "unresolved", "person_id", "alternatives", "source_id"])
def test_candidate_hints_have_no_identity_authority(extra):
    hint = {"name": "Taylor Example", "email": None, "upn": None, "aad_object_id": None, extra: True}
    with pytest.raises(ValueError):
        parsing.validate_hints(json.dumps({"version": 1, "hints": [hint]}))


def test_schedule_model_cannot_assert_availability():
    with pytest.raises(ValueError):
        parsing.validate_result(result(
            action_type="schedule-meeting", skill_output={
                "slots": [{"start": "2026-10-01T09:00:00Z", "all_free": True}]
            },
        ), "full", [])


def test_email_inner_format_and_nested_foreign_fields():
    skill = {
        "to": 0, "subject": "Release check", "body": "Can you check the release?",
        "tone": "direct", "key_points": ["Check release"],
    }
    parsing.validate_result(result(action_type="respond-email", skill_output=skill), "full", [])
    with pytest.raises(ValueError):
        parsing.validate_result(result(
            action_type="respond-email", skill_output={**skill, "email": "model@example.test"}
        ), "full", [])


def test_scalar_bounds_and_calendar_dates():
    parsing.validate_result(result(priority=1, due_date="2028-02-29"), "full", [])
    parsing.validate_result(result(priority=5), "full", [])
    for field, limit in [("title", 300), ("description", 12000), ("coaching_text", 12000)]:
        parsing.validate_result(result(**{field: "x" * limit}), "full", [])
        with pytest.raises(ValueError):
            parsing.validate_result(result(**{field: "x" * (limit + 1)}), "full", [])


@pytest.mark.parametrize("skill", [
    {"to": True, "subject": "x", "body": "x", "tone": "x", "key_points": ["x"]},
    {"to": 0, "subject": "x", "body": "x", "tone": "x", "key_points": []},
    {"to": 0, "subject": "x", "body": "x", "tone": "x", "key_points": ["x"] * 21},
])
def test_skill_recipient_and_list_bounds(skill):
    with pytest.raises(ValueError):
        parsing.validate_result(result(action_type="respond-email", skill_output=skill), "full", [])


def test_question_answer_exact_maximum_and_control_rejection():
    requested = [{"question_id": i, "question": "Q"} for i in range(20)]
    answers = [{"question_id": i, "answer": "x" * 1000} for i in range(20)]
    parsing.validate_result(result(answers=answers), "full", requested)
    answers[0]["answer"] = "x\x7f"
    with pytest.raises(ValueError):
        parsing.validate_result(result(answers=answers), "full", requested)
