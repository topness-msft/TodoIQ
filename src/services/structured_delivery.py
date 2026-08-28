"""Direct, structured WorkIQ delivery for calendar, email, and Teams actions."""

from __future__ import annotations

import html
import json
import logging
import os
import subprocess
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dateutil import tz

from src.models import get_task, update_task_action
from src.services.calendar_time import (
    calendar_event_duration_minutes,
    calendar_event_is_future,
    named_timezone_matches,
)
from src.services.runtime_mode import external_integrations_enabled
from src.services import source_locator


logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULT_START = "<<<RIVETER_RESULT>>>"
RESULT_END = "<<<END_RIVETER_RESULT>>>"
PREVIEW_TOOLS = "workiq-ask,workiq-retrieve,workiq-fetch"
EXECUTE_TOOLS = {
    "calendar": "workiq-create_entity",
    "email": "workiq-do_action",
    "teams": "workiq-create_entity",
}
STRUCTURED_CHANNELS = frozenset(EXECUTE_TOOLS)
_threads: dict[str, threading.Thread] = {}
_threads_lock = threading.Lock()


def channel_for_task(task: dict) -> str | None:
    """Return the dedicated delivery channel for a task, if one applies."""
    action_type = (task.get("action_type") or "").strip().lower()
    source_type = (task.get("source_type") or "").strip().lower()
    if action_type == "schedule-meeting":
        return "calendar"
    if action_type == "respond-email":
        return "email"
    # A typed "message X on Teams" never originates in a Teams thread, so
    # keying only on source_type sent those tasks to Cowork, which returned no
    # delivery evidence. The action type says what the user asked for; the
    # source says where it came from. Either is enough to know the channel.
    if action_type == "teams-message":
        return "teams"
    if action_type in {"follow-up", "awaiting-response"}:
        if source_type in {"teams", "chat", "teams_chat", "teams-channel"}:
            return "teams"
        # ...and a pasted Teams link is the same statement in a third form.
        # Task 2521 was a follow-up whose source_url resolved to a real
        # one-to-one conversation, yet source_type was "manual" - as it is for
        # anything typed by hand - so it fell through to Cowork, whose
        # direct-action path is off by default. Pressing Send returned 409 and
        # the message could not be delivered at all. source_type records where
        # the task came FROM; a resolved conversation says where it is GOING,
        # which is what routing is actually asking.
        resolved = task.get("source_locator_resolved")
        stored_locator = (
            resolved.get("locator")
            if isinstance(resolved, dict)
            else task.get("source_locator")
        )
        located = source_locator.resolve(
            stored_locator, task.get("source_url")
        )
        if located and located["kind"] in (
            source_locator.KIND_TEAMS_CHAT,
            source_locator.KIND_TEAMS_CHANNEL,
        ):
            return "teams"
        # A meeting-details URL is not itself a reply target, but its event id
        # resolves to the meeting chat and gives WorkIQ real Teams context.
        # The structured preview still resolves the actual destination from
        # the task and confirmed people; it must not assume the meeting chat is
        # where a person-directed follow-up should be sent.
        if (
            located
            and located["kind"] == source_locator.KIND_MEETING
            and source_locator.is_thread_readable(located)
        ):
            return "teams"
    return None


def preview_command(prompt: str) -> list[str]:
    """Build a subprocess command whose model can see only read-only WorkIQ tools."""
    return [
        "copilot",
        "-p",
        prompt,
        f"--available-tools={PREVIEW_TOOLS}",
        "--allow-tool=workiq",
        "--no-ask-user",
    ]


def execute_command(prompt: str, channel: str, recover: bool = False) -> list[str]:
    """Build a subprocess command with one channel-specific write primitive."""
    tool = EXECUTE_TOOLS.get(channel)
    if not tool:
        raise ValueError(f"Unsupported structured delivery channel: {channel}")
    tools = [tool]
    if channel == "email" or (channel == "teams" and recover):
        # /sendMail and /reply return 202 with no body, so the only honest way
        # to produce a delivery reference is to read the sent copy back. Teams
        # needs the same read, but only when recovering, so an ordinary post
        # keeps the tightest possible surface. Either way this adds a READ tool,
        # never a second write primitive.
        tools.append("workiq-fetch")
    return [
        "copilot",
        "-p",
        prompt,
        f"--available-tools={','.join(tools)}",
        "--allow-tool=workiq",
        "--no-ask-user",
    ]


def parse_result_marker(
    output: str,
    *,
    correlation_id: str,
    phase: str,
    require_delivery_ref: bool | None = None,
) -> dict:
    """Parse exactly one correlated result marker, rejecting ambiguous output."""
    if output is None:
        # Captured output can be lost without the child failing (a decode error
        # in the reader thread still yields returncode 0). Absent output is
        # unreadable, never "nothing happened", so fail closed and stay silent
        # about whether a write landed.
        raise ValueError("Structured worker produced no readable output")
    blocks: list[str] = []
    offset = 0
    while True:
        start = output.find(RESULT_START, offset)
        if start < 0:
            break
        end = output.find(RESULT_END, start + len(RESULT_START))
        if end < 0:
            raise ValueError("Structured result marker is incomplete")
        blocks.append(output[start + len(RESULT_START):end].strip())
        offset = end + len(RESULT_END)
    if len(blocks) != 1:
        raise ValueError("Expected exactly one structured result marker")
    try:
        result = json.loads(blocks[0])
    except json.JSONDecodeError as exc:
        raise ValueError("Structured result marker is not valid JSON") from exc
    if not isinstance(result, dict):
        raise ValueError("Structured result must be a JSON object")
    if result.get("correlation_id") != correlation_id:
        raise ValueError("Structured result correlation does not match")
    if result.get("phase") != phase:
        raise ValueError("Structured result phase does not match")
    if result.get("ok") is not True:
        raise ValueError(str(result.get("error") or "Structured operation failed"))
    if require_delivery_ref is None:
        require_delivery_ref = phase == "execute"
    if require_delivery_ref and not str(result.get("delivery_ref") or "").strip():
        raise ValueError("Structured execution did not return delivery evidence")
    return result


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=True)


def _task_snapshot(task: dict) -> dict:
    snapshot = {
        key: task.get(key)
        for key in (
            "id",
            "title",
            "description",
            "action_type",
            "source_type",
            "source_url",
            "key_people",
            "due_date",
            "user_notes",
            "coaching_text",
        )
    }
    resolved = task.get("source_locator_resolved")
    stored_locator = (
        resolved.get("locator")
        if isinstance(resolved, dict)
        else task.get("source_locator")
    )
    located = source_locator.resolve(stored_locator, task.get("source_url"))
    if located:
        snapshot["source_locator"] = located
        snapshot["source_read_plan"] = source_locator.read_plan(located)
    return snapshot


def _key_people(task: dict) -> list[dict]:
    value = task.get("key_people")
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    try:
        parsed = json.loads(value or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def _meeting_duration(task: dict) -> int:
    from src.services.cowork_runner import schedule_duration_minutes

    return schedule_duration_minutes(task)


def initial_payload(task: dict, channel: str) -> dict:
    """Create the immutable request envelope stored before preview starts."""
    if channel not in STRUCTURED_CHANNELS:
        raise ValueError(f"Unsupported structured delivery channel: {channel}")
    return {
        "schema_version": 1,
        "channel": channel,
        "correlation_id": str(uuid.uuid4()),
        "task": _task_snapshot(task),
    }


def _preview_schema(channel: str) -> str:
    if channel == "calendar":
        return (
            '{"schema_version":1,"channel":"calendar","subject":"...",'
            '"body":"...","duration_minutes":25,'
            '"attendees":[{"name":"...","email":"..."}],'
            '"timezone":"named IANA or Windows timezone",'
            '"scheduling_constraints":{"activity_domain":"work or unrestricted",'
            '"search_window":{'
            '"start":"local ISO-8601","end":"local ISO-8601",'
            '"timezone":"same named timezone"}},'
            '"slots":[{"id":"0","label":"...","start":"ISO-8601",'
            '"end":"ISO-8601","timezone":"...",'
            '"availability":{"attendee@example.com":"free"}}]}'
        )
    if channel == "email":
        return (
            '{"schema_version":1,"channel":"email","mode":"reply","message_id":"...",'
            '"to":["user@example.com"],"subject":"...","body":"..."}'
        )
    return (
        '{"schema_version":1,"channel":"teams","destination_kind":"chat",'
        '"chat_id":"...","team_id":null,"channel_id":null,"message_id":null,'
        '"destination_display":"...","body":"..."}'
    )


def preview_prompt(task: dict, payload: dict) -> str:
    """Build a read-only prompt that resolves destination IDs and reviewed content."""
    channel = payload["channel"]
    correlation_id = payload["correlation_id"]
    from src.services.cowork_runner import meeting_preferences

    preferences = meeting_preferences() or {}
    configured_minutes = preferences.get("default_minutes")
    default_minutes = int(configured_minutes or 25)
    # `or 0` treated a configured on-the-hour rule as no rule at all, and --
    # worse -- treated a MISSING settings file as a choice of :00. When
    # settings.json was lost in a checkout migration the prompt went on
    # asserting "Start suggestions at :00 or :30" as though Phil had picked
    # it, so his real :05 rule looked forgotten. An unset preference is
    # stated as nothing.
    configured_offset = preferences.get("start_offset_minutes")
    standing_notes = str(preferences.get("notes") or "").strip()
    duration_rule = (
        f"The user's standing meeting duration is {default_minutes} minutes. "
        if configured_minutes
        else f"Default to {default_minutes} minutes. "
    )
    offset_rule = (
        f"Start suggestions at :{int(configured_offset):02d} or "
        f":{(int(configured_offset) + 30) % 60:02d}. "
        if configured_offset is not None
        else ""
    )
    standing_rule = (
        duration_rule
        + f"Use {default_minutes} minutes unless the task explicitly states "
        "another duration. "
        + offset_rule
        + "Suggest 1-3 future mutual-free slots, treating tentative calendar "
        "blocks as available. Do not query calendar availability in this "
        "phase and do not claim the provisional slots are checked; Riveter's "
        "scheduler does that next. Resolve the task's requested date range "
        "into scheduling_constraints.search_window (future, 1 hour to 30 days) "
        "in the declared timezone. Keep 1-3 provisional slots only as an "
        "infrastructure fallback. Set activity_domain to unrestricted when "
        "the task explicitly requests evening or weekend times; otherwise work."
        # Free-text standing instructions. cowork_runner has always rendered
        # these; this path read only duration and offset, so anything Phil
        # wrote here never reached the worker that now books most meetings.
        + (f" {standing_notes}" if standing_notes else "")
        if channel == "calendar"
        else ""
    )
    # cowork_runner carries an explicit "Include the agenda" instruction, added
    # after a real task produced a draft that gave attendees nothing to prepare
    # against. The structured rewrite dropped it and started emitting a single
    # run-on sentence as the invite body, so the guidance is restored here.
    content_rule = (
        "\nWrite `body` as the invite agenda the attendees will actually read:\n"
        "- One short line naming the purpose and the decision to reach.\n"
        "- Then 2-4 agenda lines, each on its own line beginning with '- '.\n"
        "- Ground every line in the task snapshot: its title, description, "
        "notes and suggested next action. Do not invent attendees, commitments, "
        "prior decisions, documents or dates that the snapshot does not "
        "support; write only what the task supports.\n"
        "- Plain text, no markdown headings, no greeting, no sign-off."
        if channel == "calendar"
        else ""
    )
    if channel == "email":
        # A reply is addressed to its thread, so `to` is not a choice: it is a
        # fact about the thread. Recording the address from the task text
        # instead let Riveter approve one recipient and deliver to another.
        content_rule = (
            "\nFor reply mode, `to` must be the addresses the reply will "
            "actually reach - read them from the message being replied to - "
            "not the address written in the task. For new mail, `to` is the "
            "recipient you are addressing. Either way it must match who will "
            "really receive it, because the user approves that list."
        )
    if channel == "teams":        # Calendar and email each carry channel guidance; Teams carried an
        # empty string. Given only "resolve every delivery identifier" and a
        # schema with a chat_id field, a worker reported that chat ids were
        # "not exposed by the available read-only WorkIQ metadata" and gave up
        # (task 2125) -- untrue, since workiq-fetch is allowed and /me/chats
        # returns them. The execute prompt already named these endpoints; the
        # worker that has to find them never saw one.
        content_rule = (
            "\nResolve the destination by reading, not by guessing:\n"
            "- List conversations with workiq-fetch: /me/chats"
            "?$select=id,topic,chatType,webUrl&$top=50\n"
            "- Match a group chat on its topic. For a one-on-one, check who is "
            "in it with /me/chats/{chat_id}/members, since those chats carry "
            "no topic.\n"
            "- For a channel reply, resolve the team and channel with "
            "/me/joinedTeams and /teams/{team_id}/channels, and the parent "
            "message id from that channel's messages.\n"
            "- Set `destination_display` to the chat's topic, or the other "
            "person's name for a one-on-one. It is what the user reads to "
            "recognise where this is going, so a raw id will not do.\n"
            "- If nothing matches, return ok=false naming the conversation you "
            "looked for. Never report a chat id you did not read back."
        )
    # What the user said when they turned down the last attempt. The chooser
    # has always offered a "Need a different option?" box; nothing rendered it
    # into the next prompt, so a steer was collected and then dropped.
    steer = str(payload.get("steer") or "").strip()
    steer_rule = (
        f"\nThe user turned down the previous suggestions and asked: {steer}\n"
        "Honour that ask in what you propose now."
        if steer
        else ""
    )
    # Teams messages and emails drafted here are the same artefacts Cowork
    # drafts, so they get the same standing voice. Without this the voice
    # settings were honoured or ignored depending on which engine routed the
    # task -- and routing keeps moving work onto this one.
    from src.services.cowork_runner import voice_layer

    voice = voice_layer(channel) if channel in {"teams", "email"} else ""
    voice_rule = f"\n\n{voice}" if voice else ""
    # Standing instructions that fit neither the voice nor the meeting block.
    # Every channel, both engines -- meeting_preferences.notes only ever
    # reached calendar prompts, so anything general had nowhere to live.
    from src.services.cowork_runner import standing_instructions

    standing = standing_instructions()
    standing_block = f"\n\n{standing}" if standing else ""
    return f"""
You are Riveter's read-only {channel} preview worker. Use only the visible WorkIQ
read tools. Do not create, update, send, post, or delete anything.

Resolve every delivery identifier now. Do not defer recipient, message, chat,
team, channel, thread, attendee, timezone, or slot resolution until execution.
{standing_rule}{steer_rule}{content_rule}{voice_rule}{standing_block}

Task snapshot:
{_json(_task_snapshot(task))}

Return exactly one result block and no other result blocks:
{RESULT_START}
{{"correlation_id":"{correlation_id}","phase":"preview","ok":true,
"payload":{_preview_schema(channel)}}}
{RESULT_END}

If the preview cannot be safely resolved, return ok=false with a concise error
and omit payload. Never invent an identity or identifier.
""".strip()


def idempotency_key(action: dict) -> str | None:
    """The key Riveter mints so it can recognise its own write afterwards.

    One concept, two transports. Calendar carries it as Graph's native
    `transactionId`, which makes a repeated create return the existing event.
    Email carries it as an `x-riveter-correlation-id` internet message header,
    which survives both /sendMail and /reply and makes the sent copy findable,
    so the delivery reference is looked up rather than invented. Teams has no
    mechanism at all and returns None rather than pretending otherwise.

    The task id is included deliberately: a key found later on a real calendar
    event or in a raw mail header should say which task produced it, not just
    an opaque row id that means nothing outside this database. Note the email
    header travels to recipients, so it must never carry anything but these
    identifiers.
    """
    prefix = {"calendar": "cal", "email": "mail"}.get(
        str(action.get("delivery_channel") or "").strip().lower()
    )
    if not prefix:
        return None
    return f"riveter-{prefix}-t{action.get('task_id')}-a{action.get('id')}"


def plain_text_to_html(text: str | None) -> str:
    """Render an approved plain-text body as HTML without changing it.

    Mail is delivered as HTML, where a newline is only whitespace. Sending the
    approved plain text straight into an HTML body silently collapsed every
    paragraph break (production, task 2124). Riveter approves a specific
    artifact, so Riveter renders the wire format itself rather than leaving the
    choice to the worker. Escaping first means the body can never inject markup.
    """
    if not text:
        return ""
    return html.escape(str(text), quote=False).replace("\n", "<br>\n")


CORRELATION_HEADER = "x-riveter-correlation-id"

# Graph reports free/busy as a digit per interval. Severity order matters more
# than the digits: a slot is only as good as its worst moment.
AVAILABILITY_CODES = {
    "0": "free",
    "1": "tentative",
    "2": "busy",
    "3": "oof",
    "4": "workingElsewhere",
}
AVAILABILITY_SEVERITY = ["free", "workingElsewhere", "tentative", "busy", "oof"]


def _judged_bounds(slot_start: str, slot_end: str):
    """The half-hour a candidate really occupies.

    A 1:05-1:30 meeting is booked at 1:05 but it consumes the 1:00 half-hour:
    calendars are blocked in half-hour units, so anything sitting in 1:00-1:05
    collides with it in practice. Availability is judged over 1:00-1:30 while
    the invite still goes out at 1:05.
    """
    start = _parse_offset_datetime(slot_start)
    end = _parse_offset_datetime(slot_end)
    if not start or not end:
        return None, None
    floored = start.replace(
        minute=0 if start.minute < 30 else 30, second=0, microsecond=0
    )
    return floored, end
# What actually stops someone attending. The standing rule treats a tentative
# block as bookable, and working elsewhere still means working, so neither
# counts against a slot. OOF weighs heavier than busy: a meeting can be moved,
# a holiday cannot.
# What actually stops someone attending, and what is merely worth saying.
# OOF weighs heaviest: a meeting can be moved, a holiday cannot. Soft notes
# carry a small cost so a slot that suits everyone outright still leads, but
# they never make a time unofferable -- the standing rule treats a tentative
# block as bookable, and an ask just past someone's day is the same kind of
# small favour.
CONFLICT_WEIGHTS = {"outsideWorkingHours": 4, "busy": 8, "oof": 16}
SOFT_WEIGHTS = {"tentative": 1, "workingElsewhere": 1, "nearWorkingHours": 1}
# How far past the edge of a working day still counts as a reasonable ask.
WORKING_HOURS_GRACE_MINUTES = 90
AVAILABILITY_PHRASES = {
    "oof": "out of office",
    "busy": "busy",
    "tentative": "tentative",
    "workingElsewhere": "working elsewhere",
    "outsideWorkingHours": "outside working hours",
    "nearWorkingHours": "just outside working hours",
    "unknown": "not checked",
}
AVAILABILITY_INTERVAL_MINUTES = 5
_WEEKDAY_NAMES = [
    "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday",
]


def _working_hours_status(working_hours, slot_start: str, slot_end: str):
    """How far outside the attendee's own working day this slot falls.

    Returns None when it sits inside the day, "nearWorkingHours" when it is
    close enough to be a reasonable ask, and "outsideWorkingHours" beyond that.
    A calendar can be clear at a time nobody would take the meeting, and only
    the attendee's own timezone can say which it is: a 17:05 ET slot is a
    normal afternoon on Central and after hours on Eastern. Missing or
    unreadable data never invents an objection.
    """
    if not isinstance(working_hours, dict):
        return None
    zone_name = str(
        (working_hours.get("timeZone") or {}).get("name") or ""
    ).strip()
    start = _parse_offset_datetime(slot_start)
    end = _parse_offset_datetime(slot_end)
    if not zone_name or not start or not end:
        return None
    from dateutil import tz as _tz

    zone = _tz.gettz(zone_name)
    if zone is None:
        return None

    local_start = start.astimezone(zone)
    local_end = end.astimezone(zone)

    days = {
        str(day).strip().lower()
        for day in (working_hours.get("daysOfWeek") or [])
    }
    if days and _WEEKDAY_NAMES[local_start.weekday()] not in days:
        return "outsideWorkingHours"

    def _clock(value):
        text = str(value or "").strip()
        if not text:
            return None
        parts = text.split(".")[0].split(":")
        try:
            return int(parts[0]) * 60 + int(parts[1])
        except (IndexError, ValueError):
            return None

    day_start = _clock(working_hours.get("startTime"))
    day_end = _clock(working_hours.get("endTime"))
    if day_start is None or day_end is None:
        return None
    begins = local_start.hour * 60 + local_start.minute
    finishes = local_end.hour * 60 + local_end.minute
    # An end exactly on the closing time still counts as inside the day.
    overshoot = max(day_start - begins, finishes - day_end, 0)
    if overshoot <= 0:
        return None
    if overshoot <= WORKING_HOURS_GRACE_MINUTES:
        return "nearWorkingHours"
    return "outsideWorkingHours"


def _outside_working_hours(working_hours, slot_start: str, slot_end: str) -> bool:
    """True only when a slot is beyond a reasonable ask of the attendee."""
    return _working_hours_status(
        working_hours, slot_start, slot_end
    ) == "outsideWorkingHours"
# getSchedule is the one write-shaped call this worker may make, and it writes
# nothing. Riveter mints it; the model never chooses to call it and never sees
# a tool that could create an event or send a message.
AVAILABILITY_TOOLS = "workiq-do_action"
# At ten schedule items per person/day, twelve attendee-days is roughly 120
# items. Task 2478's six attendees across two candidate days sits exactly at
# this boundary and already timed out at 420 seconds, so values AT the boundary
# keep the existing per-day response cap. Smaller sets batch all numbered
# windows into one agent process.
BATCH_THRESHOLD_ATTENDEE_DAYS = 12
# Direct Graph returns in seconds, but the Copilot CLI/MCP startup around the
# call exceeded 90 seconds on task 2478. Keep this below the old 420-second
# preview ceiling while giving the exact, single-action probe room to start.
SCHEDULER_TIMEOUT_SECONDS = 180
GRAPH_SCHEDULE_SOURCE = "FindMeetingTimes+structured"


class NoMutualFreeTime(ValueError):
    """Graph completed the search and found no usable overlap."""


def _scheduler_request(
    task: dict,
    payload: dict,
    minimum_percentage: int,
    *,
    now: datetime | None = None,
) -> tuple[dict, int, int, str]:
    """Mint the exact findMeetingTimes body after validating model output."""
    expected_people = [
        person for person in _key_people(task)
        if str(person.get("email") or "").strip()
    ]
    expected = {
        str(person.get("email") or "").strip().lower()
        for person in expected_people
    }
    payload_people = [
        person for person in payload.get("attendees") or []
        if isinstance(person, dict)
    ]
    actual = {
        str(person.get("email") or "").strip().lower()
        for person in payload_people
        if str(person.get("email") or "").strip()
    }
    if not expected or actual != expected:
        raise ValueError("Calendar attendees changed before scheduler query")

    duration = payload.get("duration_minutes")
    if not isinstance(duration, int) or duration != _meeting_duration(task):
        raise ValueError("Calendar duration changed before scheduler query")

    constraints = payload.get("scheduling_constraints")
    window = (
        constraints.get("search_window")
        if isinstance(constraints, dict)
        else None
    )
    if not isinstance(window, dict):
        raise ValueError("Calendar search window is missing")
    timezone_name = str(
        window.get("timezone") or payload.get("timezone") or ""
    ).strip()
    zone = tz.gettz(timezone_name)
    if zone is None:
        raise ValueError("Calendar search window timezone is invalid")

    def _local(value):
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=zone)
        else:
            parsed = parsed.astimezone(zone)
        if not tz.datetime_exists(parsed) or tz.datetime_ambiguous(parsed):
            raise ValueError("Calendar search window contains an invalid wall time")
        return parsed

    try:
        start = _local(window.get("start"))
        end = _local(window.get("end"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Calendar search window is invalid") from exc
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("Scheduler current time must be timezone-aware")
    if start.astimezone(timezone.utc) <= current.astimezone(timezone.utc):
        start = (
            current.astimezone(zone).replace(second=0, microsecond=0)
            + timedelta(minutes=1)
        )
    span = end.astimezone(timezone.utc) - start.astimezone(timezone.utc)
    if (
        end.astimezone(timezone.utc) <= current.astimezone(timezone.utc)
        or span < timedelta(hours=1)
        or span > timedelta(days=30)
    ):
        raise ValueError("Calendar search window must be future and 1 hour to 30 days")

    from src.services.cowork_runner import meeting_preferences

    preferences = meeting_preferences() or {}
    offset = int(preferences.get("start_offset_minutes") or 0) % 30
    search_duration = duration + offset
    activity_domain = str(
        constraints.get("activity_domain") or "work"
    ).strip().lower()
    if activity_domain not in {"work", "unrestricted"}:
        raise ValueError("Calendar activity domain is invalid")
    body = {
        "attendees": [
            {
                "type": "required",
                "emailAddress": {
                    "name": str(person.get("name") or "").strip(),
                    "address": str(person.get("email") or "").strip(),
                },
            }
            for person in expected_people
        ],
        "timeConstraint": {
            "activityDomain": activity_domain,
            "timeSlots": [{
                "start": {
                    "dateTime": start.replace(tzinfo=None).isoformat(),
                    "timeZone": timezone_name,
                },
                "end": {
                    "dateTime": end.replace(tzinfo=None).isoformat(),
                    "timeZone": timezone_name,
                },
            }],
        },
        "meetingDuration": f"PT{search_duration}M",
        "maxCandidates": 10,
        "returnSuggestionReasons": True,
        "minimumAttendeePercentage": minimum_percentage,
    }
    return body, duration, offset, timezone_name


def _find_times_prompt(body: dict) -> str:
    schedules = [
        str(
            (attendee.get("emailAddress") or {}).get("address") or ""
        ).strip()
        for attendee in body.get("attendees") or []
        if isinstance(attendee, dict)
    ]
    search_start = (
        body["timeConstraint"]["timeSlots"][0]["start"]
    )
    day = str(search_start["dateTime"]).split("T", 1)[0]
    timezone_name = str(search_start["timeZone"])
    timezone_body = {
        "schedules": schedules,
        "startTime": {
            "dateTime": f"{day}T00:00:00",
            "timeZone": timezone_name,
        },
        "endTime": {
            "dateTime": f"{day}T01:00:00",
            "timeZone": timezone_name,
        },
    }
    return f"""
You are Riveter's scheduler probe. First make exactly this read-only action call:
actionUrl: /me/findMeetingTimes
jsonBody: {_json(body)}

If and only if meetingTimeSuggestions is nonempty, make this second read-only
call to collect each attendee's working-hours timezone:
actionUrl: /me/calendar/getSchedule
jsonBody: {_json(timezone_body)}

Do not call any other action and do not interpret or rewrite either response.
From getSchedule copy each scheduleId to workingHours.timeZone.name mapping.
Omit any schedule without that value. Return exactly one result block:
{RESULT_START}
{{"result":{{"emptySuggestionsReason":"","meetingTimeSuggestions":[]}},
"attendeeTimezones":{{"person@example.com":"Eastern Standard Time"}}}}
{RESULT_END}
""".strip()


def _parse_find_times(output: str) -> dict | None:
    text = output or ""
    start = text.find(RESULT_START)
    end = text.find(RESULT_END)
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(text[start + len(RESULT_START):end].strip())
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    result = parsed.get("result")
    # The prompt asks for a wrapper, but "copy the response data verbatim" can
    # reasonably produce the Graph object directly. Accept both rather than
    # silently downgrading every such response to the slower fallback.
    if result is None and isinstance(parsed.get("meetingTimeSuggestions"), list):
        result = parsed
    if not isinstance(result, dict):
        return None
    copied = dict(result)
    copied["_attendeeTimezones"] = parsed.get("attendeeTimezones") or {}
    return copied


def _scheduler_command(body: dict) -> list[str]:
    return availability_command(_find_times_prompt(body))


def _graph_datetime(value: dict) -> datetime | None:
    if not isinstance(value, dict):
        return None
    try:
        parsed = datetime.fromisoformat(
            str(value.get("dateTime") or "").replace("Z", "+00:00")
        )
    except ValueError:
        return None
    zone = tz.gettz(str(value.get("timeZone") or "").strip())
    if zone is None:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=zone)
    return parsed if tz.datetime_exists(parsed) and not tz.datetime_ambiguous(parsed) else None


def _graph_status(value) -> str | None:
    text = str(value or "").strip().lower()
    if text == "workingelsewhere":
        return "workingElsewhere"
    return text if text in AVAILABILITY_SEVERITY else None


def _validated_attendee_timezones(
    value: dict | None,
    attendees: set[str],
) -> dict[str, str]:
    if not value:
        return {}
    if not isinstance(value, dict):
        raise ValueError("Scheduler timezone evidence is invalid")
    normalized = {}
    for raw_email, raw_zone in value.items():
        email = str(raw_email or "").strip().lower()
        timezone_name = str(raw_zone or "").strip()
        if email not in attendees:
            raise ValueError("Scheduler timezone evidence is invalid")
        if tz.gettz(timezone_name) is None:
            # Some Exchange tenants return "Customized Time Zone" without
            # the custom rule definition. Timezone labels are optional display
            # metadata; never invalidate measured availability because this
            # one label cannot be resolved.
            continue
        normalized[email] = timezone_name
    return normalized


def _attendee_timezone_labels(
    attendee_timezones: dict[str, str],
    slots: list[dict],
) -> dict[str, str]:
    """Describe each attendee's offset from the organizer across offered slots."""
    instants = []
    for slot in slots or []:
        try:
            parsed = datetime.fromisoformat(
                str(slot.get("start") or "").replace("Z", "+00:00")
            )
        except ValueError:
            continue
        organizer_zone = tz.gettz(str(slot.get("timezone") or "").strip())
        if (
            parsed.tzinfo is None
            or parsed.utcoffset() is None
            or organizer_zone is None
        ):
            continue
        instant = parsed.astimezone(timezone.utc)
        organizer_offset = instant.astimezone(organizer_zone).utcoffset()
        if organizer_offset is not None:
            instants.append((instant, organizer_offset))
    if not instants:
        return {}

    def _format(hours):
        if hours == 0:
            return "same TZ"
        absolute = abs(hours)
        number = str(int(absolute)) if absolute.is_integer() else str(absolute)
        return f"{'+' if hours > 0 else '-'}{number}h"

    labels = {}
    for email, timezone_name in attendee_timezones.items():
        zone = tz.gettz(timezone_name)
        if zone is None:
            continue
        differences = set()
        for instant, organizer_offset in instants:
            attendee_offset = instant.astimezone(zone).utcoffset()
            if attendee_offset is None:
                continue
            differences.add(
                (attendee_offset - organizer_offset).total_seconds() / 3600
            )
        if differences:
            labels[email] = "/".join(
                _format(hours) for hours in sorted(differences)
            )
    return labels


def _timezones_from_measurements(measurements: list | None) -> dict[str, str]:
    """Extract measured working-hours zones from getSchedule responses."""
    timezones = {}
    for measurement in measurements or []:
        for schedule in (measurement or {}).get("schedules") or []:
            if not isinstance(schedule, dict):
                continue
            email = str(schedule.get("scheduleId") or "").strip().lower()
            timezone_name = str(
                (
                    ((schedule.get("workingHours") or {}).get("timeZone") or {})
                    .get("name")
                )
                or ""
            ).strip()
            if email and tz.gettz(timezone_name) is not None:
                timezones[email] = timezone_name
    return timezones


def _slot_label(start: datetime, end: datetime) -> str:
    def _clock(value):
        hour = value.strftime("%I").lstrip("0") or "0"
        return f"{hour}:{value:%M} {value:%p}"

    zone = start.tzname() or ""
    return (
        f"{start:%A, %B} {start.day}, {start.year}, "
        f"{_clock(start)}-{_clock(end)} {zone}"
    ).strip()


def _slots_from_find_times(
    result: dict,
    attendees: set[str],
    duration: int,
    offset: int,
    timezone_name: str,
) -> list[dict]:
    """Convert measured Graph blocks into exact event slots."""
    zone = tz.gettz(timezone_name)
    if zone is None:
        raise ValueError("Calendar result timezone is invalid")
    slots = []
    for suggestion in result.get("meetingTimeSuggestions") or []:
        if not isinstance(suggestion, dict):
            continue
        organizer = _graph_status(suggestion.get("organizerAvailability"))
        if organizer not in {"free", "tentative", "workingElsewhere"}:
            continue
        availability = {}
        for entry in suggestion.get("attendeeAvailability") or []:
            if not isinstance(entry, dict):
                continue
            attendee = entry.get("attendee") or {}
            address = str(
                (attendee.get("emailAddress") or {}).get("address") or ""
            ).strip().lower()
            status = _graph_status(entry.get("availability"))
            if address and status:
                availability[address] = status
        if set(availability) != attendees:
            continue
        if "oof" in availability.values():
            continue
        if sum(status == "busy" for status in availability.values()) > 1:
            continue
        raw = suggestion.get("meetingTimeSlot") or {}
        graph_start = _graph_datetime(raw.get("start"))
        graph_end = _graph_datetime(raw.get("end"))
        if graph_start is None or graph_end is None:
            continue
        shifted = graph_start.astimezone(zone) + timedelta(minutes=offset)
        event_end = shifted + timedelta(minutes=duration)
        if event_end.astimezone(timezone.utc) > graph_end.astimezone(timezone.utc):
            continue
        start_text = shifted.isoformat()
        end_text = event_end.isoformat()
        slots.append({
            "id": str(len(slots)),
            "label": _slot_label(shifted, event_end),
            "start": start_text,
            "end": end_text,
            "timezone": timezone_name,
            "availability": availability,
            "graph_confidence": suggestion.get("confidence"),
        })
        if len(slots) == 3:
            break
    return slots


def _find_meeting_slots(task: dict, payload: dict) -> tuple[list[dict], dict]:
    """Ask Graph for measured candidate blocks, then enforce Riveter policy."""
    attendee_count = len([
        person for person in payload.get("attendees") or []
        if isinstance(person, dict) and person.get("email")
    ])
    if attendee_count < 1:
        raise ValueError("Calendar attendees are missing")
    percentages = [100]
    if attendee_count > 1:
        percentages.append(int((attendee_count - 1) / attendee_count * 100))
    last_reason = ""
    for percentage in percentages:
        body, duration, offset, timezone_name = _scheduler_request(
            task, payload, percentage
        )
        proc = _run(
            _scheduler_command(body),
            timeout=SCHEDULER_TIMEOUT_SECONDS,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                (proc.stderr or "Scheduler subprocess failed")[-1000:]
            )
        result = _parse_find_times(proc.stdout or "")
        if result is None:
            raise ValueError("Scheduler response was unreadable")
        last_reason = str(result.get("emptySuggestionsReason") or "")
        attendees = {
            str(person.get("email") or "").strip().lower()
            for person in payload.get("attendees") or []
            if isinstance(person, dict) and person.get("email")
        }
        attendee_timezones = _validated_attendee_timezones(
            result.get("_attendeeTimezones"), attendees
        )
        slots = _slots_from_find_times(
            result, attendees, duration, offset, timezone_name
        )
        if slots:
            confidences = [
                float(slot["graph_confidence"])
                for slot in slots
                if isinstance(slot.get("graph_confidence"), (int, float))
            ]
            return slots, {
                "source": GRAPH_SCHEDULE_SOURCE,
                "query_backed": True,
                "availability_verified": True,
                "graph_confidence": min(confidences) if confidences else percentage,
                "graph_suggestion_count": len(slots),
                "graph_minimum_attendee_percentage": percentage,
                "start_offset_minutes": offset,
                "attendee_timezones": attendee_timezones,
                "attendee_timezone_labels": _attendee_timezone_labels(
                    attendee_timezones, slots
                ),
            }
        if result.get("meetingTimeSuggestions"):
            # Graph answered, but the response was incomplete or unsafe (for
            # example an unknown attendee status). This is infrastructure/data
            # quality, not proof that no mutual time exists; use the established
            # getSchedule fallback instead of lying about the outcome.
            raise ValueError("Scheduler suggestion was incomplete or unsafe")
    raise NoMutualFreeTime(
        "No mutual free time was found in the requested window"
        + (f" ({last_reason})." if last_reason else ".")
    )


def availability_prompt(attendees: list, windows: list) -> str:
    """Ask Graph what the calendars say, and ask for the answer unchanged.

    scheduleItems rather than availabilityView: exact instants instead of
    buckets, and a size that follows the number of meetings rather than the
    length of the window. A wide window of availabilityView at five-minute
    resolution ran to thousands of characters the worker could not echo back.
    """
    schedules = _json([str(email).strip() for email in attendees])
    calls = "\n".join(
        f'{index}. actionUrl /me/calendar/getSchedule with body:\n'
        f'   {{"schedules": {schedules},\n'
        f'    "startTime": {{"dateTime": "{start}", "timeZone": "UTC"}},\n'
        f'    "endTime": {{"dateTime": "{end}", "timeZone": "UTC"}}}}'
        for index, (start, end) in enumerate(windows)
    )
    return f"""
You are Riveter's availability probe. Make the calls listed below and return
their results. Do not interpret them, summarise them, or decide whether any
time is suitable - that judgement is not yours to make.

Call workiq-do_action once for each numbered window:
{calls}

Return exactly one result block holding every window's result, keyed by the
number above. For each schedule copy its scheduleId, its workingHours, and
every entry of scheduleItems as status/start/end, verbatim. Ignore
availabilityView entirely.
{RESULT_START}
{{"windows":[{{"index":0,"schedules":[{{"scheduleId":"...",
"scheduleItems":[{{"status":"busy",
"start":{{"dateTime":"2026-08-26T17:00:00.0000000","timeZone":"UTC"}},
"end":{{"dateTime":"2026-08-26T17:30:00.0000000","timeZone":"UTC"}}}}],
"workingHours":{{"daysOfWeek":["monday"],"startTime":"08:00:00",
"endTime":"17:00:00","timeZone":{{"name":"..."}}}}}}]}}]}}
{RESULT_END}

A schedule with no entries has an empty scheduleItems list - that is a real
answer and must still appear. If a call fails, give that window
"schedules":null. If none succeed, return
{RESULT_START}{{"windows":null}}{RESULT_END}.
Never invent a schedule item or a working-hours timezone.
""".strip()


def availability_command(prompt: str) -> list[str]:
    """A subprocess that can ask Graph for free/busy and nothing else."""
    return [
        "copilot",
        "-p",
        prompt,
        f"--available-tools={AVAILABILITY_TOOLS}",
        "--allow-tool=workiq",
        "--no-ask-user",
    ]


def _parse_windows(output: str) -> dict | None:
    """Pull the per-window schedules out of the probe's result block."""
    text = output or ""
    start = text.find(RESULT_START)
    end = text.find(RESULT_END)
    if start < 0 or end < 0 or end <= start:
        return None
    try:
        parsed = json.loads(text[start + len(RESULT_START):end].strip())
    except (json.JSONDecodeError, TypeError):
        return None
    windows = parsed.get("windows") if isinstance(parsed, dict) else None
    if not isinstance(windows, list):
        return None
    measured = {}
    for entry in windows:
        if not isinstance(entry, dict):
            continue
        schedules = entry.get("schedules")
        if not isinstance(schedules, list) or not schedules:
            continue
        try:
            measured[int(entry.get("index"))] = schedules
        except (TypeError, ValueError):
            continue
    return measured or None


def fetch_availability(attendees: list, slots: list) -> list | None:
    """Measure the attendees' calendars across the proposed slots.

    Normal sets use one agent process carrying every numbered day window.
    getSchedule still receives one narrow call per day; batching removes the
    repeated agent startup and session initialization around those calls.
    Larger sets retain the per-day parallel response cap because six attendees
    across two days already timed out in production (task 2478).

    Returns one entry per slot holding the raw schedules, so the caller can do
    its own overlap arithmetic. Returns None when nothing could be measured -
    never a cheerful default.
    """
    if not attendees or not slots:
        return None
    from datetime import timezone

    bounds = []
    for slot in slots:
        start, end = _judged_bounds(slot.get("start"), slot.get("end"))
        if not start or not end:
            return None
        bounds.append((start.astimezone(timezone.utc), end.astimezone(timezone.utc)))

    # One window per day, run concurrently. scheduleItems removed the
    # resolution problem but not the span one: five days across five people is
    # hundreds of meetings, and the worker cannot echo that back. Sequential
    # day-windows then blew the timeout at ~120s each, so they go in parallel
    # and the wait is the slowest window rather than their sum.
    groups: dict = {}
    for index, (start, _end) in enumerate(bounds):
        groups.setdefault(start.date(), []).append(index)

    windows, window_for_slot = [], {}
    for day in sorted(groups):
        members = groups[day]
        for index in members:
            window_for_slot[index] = len(windows)
        windows.append((
            min(bounds[i][0] for i in members).strftime("%Y-%m-%dT%H:%M:%S"),
            max(bounds[i][1] for i in members).strftime("%Y-%m-%dT%H:%M:%S"),
        ))

    def _measure_batch(batch_windows):
        """Measure numbered windows in one process, preserving partial success."""
        expected = set(range(len(batch_windows)))
        expected_attendees = {
            str(email).strip().lower() for email in attendees if str(email).strip()
        }
        measured: dict[int, list] = {}

        def _window_complete(index):
            returned = {
                str(entry.get("scheduleId") or "").strip().lower()
                for entry in measured.get(index, [])
                if isinstance(entry, dict)
            }
            return expected_attendees.issubset(returned)

        # One retry: a window can come back unreadable for reasons that have
        # nothing to do with its size -- a 30-minute window has failed where a
        # five-hour one succeeded. Retry the whole batch, but only back-fill
        # missing schedules: replacing the dict would discard a first-attempt
        # success, while treating any nonempty list as complete would lose
        # attendees returned only on the second attempt.
        for attempt in (1, 2):
            try:
                proc = _run(
                    availability_command(
                        availability_prompt(attendees, batch_windows)
                    ),
                    timeout=200,
                )
            except Exception as exc:  # noqa: BLE001 - never break preview
                logger.warning(
                    "Availability probe failed to run (attempt %s): %s",
                    attempt, exc,
                )
                continue
            parsed = _parse_windows(proc.stdout or "") or {}
            for index, schedules in parsed.items():
                if index not in expected:
                    continue
                by_attendee = {
                    str(entry.get("scheduleId") or "").strip().lower(): entry
                    for entry in measured.get(index, [])
                    if isinstance(entry, dict) and entry.get("scheduleId")
                }
                for entry in schedules:
                    if not isinstance(entry, dict):
                        continue
                    email = str(entry.get("scheduleId") or "").strip().lower()
                    if email and email not in by_attendee:
                        by_attendee[email] = entry
                measured[index] = list(by_attendee.values())
            if all(_window_complete(index) for index in expected):
                return measured
            logger.warning(
                "Availability batch incomplete (attempt %s of 2; %s/%s windows complete)",
                attempt,
                sum(_window_complete(index) for index in expected),
                len(expected),
            )
        return measured

    attendee_days = len(attendees) * len(windows)
    if attendee_days < BATCH_THRESHOLD_ATTENDEE_DAYS:
        batch = _measure_batch(windows)
        results = [batch.get(index) for index in range(len(windows))]
    else:
        from concurrent.futures import ThreadPoolExecutor

        def _measure_window(window):
            return _measure_batch([window]).get(0)

        with ThreadPoolExecutor(max_workers=min(len(windows), 4)) as pool:
            results = list(pool.map(_measure_window, windows))

    if not any(results):
        logger.warning("Availability probe returned no readable schedules")
        return None
    return [
        {"schedules": results[window_for_slot[index]]}
        for index in range(len(slots))
    ]



def _parse_offset_datetime(value: str):
    from datetime import datetime

    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def _availability_window(slots: list) -> tuple[str, str] | None:
    """The single span that covers every proposed slot."""
    starts, ends = [], []
    for slot in slots or []:
        start = _parse_offset_datetime(slot.get("start"))
        end = _parse_offset_datetime(slot.get("end"))
        if not start or not end:
            return None
        starts.append(start)
        ends.append(end)
    if not starts:
        return None
    return min(starts).isoformat(), max(ends).isoformat()


def _status_from_items(items, slot_start: str, slot_end: str) -> str | None:
    """Worst availability across the half-hour a candidate occupies.

    Graph's scheduleItems carry exact instants, so there are no buckets to
    round against and nothing turns on the query's resolution. They are also
    compact: a handful of meetings a day rather than one character per
    interval, which is what made a wide window unreturnable before.

    Riveter does this arithmetic itself. The subprocess that fetches Graph's
    response is a transport; letting it decide whether a slot is free would
    reproduce the very problem this exists to fix.
    """
    if items is None:
        return None
    start, end = _judged_bounds(slot_start, slot_end)
    if not start or not end or end <= start:
        return None

    worst = "free"
    for item in items:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "").strip()
        if status not in AVAILABILITY_SEVERITY:
            continue
        begins = _parse_graph_instant(item.get("start"))
        finishes = _parse_graph_instant(item.get("end"))
        if not begins or not finishes:
            continue
        # Touching at an edge is not an overlap: a meeting ending at 1:00 does
        # not collide with the 1:00 half-hour.
        if finishes <= start or begins >= end:
            continue
        if AVAILABILITY_SEVERITY.index(status) > AVAILABILITY_SEVERITY.index(worst):
            worst = status
    return worst


def _parse_graph_instant(value):
    """Read a Graph {dateTime, timeZone} pair, which omits the offset."""
    if isinstance(value, dict):
        text = str(value.get("dateTime") or "").strip()
        zone = str(value.get("timeZone") or "").strip()
    else:
        text = str(value or "").strip()
        zone = ""
    if not text:
        return None
    # Graph pads to seven fractional digits, which fromisoformat rejects.
    if "." in text:
        head, _, tail = text.partition(".")
        text = head + "." + tail[:6]
    parsed = None
    try:
        from datetime import datetime

        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo:
        return parsed
    from datetime import timezone

    if zone and zone.upper() != "UTC":
        from dateutil import tz as _tz

        named = _tz.gettz(zone)
        if named is not None:
            return parsed.replace(tzinfo=named)
    return parsed.replace(tzinfo=timezone.utc)


def _apply_verified_availability(
    attendees: list,
    slots: list,
    measurements: list,
    interval_minutes: int = AVAILABILITY_INTERVAL_MINUTES,
) -> list:
    """Replace claimed availability with measured availability, best first.

    Each measurement covers its own slot's window. A slot whose measurement is
    missing reads `unknown` rather than keeping what the preview claimed: the
    claim is exactly the thing that was never checked, and everything
    downstream -- the option text, the availability grid, the certifier --
    reads this map as measurement. Task 2558 showed both attendees green and
    "free" beside its own sentence saying the calendars could not be read.

    Nothing is withdrawn. A time that suits three of five people is often the
    right meeting to hold, and withdrawing it leaves the user with no options
    and no way to say who matters. Slots are ranked so the cleanest lead, and
    each carries the conflicts that cost it its place.
    """
    ranked = []
    for position, slot in enumerate(slots or []):
        measurement = (
            measurements[position]
            if measurements and position < len(measurements)
            else None
        ) or {}
        views, hours = {}, {}
        for entry in measurement.get("schedules") or []:
            email = str(entry.get("scheduleId") or "").strip().lower()
            if email:
                views[email] = entry.get("scheduleItems")
                hours[email] = entry.get("workingHours")

        measured, conflicts = {}, {}
        for email in attendees:
            key = str(email).strip().lower()
            status = _status_from_items(
                views.get(key), slot.get("start"), slot.get("end")
            )
            if status is None:
                # Never measured. "unknown" is the verdict-free answer; the
                # preview's own claim is not, because every reader downstream
                # treats this map as measurement.
                measured[key] = "unknown"
                continue
            measured[key] = status
            if status in CONFLICT_WEIGHTS:
                conflicts[key] = status
                continue
            hours_status = _working_hours_status(
                hours.get(key), slot.get("start"), slot.get("end")
            )
            if hours_status == "outsideWorkingHours":
                # A clear calendar well outside someone's own day is still a
                # poor time for them, and only their timezone can say so.
                measured[key] = hours_status
                conflicts[key] = hours_status
            elif hours_status == "nearWorkingHours" and status == "free":
                # Close enough to be a reasonable ask. Worth saying, not worth
                # ruling out -- the same standing as a tentative block.
                measured[key] = hours_status
        updated = dict(slot)
        updated["availability"] = measured
        if conflicts:
            updated["conflicts"] = conflicts
        else:
            updated.pop("conflicts", None)
        cost = sum(CONFLICT_WEIGHTS[status] for status in conflicts.values())
        # Soft notes never block a time, but they should not outrank a slot
        # that suits everyone outright.
        cost += sum(
            SOFT_WEIGHTS.get(str(status), 0)
            for email, status in measured.items()
            if email not in conflicts
        )
        # Cost first, then the worker's own ordering, which carries its sense
        # of which times suit the task.
        ranked.append((cost, len(conflicts), position, updated))

    ranked.sort(key=lambda entry: entry[:3])
    return [entry[3] for entry in ranked]


def _availability_phrase(status) -> str:
    """Plain words for a status, tolerant of casing."""
    text = str(status or "").strip()
    if text in AVAILABILITY_PHRASES:
        return AVAILABILITY_PHRASES[text]
    lowered = text.lower()
    for key, phrase in AVAILABILITY_PHRASES.items():
        if key.lower() == lowered:
            return phrase
    return lowered


def _availability_coverage(slots: list) -> str:
    """How much of what is being offered was actually measured.

    `availability_verified` is a single boolean over a set of slots that can
    each have a different measurement state, so "not all measured" and "none
    measured" collapse into the same value. Task 2610 measured two slots free,
    failed to measure a third, and told the user the calendars "could not be
    read" -- overstating the failure exactly as reporting "free" on an
    unmeasured slot overstated the success.
    """
    if not slots:
        return "none"
    measured = 0
    for slot in slots:
        values = list((slot.get("availability") or {}).values())
        if values and all(
            str(value).strip().lower() != "unknown" for value in values
        ):
            measured += 1
    if measured == len(slots):
        return "full"
    return "partial" if measured else "none"


def _availability_description(slot: dict, names: dict | None = None) -> str:
    """Say who cannot make it, by name, so the choice is an informed one."""
    lookup = names or {}
    conflicts = slot.get("conflicts") or {}
    availability = slot.get("availability") or {}
    statuses = {str(status).strip().lower() for status in availability.values()}
    if availability and statuses == {"unknown"}:
        # Nothing was measured, so the description must not answer the
        # question it was asked. Saying "available" here is the claim the
        # probe failed to check.
        return "Availability not checked - this time may clash."
    if not conflicts:
        soft = sorted(
            email for email, status in availability.items()
            if str(status).strip().lower() not in {"free", ""}
        )
        if not soft:
            return "All confirmed attendees are available."
        return "; ".join(
            f"{lookup.get(email, email)} is "
            f"{_availability_phrase(availability[email])}"
            for email in soft
        ) + "."
    return "; ".join(
        f"{lookup.get(email, email)} is {_availability_phrase(conflicts[email])}"
        for email in sorted(conflicts)
    ) + "."
# How long a Teams post stays recoverable. Teams cannot be stamped with a key,
# so recovery works by reading recent messages and matching sender and body.
# That look is only trustworthy while the message is still near the top of the
# chat; beyond this a busy thread could have pushed it out of view, and a
# "retry" would be a genuine second post.
TEAMS_RECOVERY_WINDOW_MINUTES = 120


def execute_prompt(
    payload: dict,
    correlation_id: str,
    idempotency_key_value: str | None = None,
    recover: bool = False,
) -> str:
    """Build a write-only prompt that sends the exact sealed payload once."""
    channel = payload.get("channel")
    marker_extra = ""
    if channel == "calendar":
        operation = (
            "Call workiq-create_entity exactly once with parentUrl /me/events. "
            "Map the exact subject, body, selected start/end/timezone, and attendees "
            "from the payload to a Microsoft Graph event. Return the created event id."
        )
        if idempotency_key_value:
            operation += (
                f' Set the event property "transactionId" to exactly '
                f'"{idempotency_key_value}". Do not alter or regenerate it. Graph '
                "uses it to avoid creating a duplicate meeting, so it must be sent "
                "verbatim, and it must be echoed back in the result block."
            )
            marker_extra = f',\n"idempotency_key":"{idempotency_key_value}"'
    elif channel == "email":
        rendered = plain_text_to_html(payload.get("body"))
        operation = (
            "Call workiq-do_action exactly once to send. For reply mode use "
            "/me/messages/{message_id}/reply. For new mode use /me/sendMail with "
            "the exact recipients and subject.\n\n"
            "Send the body as HTML, using this exact rendered content verbatim. "
            "Do not reformat it, re-wrap it, restyle it, or substitute the plain "
            'text version. Set the message body to {"contentType":"html",'
            '"content": <<<the block below>>>}:\n'
            "-----BEGIN APPROVED HTML BODY-----\n"
            f"{rendered}\n"
            "-----END APPROVED HTML BODY-----"
        )
        if idempotency_key_value:
            operation += (
                "\n\nBefore sending, attach Riveter's correlation header to the "
                "outgoing message so the sent copy can be identified afterwards. "
                "Set internetMessageHeaders on the message object to exactly "
                f'[{{"name":"{CORRELATION_HEADER}",'
                f'"value":"{idempotency_key_value}"}}]. For reply mode this goes '
                'in the "Message" property alongside the body; for new mail it '
                'goes in the "Message" property of /me/sendMail.'
                "\n\nAfter sending, the send returns 202 with no body, so it "
                "carries no reference. Do NOT invent one. Instead read "
                "/me/mailFolders/sentitems/messages"
                "?$top=5&$select=id,internetMessageId,internetMessageHeaders,"
                "toRecipients&$orderby=sentDateTime desc with workiq-fetch, find "
                f'the message whose {CORRELATION_HEADER} header equals '
                f'"{idempotency_key_value}", and return that message\'s id as '
                "delivery_ref. If no such message is found, return ok=false: "
                "never report a delivery reference you did not read back.\n\n"
                "Also report that sent message's actual toRecipients addresses "
                'as "recipients". A reply is addressed to its thread, so the '
                "delivered recipients can differ from the payload; report what "
                "the sent copy really shows, not what the payload asked for."
            )
            marker_extra = (
                f',\n"idempotency_key":"{idempotency_key_value}"'
                ',\n"recipients":["actual addresses from the sent message"]'
            )
    elif channel == "teams":
        rendered = plain_text_to_html(payload.get("body"))
        # Graph's chatMessage.body is an itemBody, not a string. "Post the exact
        # body from the payload" left the shape to the worker, which passed the
        # plain string through and was rejected with "Property body in payload
        # has a value that does not match schema" on every send (tasks 2592 and
        # 2593). Teams renders HTML, so the same rendering email uses applies:
        # Riveter owns the wire format for what it approved.
        body_block = (
            'Graph requires the message body to be an itemBody object, not a '
            'string. Set it to {"contentType":"html","content": <<<the block '
            "below>>>} using this exact rendered content verbatim. Do not "
            "reformat it, re-wrap it, restyle it, or substitute the plain text "
            "version:\n"
            "-----BEGIN APPROVED HTML BODY-----\n"
            f"{rendered}\n"
            "-----END APPROVED HTML BODY-----"
        )
        operation = (
            "Call workiq-create_entity exactly once. For a chat use "
            "/me/chats/{chat_id}/messages. For a channel reply use "
            "/teams/{team_id}/channels/{channel_id}/messages/{message_id}/replies.\n\n"
            f"{body_block}\n\n"
            "Return the created message id."
        )
        if recover:
            # Teams cannot be stamped with a key, so the only defence against a
            # second post is to look for the first one before writing.
            operation = (
                "An earlier attempt to post this message may or may not have "
                "succeeded, and Teams offers no idempotency key, so you must "
                "look before you write.\n\n"
                "1. Read the recent messages in the destination thread with "
                "workiq-fetch: /chats/{chat_id}/messages?$top=25 for a chat, or "
                "/teams/{team_id}/channels/{channel_id}/messages/{message_id}"
                "/replies?$top=25 for a channel reply.\n"
                "2. If a message sent by the signed-in user already carries the "
                "approved content below, the earlier attempt succeeded. Compare "
                "on the visible text, ignoring HTML tag differences, because the "
                "post was made as HTML. Do not post anything. Return that "
                'message\'s id as delivery_ref with "already_posted": true.\n'
                "3. Only if no such message exists, call workiq-create_entity "
                "exactly once to post it, and return the new message id with "
                '"already_posted": false.\n\n'
                f"{body_block}\n\n"
                "Never post when step 2 matched, and never report a message id "
                "you did not either read or create."
            )
            marker_extra += ',\n"already_posted":true or false'
    else:
        raise ValueError(f"Unsupported structured delivery channel: {channel}")
    # The blanket "do not fetch" rule predates the email read-back and would
    # now contradict the instruction that follows it. Contradictory prompts are
    # their own failure mode, so the read allowance is stated per channel.
    if channel == "email":
        read_rule = (
            "The only read permitted is the sent-items lookup described below; "
            "do not search for anything else."
        )
    elif channel == "teams" and recover:
        read_rule = (
            "The only read permitted is the thread lookup described below; do "
            "not search for anything else."
        )
    else:
        read_rule = "Do not search or fetch anything."
    write_rule = (
        "Post at most once, and only if the message is not already there."
        if channel == "teams" and recover
        else "Perform one write only."
    )
    return f"""
You are Riveter's {channel} execution worker. {write_rule}
{read_rule}
Do not reinterpret, improve, or change the approved payload.

Sealed payload:
{_json(payload)}

{operation}

Return exactly one result block:
{RESULT_START}
{{"correlation_id":"{correlation_id}","phase":"execute","ok":true,
"delivery_ref":"non-empty external reference"{marker_extra}}}
{RESULT_END}

If the write fails or its result is ambiguous, return ok=false with an error.
Never retry a write.
""".strip()


PREVIEW_TIMEOUT_SECONDS = 300
# A calendar preview asks WorkIQ to find candidate times across several
# attendees' calendars, which is a slower question than drafting a message.
# Task 2558 lost two runs (actions 262 and 267) at exactly 301s on the shared
# default, and a run killed on its budget leaves nothing to show for the
# minutes it spent.
CALENDAR_PREVIEW_TIMEOUT_SECONDS = 420


def _preview_timeout(channel: str | None) -> int:
    """How long a preview of this kind is allowed to take."""
    if str(channel or "").strip().lower() == "calendar":
        return CALENDAR_PREVIEW_TIMEOUT_SECONDS
    return PREVIEW_TIMEOUT_SECONDS


def _run(argv: list[str], timeout: int = 300) -> subprocess.CompletedProcess[str]:    # encoding/errors are load-bearing, not cosmetic. `text=True` alone decodes
    # with the locale codec, which on Windows is cp1252; the CLI emits UTF-8, so
    # the reader thread died on the first non-cp1252 byte and subprocess.run
    # returned returncode 0 with stdout=None. Every structured channel then
    # failed. cowork_runner spawns with the same pair for the same reason.
    return subprocess.run(
        argv,
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        check=False,
    )


def apply_email_signature(payload: dict) -> None:
    """Put the signature into the body the user is about to approve.

    Graph sends exactly what it is given -- /me/sendMail and /reply append
    nothing -- so mail Riveter sent went out unsigned while the voice layer
    told the drafter a signature would be added for it.

    Appended here rather than at send time because the user approves an exact
    draft; adding text afterwards would deliver something they never read.
    Appended rather than requested in the prompt because a signature is a
    fixed block, not a judgement call.
    """
    if payload.get("channel") != "email":
        return
    from src.services.cowork_runner import email_signature

    signature = email_signature()
    if not signature:
        return
    body = str(payload.get("body") or "").rstrip()
    # A drafter that already reproduced it should not get it twice.
    if signature.splitlines()[0].strip() in body:
        return
    payload["body"] = f"{body}\n\n{signature}" if body else signature


def _preview_draft(payload: dict) -> str:
    channel = payload.get("channel")
    if channel == "calendar":
        slots = payload.get("slots") or []
        lines = [payload.get("subject") or "Meeting", "", payload.get("body") or ""]
        if slots:
            lines.append("")
            lines.append("Proposed times:")
        # The label already carries the full range, so appending the raw ISO end
        # rendered "2:05-2:30 PM ET - 2026-08-25T14:30:00-04:00", which reads as
        # a contradiction rather than extra detail.
        lines.extend(
            f"- {slot.get('label') or slot.get('start')}" for slot in slots
        )
        return "\n".join(line for line in lines if line is not None).strip()
    if channel == "email":
        return (
            f"Subject: {payload.get('subject') or ''}\n\n"
            f"{payload.get('body') or ''}"
        ).strip()
    return str(payload.get("body") or "").strip()


def calendar_event_summary(event: dict, label: str | None = None) -> str:
    """Describe the meeting that was actually booked.

    The execution row previously inherited the preview draft, so a finished
    meeting still listed every time that had merely been offered. What the card
    owes the reader afterwards is what happened, not what was considered.
    """
    when = str(label or "").strip() or " to ".join(
        part for part in (
            str(event.get("start") or "").strip(),
            str(event.get("end") or "").strip(),
        ) if part
    )
    attendees = ", ".join(
        str(person.get("name") or person.get("email") or "").strip()
        for person in event.get("attendees") or []
        if str(person.get("name") or person.get("email") or "").strip()
    )
    lines = [str(event.get("subject") or "Meeting").strip(), ""]
    body = str(event.get("body") or "").strip()
    if body:
        lines.extend([body, ""])
    if when:
        lines.append(f"When: {when}")
    duration = event.get("duration_minutes")
    if isinstance(duration, int) and duration > 0:
        lines.append(f"Duration: {duration} minutes")
    if attendees:
        lines.append(f"Attendees: {attendees}")
    return "\n".join(lines).strip()


def _preview_destination(payload: dict) -> tuple[str, str]:
    channel = payload.get("channel")
    if channel == "calendar":
        attendees = payload.get("attendees") or []
        refs = [str(item.get("email") or "").strip() for item in attendees]
        names = [
            str(item.get("name") or item.get("email") or "").strip()
            for item in attendees
        ]
        return ";".join(filter(None, refs)), ", ".join(filter(None, names))
    if channel == "email":
        recipients = [str(item).strip() for item in payload.get("to") or []]
        return ";".join(filter(None, recipients)), ", ".join(filter(None, recipients))
    chat_id = str(payload.get("chat_id") or "").strip()
    if chat_id:
        ref = chat_id
    else:
        # Joining raw values here once produced "||" from three empty ids, which
        # is truthy and sailed through the unresolved-destination guard. Require
        # the full channel triple instead, so a half-resolved thread fails.
        parts = [
            str(payload.get(key) or "").strip()
            for key in ("team_id", "channel_id", "message_id")
        ]
        ref = "|".join(parts) if all(parts) else ""
    return ref, str(payload.get("destination_display") or ref or "")


def finish_preview(
    action_id: int,
    *,
    stdout: str,
    stderr: str,
    exit_code: int,
    correlation_id: str,
    expected_channel: str | None = None,
    expected_attendees: set[str] | None = None,
    expected_duration: int | None = None,
    _graph_evidence: dict | None = None,
) -> dict | None:
    """Persist a validated preview or a fail-closed error."""
    if exit_code != 0:
        return update_task_action(
            action_id,
            frozenset({"state", "error"}),
            required_state="previewing",
            state="failed",
            error=(stderr or f"WorkIQ preview exited with code {exit_code}")[-4000:],
        )
    try:
        result = parse_result_marker(
            stdout, correlation_id=correlation_id, phase="preview"
        )
        payload = result.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("Structured preview payload is missing")
        channel = payload.get("channel")
        if channel not in STRUCTURED_CHANNELS:
            raise ValueError("Structured preview channel is invalid")
        if expected_channel is not None and channel != expected_channel:
            raise ValueError("Structured preview channel changed during resolution")
        destination_ref, destination_display = _preview_destination(payload)
        if not destination_ref or not destination_display:
            raise ValueError("Structured preview destination is unresolved")
        # Before the draft is built, so the text the user approves is the text
        # that is sent.
        apply_email_signature(payload)
        draft = _preview_draft(payload)
        if not draft:
            raise ValueError("Structured preview content is empty")
        fields: dict[str, Any] = {
            "state": "ready",
            "draft": draft,
            "finding": draft,
            "structured_payload": _json(payload),
            "delivery_channel": channel,
            "destination_ref": destination_ref,
            "destination_display": destination_display,
            "destination_source": "workiq_preview",
        }
        if channel == "calendar":
            # The certifier that gates slot selection compares this list against
            # cowork_runner._attendee_emails(), which is sorted and de-duplicated.
            # Building it in payload order instead produced an equal *set* but an
            # unequal *list*, so every slot click was refused with "no longer
            # verified for the current attendees". Share the one canonical form.
            from src.services.cowork_runner import _attendee_emails

            attendees = _attendee_emails(payload.get("attendees") or [])
            duration = payload.get("duration_minutes")
            slots = payload.get("slots")
            if not attendees or not isinstance(duration, int) or duration <= 0:
                raise ValueError("Calendar attendees or duration are invalid")
            if expected_attendees is not None and set(attendees) != expected_attendees:
                raise ValueError("Calendar attendees changed during resolution")
            if expected_duration is not None and duration != expected_duration:
                raise ValueError("Calendar duration changed during resolution")
            if not isinstance(slots, list) or not 1 <= len(slots) <= 3:
                raise ValueError("Calendar preview must contain one to three slots")
            options = []
            evidence_slots = []
            for index, slot in enumerate(slots):
                if not isinstance(slot, dict):
                    raise ValueError("Calendar slot is invalid")
                value = str(slot.get("id") if slot.get("id") is not None else index)
                start = str(slot.get("start") or "").strip()
                end = str(slot.get("end") or "").strip()
                timezone_name = str(
                    slot.get("timezone") or payload.get("timezone") or ""
                ).strip()
                availability = slot.get("availability")
                if (
                    not start
                    or not end
                    or not timezone_name
                    or not isinstance(availability, dict)
                    or {
                        str(email).strip().lower() for email in availability
                    } != set(attendees)
                    or any(
                        (
                            _graph_status(status) is None
                            and not (
                                _graph_evidence is None
                                and str(status).strip().lower() == "unknown"
                            )
                        )
                        for status in availability.values()
                    )
                ):
                    raise ValueError("Calendar slot lacks complete availability evidence")
                event = {
                    "start": start,
                    "end": end,
                    "time_zone": timezone_name,
                }
                if (
                    not named_timezone_matches(start, timezone_name)
                    or not named_timezone_matches(end, timezone_name)
                    or not calendar_event_is_future(event)
                    or calendar_event_duration_minutes(event) != duration
                ):
                    raise ValueError("Calendar slot has invalid date or timezone data")
                normalized_availability = {
                    str(email).strip().lower(): (
                        _graph_status(status)
                        or (
                            "unknown"
                            if _graph_evidence is None
                            and str(status).strip().lower() == "unknown"
                            else None
                        )
                    )
                    for email, status in availability.items()
                }
                options.append({
                    "label": str(slot.get("label") or start),
                    "value": value,
                    "description": "All confirmed attendees are available.",
                })
                evidence_slots.append({
                    "value": value,
                    "label": str(slot.get("label") or start),
                    "start": start,
                    "end": end,
                    "timezone": timezone_name,
                    "availability": normalized_availability,
                })
            attendee_timezones = {}
            if _graph_evidence is not None:
                if (
                    not isinstance(_graph_evidence, dict)
                    or _graph_evidence.get("source") != GRAPH_SCHEDULE_SOURCE
                    or _graph_evidence.get("query_backed") is not True
                    or _graph_evidence.get("availability_verified") is not True
                ):
                    raise ValueError("Structured scheduler evidence is invalid")
                availability_verified = True
                attendee_timezones = dict(
                    _graph_evidence.get("attendee_timezones") or {}
                )
                # Graph already measured these exact free blocks. Calling
                # getSchedule here would spend another agent run and could
                # overwrite stronger evidence with an infrastructure failure.
                for slot_record in evidence_slots:
                    conflicts = {
                        email: status
                        for email, status in slot_record["availability"].items()
                        if status in CONFLICT_WEIGHTS
                    }
                    if conflicts:
                        slot_record["conflicts"] = conflicts
            else:
                # The worker cannot reach getSchedule, so everything above is
                # a claim. Riveter measures it before offering it: task 2478
                # proposed a Wednesday its own evidence called free while the
                # attendee was out of office all day.
                schedules = fetch_availability(attendees, evidence_slots)
                attendee_timezones = _timezones_from_measurements(schedules)
                availability_verified = bool(schedules) and all(
                    (entry or {}).get("schedules") for entry in schedules
                )
                if schedules:
                    evidence_slots = _apply_verified_availability(
                        attendees, evidence_slots, schedules,
                        AVAILABILITY_INTERVAL_MINUTES,
                    )
                else:
                    evidence_slots = _apply_verified_availability(
                        attendees, evidence_slots, [],
                        AVAILABILITY_INTERVAL_MINUTES,
                    )
            attendee_names = {
                str(person.get("email") or "").strip().lower():
                    str(person.get("name") or "").strip()
                for person in (payload.get("attendees") or [])
                if isinstance(person, dict) and person.get("name")
            }
            options = [
                {
                    "label": slot_record["label"],
                    "value": slot_record["value"],
                    "description": _availability_description(
                        slot_record, attendee_names
                    ),
                }
                for slot_record in evidence_slots
            ]
            if not options:
                raise ValueError("Calendar preview must contain one to three slots")
            contested = [
                slot_record for slot_record in evidence_slots
                if slot_record.get("conflicts")
            ]
            coverage = _availability_coverage(evidence_slots)
            interaction = {
                "invocation_id": f"structured-calendar-{action_id}",
                "questions": [{
                    "id": "0",
                    "header": "Select & create meeting",
                    "question": (
                        # When nobody is free the honest move is to say so and
                        # invite steering, rather than hide the conflict or
                        # offer nothing at all. And when the calendars could
                        # not be read at all, say that rather than implying
                        # these times were checked -- or, when only some were
                        # read, implying none of them were.
                        "I could not read the attendees' calendars, so these "
                        "times are unchecked - they may clash. Choose one, or "
                        "ask me to try again. There is no second confirmation."
                        if coverage == "none"
                        else "I could only check some of these times; the rest "
                        "are unchecked and may clash. Each one says which it "
                        "is. There is no second confirmation."
                        if coverage == "partial"
                        else "No time suits everyone. Times are ordered by who "
                        "can attend, and each says who cannot. Choose the best "
                        "one, or say whose availability matters most and I "
                        "will look again. There is no second confirmation."
                        if contested and len(contested) == len(evidence_slots)
                        else "Choose one verified time, then press Select & "
                        "create meeting. There is no second confirmation."
                    ),
                    "multi_select": False,
                    "options": options,
                }],
                "schedule_evidence": {
                    "valid": True,
                    # Preview holds only read tools, and Graph's findMeetingTimes
                    # and getSchedule are POST actions needing do_action, so this
                    # worker cannot call the scheduler. Availability here comes
                    # from Copilot M365 answering a live query. That is a genuine
                    # query, just a weaker evidence class than the scheduler's
                    # confidence-ranked output, and the certifier is told which
                    # one it is rather than being handed a label it will match
                    # against itself.
                    "source": (
                        _graph_evidence["source"]
                        if _graph_evidence is not None
                        else "copilot-ask"
                    ),
                    "attendees": attendees,
                    "query_backed": True,
                    # Whether the availability above was measured against the
                    # attendees' calendars or is only the worker's claim. The
                    # certifier refuses a selection when this is False rather
                    # than booking on an unchecked guess.
                    "availability_verified": availability_verified,
                    # Which of the offered slots were actually measured, so
                    # the card can distinguish "none" from "not all".
                    "availability_coverage": coverage,
                    "duration_minutes": duration,
                    "start_offset_minutes": (
                        _graph_evidence.get("start_offset_minutes")
                        if _graph_evidence is not None
                        else None
                    ),
                    "slots": evidence_slots,
                    **({
                        key: value
                        for key, value in _graph_evidence.items()
                        if key not in {
                            "source",
                            "query_backed",
                            "availability_verified",
                            "start_offset_minutes",
                        }
                    } if _graph_evidence is not None else {}),
                },
            }
            if attendee_timezones:
                interaction["schedule_evidence"]["attendee_timezones"] = (
                    attendee_timezones
                )
                interaction["schedule_evidence"]["attendee_timezone_labels"] = (
                    _attendee_timezone_labels(
                        attendee_timezones, evidence_slots
                    )
                )
            fields.update({
                "state": "previewing",
                "blocked_question": _json(interaction),
                "had_interaction": 1,
            })
        return update_task_action(
            action_id,
            frozenset(fields),
            required_state="previewing",
            **fields,
        )
    except ValueError as exc:
        return update_task_action(
            action_id,
            frozenset({"state", "error"}),
            required_state="previewing",
            state="failed",
            error=str(exc),
        )


def finish_execute(
    action_id: int,
    *,
    stdout: str,
    stderr: str,
    exit_code: int,
    correlation_id: str,
    expected_idempotency_key: str | None = None,
    require_post_disposition: bool = False,
    expected_recipients: set[str] | None = None,
) -> dict | None:
    """Persist execution only when correlated external delivery evidence exists."""
    if exit_code != 0:
        message = (stderr or f"WorkIQ execution exited with code {exit_code}")[-4000:]
        return update_task_action(
            action_id,
            frozenset({"state", "error"}),
            required_state="executing",
            state="execute_unconfirmed",
            error=message,
        )
    try:
        result = parse_result_marker(
            stdout,
            correlation_id=correlation_id,
            phase="execute",
            require_delivery_ref=True,
        )
        if expected_idempotency_key is not None:
            echoed = str(result.get("idempotency_key") or "").strip()
            if echoed != expected_idempotency_key:
                # Without the key we cannot recognise our own write later, so a
                # retry could send twice and the reference may not be real.
                raise ValueError(
                    "The write completed without Riveter's idempotency key"
                )
        if require_post_disposition and not isinstance(
            result.get("already_posted"), bool
        ):
            # A recovery run must say whether it found the message or sent it.
            # Without that, "it worked" cannot be told apart from "it posted a
            # second copy", which is the only thing recovery exists to prevent.
            raise ValueError(
                "The recovery run did not report whether it had already posted"
            )
        if expected_recipients is not None:
            reported = {
                str(value).strip().lower()
                for value in (result.get("recipients") or [])
                if str(value).strip()
            }
            if not reported:
                raise ValueError(
                    "The send did not report who actually received it"
                )
            if reported != {str(v).strip().lower() for v in expected_recipients}:
                # A reply is addressed to its thread, so Graph can deliver to
                # people the approved destination never named.
                raise ValueError(
                    "The message went to different recipients than the ones "
                    f"approved ({', '.join(sorted(reported))})"
                )
        delivery_ref = str(result["delivery_ref"]).strip()
        return update_task_action(
            action_id,
            frozenset({"state", "workiq_delivery_ref", "error"}),
            required_state="executing",
            state="executed",
            workiq_delivery_ref=delivery_ref,
            error=None,
        )
    except ValueError as exc:
        return update_task_action(
            action_id,
            frozenset({"state", "error"}),
            required_state="executing",
            state="execute_unconfirmed",
            error=(
                f"{exc}. Delivery could not be confirmed; check the destination "
                "before retrying."
            ),
        )


def _preview_worker(task: dict, action: dict) -> None:
    payload = json.loads(action["structured_payload"])
    correlation_id = payload["correlation_id"]
    try:
        result = _run(
            preview_command(preview_prompt(task, payload)),
            timeout=_preview_timeout(payload.get("channel")),
        )
        preview_stdout = result.stdout
        graph_evidence = None
        phase_one_payload = None
        if payload["channel"] == "calendar" and result.returncode == 0:
            try:
                phase_one = parse_result_marker(
                    result.stdout,
                    correlation_id=correlation_id,
                    phase="preview",
                )
                phase_one_payload = phase_one.get("payload")
                if not isinstance(phase_one_payload, dict):
                    raise ValueError("Calendar phase-one payload is missing")
                slots, graph_evidence = _find_meeting_slots(
                    task, phase_one_payload
                )
                phase_one_payload["slots"] = slots
                preview_stdout = (
                    RESULT_START
                    + _json({
                        "correlation_id": correlation_id,
                        "phase": "preview",
                        "ok": True,
                        "payload": phase_one_payload,
                    })
                    + RESULT_END
                )
            except NoMutualFreeTime as exc:
                update_task_action(
                    action["id"],
                    frozenset({"state", "error"}),
                    required_state="previewing",
                    state="failed",
                    error=(
                        f"{exc} Try a different week or say whose "
                        "availability matters most."
                    ),
                )
                return
            except Exception as exc:  # noqa: BLE001 - existing probe is fallback
                logger.warning(
                    "Structured scheduler unavailable; using getSchedule fallback: %s",
                    exc,
                )
                if isinstance(phase_one_payload, dict):
                    expected = {
                        str(person.get("email") or "").strip().lower()
                        for person in _key_people(task)
                        if str(person.get("email") or "").strip()
                    }
                    for slot in phase_one_payload.get("slots") or []:
                        if not isinstance(slot, dict):
                            continue
                        existing = {
                            str(email).strip().lower(): (
                                _graph_status(status) or "unknown"
                            )
                            for email, status in (
                                slot.get("availability") or {}
                            ).items()
                        }
                        slot["availability"] = {
                            email: existing.get(email, "unknown")
                            for email in expected
                        }
                    preview_stdout = (
                        RESULT_START
                        + _json({
                            "correlation_id": correlation_id,
                            "phase": "preview",
                            "ok": True,
                            "payload": phase_one_payload,
                        })
                        + RESULT_END
                    )
        finish_preview(
            action["id"],
            stdout=preview_stdout,
            stderr=result.stderr,
            exit_code=result.returncode,
            correlation_id=correlation_id,
            expected_channel=payload["channel"],
            expected_attendees=(
                {
                    str(person.get("email") or "").strip().lower()
                    for person in _key_people(task)
                    if str(person.get("email") or "").strip()
                }
                if payload["channel"] == "calendar"
                else None
            ),
            expected_duration=(
                _meeting_duration(task) if payload["channel"] == "calendar" else None
            ),
            _graph_evidence=graph_evidence,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("structured preview failed")
        update_task_action(
            action["id"],
            frozenset({"state", "error"}),
            required_state="previewing",
            state="failed",
            error=f"Could not complete WorkIQ preview: {exc}",
        )
    finally:
        with _threads_lock:
            _threads.pop(f"preview:{action['id']}", None)


def _execute_worker(action: dict, recover: bool = False) -> None:
    payload = json.loads(action["structured_payload"])
    correlation_id = str(uuid.uuid4())
    # Derived from the row, not the attempt, so re-running this execution reuses
    # the same key: Graph returns the existing event, and the sent mail copy
    # stays findable instead of being sent twice.
    key = idempotency_key(action)
    teams_recovery = recover and payload.get("channel") == "teams"
    # The user approved a specific recipient list; Graph's /reply can deliver
    # somewhere else entirely, so the sent copy is checked against it.
    expected_recipients = (
        {
            str(value).strip().lower()
            for value in payload.get("to") or []
            if str(value).strip()
        }
        if payload.get("channel") == "email"
        else None
    ) or None
    try:
        result = _run(
            execute_command(
                execute_prompt(payload, correlation_id, key, recover=recover),
                payload["channel"],
                recover=recover,
            )
        )
        finish_execute(
            action["id"],
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.returncode,
            correlation_id=correlation_id,
            expected_idempotency_key=key,
            require_post_disposition=teams_recovery,
            expected_recipients=expected_recipients,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("structured execution failed")
        update_task_action(
            action["id"],
            frozenset({"state", "error"}),
            required_state="executing",
            state="execute_unconfirmed",
            error=(
                f"Could not confirm the WorkIQ delivery: {exc}. Check the "
                "destination before retrying."
            ),
        )
    finally:
        with _threads_lock:
            _threads.pop(f"execute:{action['id']}", None)


def _start_thread(key: str, target, *args) -> None:
    if not external_integrations_enabled():
        raise RuntimeError("Structured WorkIQ delivery is disabled in demo mode")
    with _threads_lock:
        running = _threads.get(key)
        if running and running.is_alive():
            raise RuntimeError("Structured WorkIQ operation is already running")
        thread = threading.Thread(
            target=target,
            args=args,
            daemon=True,
            name=f"riveter-{key}",
        )
        _threads[key] = thread
        thread.start()


def start_preview(task: dict, action: dict) -> None:
    """Launch a read-only structured preview worker."""
    if get_task(task["id"]) is None:
        raise ValueError("Task not found")
    _start_thread(f"preview:{action['id']}", _preview_worker, dict(task), dict(action))


def start_execute(action: dict, recover: bool = False) -> None:
    """Launch a single-write structured execution worker.

    ``recover`` re-runs an execution whose outcome was never confirmed. For
    calendar and email the stamped key makes that inherently safe; for Teams it
    switches the worker into look-before-you-write mode, which is the only
    protection available there.
    """
    _start_thread(
        f"execute:{action['id']}", _execute_worker, dict(action), recover
    )
