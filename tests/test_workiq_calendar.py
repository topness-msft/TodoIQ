from unittest.mock import Mock, patch

import pytest

from src.services.workiq_calendar import find_meeting_times, get_schedule
from src.services.workiq_policy import CapabilityError
from src.services.workiq_runtime import NotReadyError


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
    ("call", "body", "expected"),
    [
        (find_meeting_times, FIND_BODY, {"meetingTimeSuggestions": []}),
        (get_schedule, SCHEDULE_BODY, {"value": []}),
    ],
)
def test_calendar_facade_returns_only_typed_data(call, body, expected):
    runtime = Mock()
    runtime.execute_calendar.return_value = expected
    with patch("src.services.workiq_calendar.get_runtime", return_value=runtime):
        assert call(body, timeout=17) == expected
    operation = runtime.execute_calendar.call_args.args[0]
    assert operation.path in {"/me/findMeetingTimes", "/me/calendar/getSchedule"}
    assert operation.body == body
    assert runtime.execute_calendar.call_args.kwargs == {"timeout": 17}


@pytest.mark.parametrize(
    ("call", "body"),
    [
        (find_meeting_times, {}),
        (find_meeting_times, {**FIND_BODY, "attendees": []}),
        (find_meeting_times, {**FIND_BODY, "meetingDuration": 30}),
        (get_schedule, {}),
        (get_schedule, {**SCHEDULE_BODY, "schedules": []}),
        (get_schedule, {**SCHEDULE_BODY, "startTime": None}),
    ],
)
def test_calendar_facade_rejects_invalid_body_before_transport(call, body):
    runtime = Mock()
    with patch("src.services.workiq_calendar.get_runtime", return_value=runtime):
        with pytest.raises(CapabilityError):
            call(body, timeout=17)
    runtime.execute_calendar.assert_not_called()


def test_calendar_facade_surfaces_authenticated_readiness_failure():
    runtime = Mock()
    runtime.execute_calendar.side_effect = NotReadyError(
        "Work IQ authenticated readiness is required."
    )
    with patch("src.services.workiq_calendar.get_runtime", return_value=runtime):
        with pytest.raises(NotReadyError):
            find_meeting_times(FIND_BODY, timeout=17)
