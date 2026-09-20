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
RECOVERY_CHATS_URL = "/me/chats?$select=id,topic,chatType,webUrl&$top=50"
RECOVERY_SELF_URL = "/me?$select=id,mail,userPrincipalName"
DIRECTORY_SELECT = "id,displayName,mail,userPrincipalName,userType"
DIRECTORY_SELF_URL = f"/me?$select={DIRECTORY_SELECT}"


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


@dataclass(frozen=True)
class RecoveryReadOperation:
    target_id: str | None
    target_email: str | None
    topic: str | None
    _mint: object = field(repr=False, compare=False)


@dataclass(frozen=True)
class DirectoryLookupOperation:
    kind: str
    query_value: str | None
    _mint: object = field(repr=False, compare=False)


@dataclass(frozen=True)
class SavedTeamsChatOperation:
    source: SourceReadOperation
    _mint: object = field(repr=False, compare=False)


def _directory_aad(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(
        r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}", value,
    ) is None:
        raise CapabilityError("Work IQ directory requires an exact AAD UUID.")
    return value.lower()


def _directory_name(value: object) -> str:
    if (
        not isinstance(value, str) or not value.strip() or len(value) > 256
        or any(unicodedata.category(char).startswith("C") for char in value)
    ):
        raise CapabilityError("Work IQ directory requires a bounded full name.")
    return " ".join(value.split())


def build_directory_operation(kind: str, query_value: object = None) -> DirectoryLookupOperation:
    """Mint exact reads only; callers cannot supply a path, fields, or limits."""
    if kind == "self" and query_value is None:
        value = None
    elif kind == "aad_exact":
        value = _directory_aad(query_value)
    elif kind == "email_exact":
        value = _recovery_email(query_value)
    elif kind == "full_name_candidates":
        value = _directory_name(query_value)
    else:
        raise CapabilityError("Work IQ directory lookup is not allowed.")
    return DirectoryLookupOperation(kind, value, (_MINT, kind, value))


def require_directory_operation(operation: object) -> DirectoryLookupOperation:
    if (
        not isinstance(operation, DirectoryLookupOperation)
        or operation._mint != (_MINT, operation.kind, operation.query_value)
    ):
        raise CapabilityError("Work IQ directory operation was not policy-minted.")
    return build_directory_operation(operation.kind, operation.query_value)


def _directory_path(operation: DirectoryLookupOperation) -> str:
    operation = require_directory_operation(operation)
    if operation.kind == "self":
        return DIRECTORY_SELF_URL
    if operation.kind == "aad_exact":
        return f"/users/{quote(operation.query_value, safe='')}?$select={DIRECTORY_SELECT}"
    literal = operation.query_value.replace("'", "''")
    query = (
        f"mail eq '{literal}' or userPrincipalName eq '{literal}'"
        if operation.kind == "email_exact" else f"displayName eq '{literal}'"
    )
    limit = 2 if operation.kind == "email_exact" else 10
    return f"/users?$filter={quote(query, safe='')}&$select={DIRECTORY_SELECT}&$top={limit}"


def build_saved_teams_operation(locator: object) -> SavedTeamsChatOperation:
    source = build_source_operation(locator)
    if source.kind != "teams_chat":
        raise CapabilityError("Work IQ participants require an exact saved Teams chat.")
    return SavedTeamsChatOperation(source, (_MINT, source))


def require_saved_teams_operation(operation: object) -> SavedTeamsChatOperation:
    if (
        not isinstance(operation, SavedTeamsChatOperation)
        or operation._mint != (_MINT, operation.source)
    ):
        raise CapabilityError("Work IQ saved chat operation was not policy-minted.")
    source = require_source_operation(operation.source)
    return build_saved_teams_operation({
        "kind": source.kind, "source": source.locator_source, **dict(source.identifiers),
    })


def _saved_teams_context_path(operation: SavedTeamsChatOperation) -> str:
    operation = require_saved_teams_operation(operation)
    chat = dict(operation.source.identifiers)["conversation_id"]
    return (
        f"/me/chats/{quote(chat, safe='')}/messages?"
        "$select=id,chatId,createdDateTime,from,body,webUrl&$top=20"
    )


def _recovery_email(value: object) -> str:
    if (
        not isinstance(value, str) or len(value) > 320 or value.startswith("/")
        or re.fullmatch(r"[^@\s<>/\\]+@[^@\s<>/\\]+", value) is None
        or any(unicodedata.category(char).startswith("C") for char in value)
    ):
        raise CapabilityError("Work IQ recovery requires a verified email.")
    return value.lower()


def build_recovery_operation(target: object, *, topic: str | None = None) -> RecoveryReadOperation:
    """Seal only captured person identity and optional exact captured chat topic."""
    if not isinstance(target, dict) or set(target) - {"id", "email"}:
        raise CapabilityError("Work IQ recovery target is invalid.")
    identifier = _source_identifier(target["id"]) if target.get("id") is not None else None
    email = _recovery_email(target["email"]) if target.get("email") is not None else None
    if not identifier and not email:
        raise CapabilityError("Work IQ recovery requires a verified person.")
    if topic is not None and (
        not isinstance(topic, str) or not topic.strip() or len(topic) > 512
        or any(unicodedata.category(char).startswith("C") for char in topic)
    ):
        raise CapabilityError("Work IQ recovery topic is invalid.")
    return RecoveryReadOperation(identifier, email, topic, (_MINT, identifier, email, topic))


def require_recovery_operation(operation: object) -> RecoveryReadOperation:
    if (
        not isinstance(operation, RecoveryReadOperation)
        or operation._mint != (_MINT, operation.target_id, operation.target_email, operation.topic)
    ):
        raise CapabilityError("Work IQ recovery operation was not policy-minted.")
    return build_recovery_operation(
        {"id": operation.target_id, "email": operation.target_email}, topic=operation.topic,
    )


def _recovery_members_path(candidate_id: str) -> str:
    return f"/me/chats/{quote(_source_identifier(candidate_id), safe='')}/members"


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
