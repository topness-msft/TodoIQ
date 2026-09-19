from dataclasses import FrozenInstanceError, fields

import pytest

from src.services import workiq_policy as policy

from src.services.workiq_policy import (
    CalendarAction,
    CapabilityError,
    build_calendar_operation,
    discover_read_capabilities,
)
from src.services.workiq_setup import load_runtime_manifest


def test_policy_allows_only_exact_runtime_discovered_action_tool():
    tools = [
        {"name": "ask_work_iq"},
        {"name": "accept_eula"},
        {"name": "create_message", "annotations": {"readOnlyHint": True}},
        {"name": "workiq-ask"},
        {"name": "ASK_WORK_IQ"},
    ]
    tools.append({"name": "do_action"})
    assert discover_read_capabilities(tools) == ("do_action",)


def test_policy_discovers_actual_ask_without_the_stale_alias():
    tools = [
        {"name": "retrieve"},
        {"name": "fetch"},
        {"name": "ask"},
        {"name": "accept_eula"},
    ]
    tools.append({"name": "do_action"})
    assert discover_read_capabilities(tools) == ("do_action", "fetch", "ask")


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        (["ask"], ("ask",)),
        (["fetch"], ("fetch",)),
        (["ask", "fetch", "do_action"], ("do_action", "fetch", "ask")),
    ],
)
def test_policy_discovers_independent_owned_reads(names, expected):
    assert discover_read_capabilities([{"name": name} for name in names]) == expected


def test_ask_policy_mints_frozen_question_only_operation():
    question = "  private-question@example.test  "
    operation = policy.build_ask_operation(question)
    assert operation.question == question
    assert {field.name for field in fields(operation)} == {"question", "_mint"}
    assert policy.require_ask_operation(operation).question == question
    with pytest.raises(FrozenInstanceError):
        operation.question = "replacement"
    with pytest.raises(CapabilityError):
        policy.require_ask_operation(policy.AskOperation(question, object()))
    with pytest.raises(CapabilityError):
        policy.require_ask_operation({"question": question})
    with pytest.raises(TypeError):
        policy.build_ask_operation(question, fileUrls=["private"])
    with pytest.raises(TypeError):
        policy.build_ask_operation()


@pytest.mark.parametrize("question", [None, "", " \n ", 7, {}, [], True])
def test_ask_policy_rejects_invalid_questions(question):
    with pytest.raises(CapabilityError):
        policy.build_ask_operation(question)


@pytest.mark.parametrize(
    "tools",
    [
        [],
        [{"name": "do_action"}, {"name": "do_action"}],
        [{"name": 7}],
        [{"name": "ask_work_iq"}],
        [{"name": "ask"}, {"name": "ask"}],
        [{"name": "ask"}, None],
        {},
    ],
)
def test_policy_fails_closed_without_one_valid_action_tool(tools):
    with pytest.raises(CapabilityError):
        discover_read_capabilities(tools)


@pytest.mark.parametrize(
    ("action", "body", "path"),
    [
        (
            CalendarAction.FIND_MEETING_TIMES,
            {
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
            },
            "/me/findMeetingTimes",
        ),
        (
            CalendarAction.GET_SCHEDULE,
            {
                "schedules": ["ada@example.com"],
                "startTime": {
                    "dateTime": "2026-09-08T09:00:00",
                    "timeZone": "UTC",
                },
                "endTime": {
                    "dateTime": "2026-09-08T17:00:00",
                    "timeZone": "UTC",
                },
            },
            "/me/calendar/getSchedule",
        ),
    ],
)
def test_calendar_policy_emits_only_two_exact_actions(action, body, path):
    operation = build_calendar_operation(action, body)
    assert operation.path == path
    assert operation.body == body


@pytest.mark.parametrize(
    "path",
    [
        "/me/sendMail",
        "/me/events",
        "/ME/findMeetingTimes",
        "/me/findMeetingTimes?x=1",
        "/me/findMeetingTimes#x",
        "/me/../findMeetingTimes",
        "prefix/me/findMeetingTimes",
        "/me/findMeetingTimes/suffix",
        "",
        None,
        7,
    ],
)
def test_calendar_policy_rejects_every_other_path(path):
    with pytest.raises(CapabilityError):
        build_calendar_operation(path, {})
