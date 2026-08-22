"""Direct, structured WorkIQ delivery for calendar, email, and Teams actions."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any

from src.models import get_task, update_task_action
from src.services.calendar_time import (
    calendar_event_duration_minutes,
    calendar_event_is_future,
    named_timezone_matches,
)
from src.services.runtime_mode import external_integrations_enabled


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
    if action_type in {"follow-up", "awaiting-response"} and source_type in {
        "teams",
        "chat",
        "teams_chat",
        "teams-channel",
    }:
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


def execute_command(prompt: str, channel: str) -> list[str]:
    """Build a subprocess command with one channel-specific write primitive."""
    tool = EXECUTE_TOOLS.get(channel)
    if not tool:
        raise ValueError(f"Unsupported structured delivery channel: {channel}")
    tools = [tool]
    if channel == "email":
        # /sendMail and /reply return 202 with no body, so the only honest way
        # to produce a delivery reference is to read the sent copy back. This
        # adds a READ tool, never a second write primitive.
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
    return {
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
    default_minutes = int(preferences.get("default_minutes") or 25)
    start_offset = int(preferences.get("start_offset_minutes") or 0)
    standing_rule = (
        f"The user's standing meeting duration is {default_minutes} minutes. Use "
        f"{default_minutes} minutes unless the task explicitly states another "
        f"duration. Start suggestions at :{start_offset:02d} or "
        f":{(start_offset + 30) % 60:02d}. Suggest 1-3 future mutual-free slots, "
        "treating tentative calendar blocks as available."
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
    return f"""
You are Riveter's read-only {channel} preview worker. Use only the visible WorkIQ
read tools. Do not create, update, send, post, or delete anything.

Resolve every delivery identifier now. Do not defer recipient, message, chat,
team, channel, thread, attendee, timezone, or slot resolution until execution.
{standing_rule}{content_rule}

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


CORRELATION_HEADER = "x-riveter-correlation-id"


def execute_prompt(
    payload: dict,
    correlation_id: str,
    idempotency_key_value: str | None = None,
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
        operation = (
            "Call workiq-do_action exactly once to send. For reply mode use "
            "/me/messages/{message_id}/reply. For new mode use /me/sendMail with "
            "the exact recipients, subject, and body."
        )
        if idempotency_key_value:
            operation += (
                "\n\nBefore sending, attach Riveter's correlation header to the "
                "outgoing message so the sent copy can be identified afterwards. "
                "Set internetMessageHeaders on the message object to exactly "
                f'[{{"name":"{CORRELATION_HEADER}",'
                f'"value":"{idempotency_key_value}"}}]. For reply mode this goes '
                'in the "Message" property alongside "Comment"; for new mail it '
                'goes in the "Message" property of /me/sendMail.'
                "\n\nAfter sending, the send returns 202 with no body, so it "
                "carries no reference. Do NOT invent one. Instead read "
                "/me/mailFolders/sentitems/messages"
                "?$top=5&$select=id,internetMessageId,internetMessageHeaders"
                "&$orderby=sentDateTime desc with workiq-fetch, find the message "
                f'whose {CORRELATION_HEADER} header equals '
                f'"{idempotency_key_value}", and return that message\'s id as '
                "delivery_ref. If no such message is found, return ok=false: "
                "never report a delivery reference you did not read back."
            )
            marker_extra = f',\n"idempotency_key":"{idempotency_key_value}"'
    elif channel == "teams":
        operation = (
            "Call workiq-create_entity exactly once. For a chat use "
            "/me/chats/{chat_id}/messages. For a channel reply use "
            "/teams/{team_id}/channels/{channel_id}/messages/{message_id}/replies. "
            "Post the exact body from the payload and return the created message id."
        )
    else:
        raise ValueError(f"Unsupported structured delivery channel: {channel}")
    # The blanket "do not fetch" rule predates the email read-back and would
    # now contradict the instruction that follows it. Contradictory prompts are
    # their own failure mode, so the read allowance is stated per channel.
    read_rule = (
        "The only read permitted is the sent-items lookup described below; do "
        "not search for anything else."
        if channel == "email"
        else "Do not search or fetch anything."
    )
    return f"""
You are Riveter's {channel} execution worker. Perform one write only.
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


def _run(argv: list[str], timeout: int = 300) -> subprocess.CompletedProcess[str]:
    # encoding/errors are load-bearing, not cosmetic. `text=True` alone decodes
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
                        str(status).strip().lower() not in {"free", "tentative"}
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
                    str(email).strip().lower(): str(status).strip().lower()
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
            interaction = {
                "invocation_id": f"structured-calendar-{action_id}",
                "questions": [{
                    "id": "0",
                    "header": "Select & create meeting",
                    "question": (
                        "Choose one verified time, then press Select & create "
                        "meeting. There is no second confirmation."
                    ),
                    "multi_select": False,
                    "options": options,
                }],
                "schedule_evidence": {
                    "valid": True,
                    "source": "FindMeetingTimes+interaction",
                    "attendees": attendees,
                    "query_backed": True,
                    "duration_minutes": duration,
                    "start_offset_minutes": None,
                    "slots": evidence_slots,
                },
            }
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
        result = _run(preview_command(preview_prompt(task, payload)))
        finish_preview(
            action["id"],
            stdout=result.stdout,
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


def _execute_worker(action: dict) -> None:
    payload = json.loads(action["structured_payload"])
    correlation_id = str(uuid.uuid4())
    # Derived from the row, not the attempt, so re-running this execution reuses
    # the same key: Graph returns the existing event, and the sent mail copy
    # stays findable instead of being sent twice.
    key = idempotency_key(action)
    try:
        result = _run(
            execute_command(
                execute_prompt(payload, correlation_id, key),
                payload["channel"],
            )
        )
        finish_execute(
            action["id"],
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.returncode,
            correlation_id=correlation_id,
            expected_idempotency_key=key,
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


def start_execute(action: dict) -> None:
    """Launch a single-write structured execution worker."""
    _start_thread(f"execute:{action['id']}", _execute_worker, dict(action))
