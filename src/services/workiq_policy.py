"""Owned allowlist for direct Work IQ MCP reads."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from enum import Enum


ACTION_TOOL = "do_action"
FETCH_TOOL = "fetch"
SENT_ITEMS_URL = (
    "/me/mailFolders/sentitems/messages?$top=20&$select="
    "id,internetMessageHeaders,toRecipients&$orderby=sentDateTime desc"
)
_DURATION_RE = re.compile(r"^PT[1-9]\d*M$")
_MINT = object()


class CapabilityError(ValueError):
    code = "capability_denied"


class CalendarAction(str, Enum):
    FIND_MEETING_TIMES = "/me/findMeetingTimes"
    GET_SCHEDULE = "/me/calendar/getSchedule"


@dataclass(frozen=True)
class CalendarOperation:
    path: str
    body: dict
    _mint: object = field(repr=False, compare=False)


def discover_read_capabilities(tools: object) -> tuple[str, ...]:
    """Return only the exact capabilities required by the owned read plans."""
    if not isinstance(tools, list):
        raise CapabilityError("Work IQ returned an invalid capability list.")
    names = []
    for tool in tools:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
            raise CapabilityError("Work IQ returned a malformed capability.")
        names.append(tool["name"])
    if len(names) != len(set(names)):
        raise CapabilityError("Work IQ returned duplicate capabilities.")
    if ACTION_TOOL not in names:
        raise CapabilityError("Work IQ does not advertise the required read capabilities.")
    return tuple(
        name for name in (ACTION_TOOL, FETCH_TOOL) if name in names
    )


def _datetime(value: object) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("dateTime"), str)
        and bool(value["dateTime"].strip())
        and isinstance(value.get("timeZone"), str)
        and bool(value["timeZone"].strip())
    )


def _valid_find_body(body: object) -> bool:
    if not isinstance(body, dict):
        return False
    if set(body) != {
        "attendees",
        "timeConstraint",
        "meetingDuration",
        "maxCandidates",
        "returnSuggestionReasons",
        "minimumAttendeePercentage",
    }:
        return False
    attendees = body.get("attendees")
    if not isinstance(attendees, list) or not attendees:
        return False
    for attendee in attendees:
        address = attendee.get("emailAddress") if isinstance(attendee, dict) else None
        if (
            attendee.get("type") != "required"
            or not isinstance(address, dict)
            or not isinstance(address.get("address"), str)
            or not address["address"].strip()
        ):
            return False
    addresses = [
        attendee["emailAddress"]["address"].strip().lower()
        for attendee in attendees
    ]
    if len(set(addresses)) != len(addresses):
        return False
    constraint = body.get("timeConstraint")
    slots = constraint.get("timeSlots") if isinstance(constraint, dict) else None
    if (
        constraint.get("activityDomain") not in {"work", "unrestricted"}
        or not isinstance(slots, list)
        or len(slots) != 1
        or not isinstance(slots[0], dict)
        or not _datetime(slots[0].get("start"))
        or not _datetime(slots[0].get("end"))
        or slots[0]["start"]["timeZone"] != slots[0]["end"]["timeZone"]
        or slots[0]["start"]["dateTime"] >= slots[0]["end"]["dateTime"]
    ):
        return False
    percentage = body.get("minimumAttendeePercentage")
    return (
        isinstance(body.get("meetingDuration"), str)
        and _DURATION_RE.fullmatch(body["meetingDuration"]) is not None
        and isinstance(body.get("maxCandidates"), int)
        and 1 <= body["maxCandidates"] <= 100
        and body.get("returnSuggestionReasons") is True
        and isinstance(percentage, int)
        and not isinstance(percentage, bool)
        and 1 <= percentage <= 100
    )


def _valid_schedule_body(body: object) -> bool:
    if not isinstance(body, dict):
        return False
    if set(body) != {"schedules", "startTime", "endTime"}:
        return False
    schedules = body.get("schedules")
    return (
        isinstance(schedules, list)
        and bool(schedules)
        and all(isinstance(item, str) and item.strip() for item in schedules)
        and len({item.strip().lower() for item in schedules}) == len(schedules)
        and _datetime(body.get("startTime"))
        and _datetime(body.get("endTime"))
        and body["startTime"]["timeZone"] == body["endTime"]["timeZone"]
        and body["startTime"]["dateTime"] < body["endTime"]["dateTime"]
    )


def build_calendar_operation(action: CalendarAction | object, body: object) -> CalendarOperation:
    """Mint one of the only two representable calendar read operations."""
    if not isinstance(action, CalendarAction):
        raise CapabilityError("Work IQ calendar action is not allowed.")
    valid = (
        _valid_find_body(body)
        if action is CalendarAction.FIND_MEETING_TIMES
        else _valid_schedule_body(body)
    )
    if not valid:
        raise CapabilityError("Work IQ calendar request body is invalid.")
    return CalendarOperation(action.value, copy.deepcopy(body), _MINT)


def require_calendar_operation(operation: object) -> CalendarOperation:
    if not isinstance(operation, CalendarOperation) or operation._mint is not _MINT:
        raise CapabilityError("Work IQ calendar operation was not policy-minted.")
    action = CalendarAction(operation.path)
    return build_calendar_operation(action, operation.body)
