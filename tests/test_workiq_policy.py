import pytest

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


def test_policy_does_not_require_or_expose_ask():
    tools = [
        {"name": "retrieve"},
        {"name": "fetch"},
        {"name": "ask"},
        {"name": "accept_eula"},
    ]
    tools.append({"name": "do_action"})
    assert discover_read_capabilities(tools) == ("do_action",)


@pytest.mark.parametrize(
    "tools",
    [
        [],
        [{"name": "do_action"}, {"name": "do_action"}],
        [{"name": 7}],
        [{"name": "ask_work_iq"}],
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
