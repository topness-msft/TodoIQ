"""Owned allowlist for direct Work IQ MCP reads."""

from __future__ import annotations

import copy
import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import quote


ACTION_TOOL = "do_action"
FETCH_TOOL = "fetch"
ASK_TOOL = "ask"
SENT_ITEMS_URL = (
    "/me/mailFolders/sentitems/messages?$top=20&$select="
    "id,internetMessageHeaders,toRecipients&$orderby=sentDateTime desc"
)
_DURATION_RE = re.compile(r"^PT[1-9]\d*M$")
_MINT = object()
SOURCE_ID_LIMIT = 2048
SOURCE_EMAIL_SELECT = (
    "id,subject,conversationId,internetMessageId,receivedDateTime,from,bodyPreview,webLink"
)


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


@dataclass(frozen=True)
class AskOperation:
    question: str
    _mint: object = field(repr=False, compare=False)


@dataclass(frozen=True)
class SourceReadOperation:
    kind: str
    identifiers: tuple[tuple[str, str], ...]
    locator_source: str
    _mint: object = field(repr=False, compare=False)


def _source_identifier(value: object) -> str:
    if (
        not isinstance(value, str) or not value or len(value) > SOURCE_ID_LIMIT
        or value != value.strip()
        or any(unicodedata.category(char).startswith("C") for char in value)
        or any(char in value for char in ("?", "#", "%", "\\"))
        or value.startswith("/") or "://" in value
        or any(segment in {".", ".."} for segment in value.split("/"))
    ):
        raise CapabilityError("Work IQ source identifier is invalid.")
    return value


def build_source_operation(locator: object) -> SourceReadOperation:
    """Seal a task snapshot's resolved locator, never a dedup key or read-plan URL."""
    keys = ("conversation_id", "message_id", "team_id", "channel_id", "event_id")
    if (
        not isinstance(locator, dict)
        or set(locator) - {*keys, "kind", "source", "version", "internet_message_id"}
        or type(locator.get("version", 1)) is not int
        or locator.get("version", 1) != 1
        or locator.get("source") not in {"captured", "derived_from_url"}
    ):
        raise CapabilityError("Work IQ requires a resolved source locator.")
    kind = locator.get("kind")
    allowed = {
        "teams_chat": {"conversation_id", "message_id"},
        "teams_channel": {"team_id", "channel_id", "message_id"},
        "email": {"message_id"},
        "meeting": {"conversation_id", "message_id", "event_id"},
    }
    if not isinstance(kind, str) or kind not in allowed:
        raise CapabilityError("Work IQ source kind is not allowed.")
    ids = {}
    for key in keys:
        value = locator.get(key)
        if value is not None:
            if key not in allowed[kind]:
                raise CapabilityError("Work IQ source locator contains mixed identities.")
            ids[key] = _source_identifier(value)
    if locator.get("internet_message_id") is not None:
        if kind != "email":
            raise CapabilityError("Work IQ source locator contains mixed identities.")
        _source_identifier(locator["internet_message_id"])
    required = {
        "teams_chat": {"conversation_id"},
        "teams_channel": {"team_id", "channel_id", "message_id"},
        "email": {"message_id"},
        "meeting": {"conversation_id"} if ids.get("conversation_id") else {"event_id"},
    }
    if not required[kind].issubset(ids):
        raise CapabilityError("Work IQ source locator is incomplete.")
    identifiers = tuple(ids.items())
    provenance = locator["source"]
    # Bind the mint to its values: dataclasses.replace cannot reuse it for new IDs.
    return SourceReadOperation(kind, identifiers, provenance, (_MINT, kind, identifiers, provenance))


def require_source_operation(operation: object) -> SourceReadOperation:
    if (
        not isinstance(operation, SourceReadOperation)
        or operation._mint != (_MINT, operation.kind, operation.identifiers, operation.locator_source)
    ):
        raise CapabilityError("Work IQ source operation was not policy-minted.")
    return build_source_operation({
        "kind": operation.kind, "source": operation.locator_source, **dict(operation.identifiers),
    })


def _source_chat_path(conversation_id: str) -> str:
    return f"/me/chats/{quote(_source_identifier(conversation_id), safe='')}/messages?$top=50"


def _source_initial_path(operation: SourceReadOperation) -> str:
    operation = require_source_operation(operation)
    ids = dict(operation.identifiers)
    encoded = {key: quote(value, safe="") for key, value in ids.items()}
    if operation.kind in {"teams_chat", "meeting"} and ids.get("conversation_id"):
        return _source_chat_path(ids["conversation_id"])
    if operation.kind == "teams_channel":
        return (
            f"/teams/{encoded['team_id']}/channels/{encoded['channel_id']}"
            f"/messages/{encoded['message_id']}/replies?$top=50"
        )
    if operation.kind == "email":
        return f"/me/messages/{encoded['message_id']}?$select={SOURCE_EMAIL_SELECT}"
    return f"/me/events/{encoded['event_id']}?$select=id,subject,start,end,organizer,onlineMeeting"


def _source_email_conversation_path(conversation_id: str) -> str:
    literal = _source_identifier(conversation_id).replace("'", "''")
    query = quote(f"conversationId eq '{literal}'", safe="")
    return f"/me/messages?$filter={query}&$select={SOURCE_EMAIL_SELECT}&$top=25"


def build_ask_operation(question: object) -> AskOperation:
    """Mint a question-only read; optional provider arguments are not exposed."""
    if not isinstance(question, str) or not question.strip():
        raise CapabilityError("Work IQ ask requires a nonblank question.")
    return AskOperation(question, _MINT)


def require_ask_operation(operation: object) -> AskOperation:
    if not isinstance(operation, AskOperation) or operation._mint is not _MINT:
        raise CapabilityError("Work IQ ask operation was not policy-minted.")
    return build_ask_operation(operation.question)


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
    allowed = tuple(
        name for name in (ACTION_TOOL, FETCH_TOOL, ASK_TOOL) if name in names
    )
    if not allowed:
        raise CapabilityError("Work IQ does not advertise the required read capabilities.")
    return allowed


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
