"""Cowork preview and explicitly approved action API.

Routes:
    POST /api/tasks/<id>/cowork    start a preview (202); 409 if already running
    GET  /api/tasks/<id>/cowork    latest attempt; ?history=1 for the full chain
    PUT  /api/tasks/<id>/cowork    save the user's edited draft
    POST /api/tasks/<id>/cowork/execute  execute one approved draft (202)
"""

import json
import logging
import re
import threading
import asyncio

import tornado.web

logger = logging.getLogger(__name__)

from ..models import (
    ACTION_EDITABLE_FIELDS,
    DELIVERY_CHANNELS,
    clear_blocked_question_if_unchanged,
    claim_blocked_question_answer,
    confirm_destination,
    create_execution_action,
    create_task_action,
    get_latest_task_action,
    get_task,
    list_task_actions,
    mark_task_action_seen,
    restore_claimed_blocked_question,
    set_blocked_question_if_missing,
    update_task_action,
)
from ..services.cowork_runner import (
    AlreadyRunning,
    CoworkAnswerRejected,
    _looks_like_write,
    answer_interaction,
    cancel_run,
    compose_prompt,
    compose_execution_prompt,
    compose_refine_prompt,
    continue_preview,
    default_delivery_channel,
    execution_label,
    get_result,
    get_progress,
    get_cached_cowork_island,
    handoff_status,
    is_running,
    new_conversation_id,
    parse_cowork_output,
    parse_execution_output,
    parse_source_url,
    preview_label,
    read_blocked_question,
    start_preview,
    start_execution,
)
from ..services.workspace_settings import api_transport_enabled

# Test seams. Production leaves both None so the runner uses its own defaults.
SPAWN = None
LOG_DIR_OVERRIDE = None

# Handoff lookup seam. Production leaves this None and the runner's own
# `handoff_status` is used; tests replace it so the unit suite never touches the
# network. A real network call in the poll path once took the suite from 35s to
# 313s, so this seam is not optional.
HANDOFF_FN = None
BLOCKED_QUESTION_FN = None
BLOCKED_QUESTION_STORE_FN = None
_question_recovering = set()
_question_recovering_lock = threading.Lock()

# Cancel seam, same reasoning: the unit suite must never post a real pause.
CANCEL_FN = None
ANSWER_FN = None
EXECUTE_FN = None
EXECUTE_TRANSPORT_ENABLED_FN = None

# Bulk of the CLI payload (82 entries in the spike) and of no value once parsed.
_NEVER_PERSIST = ("sse_events",)


def _refs(task: dict) -> list:
    """--ref person:<email> for each known participant.

    key_people is JSON on 1942 of 1958 live tasks — a list of
    {"name", "email", "role"} objects — not the comma separated string it
    looks like. Splitting on commas produced refs such as
    `person:[{"name": "Sarah Goodwin"` on essentially every task, which is why
    this parses properly and falls back to a plain split only for the handful
    of rows that really are prose.
    """
    raw = (task.get("key_people") or "").strip()
    if not raw:
        return []

    candidates = []
    if raw.startswith("[") or raw.startswith("{"):
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
        if isinstance(data, dict):
            data = [data]
        for person in data:
            if isinstance(person, dict):
                value = person.get("email") or person.get("name")
            else:
                value = str(person)
            if value:
                candidates.append(str(value).strip())
    else:
        candidates = [c.strip() for c in raw.replace(";", ",").split(",")]

    refs = []
    for value in candidates:
        if not value:
            continue
        ref = f"person:{value}"
        if ref not in refs:
            refs.append(ref)
    return refs


_CHANNEL_BY_SOURCE = {
    "email": "email",
    "chat": "teams",
    "teams": "teams",
    "meeting": "teams",
}

_BROADCAST_KINDS = ("group", "meeting", "channel", "unknown")

# Channel inference from the task's own words, for tasks with no source URL and
# no channel-bearing source_type. Derived from a sweep of all 1,967 live tasks,
# not invented: these Teams phrasings scored 174 true / 0 false against
# source-derived labels.
#
# Email is deliberately NOT inferred. The same sweep showed email wording is
# usually background context ("review the email thread", "so Aamer can send an
# email") rather than a delivery instruction, and it scored 20%. The failure is
# also asymmetric: a wrong "email" puts a subject line and a sign-off on a Teams
# message, while no channel simply falls back to the neutral voice.
_TEAMS_TEXT_RE = re.compile(
    r"\bteams (?:message|note|chat|dm)\b|\bon teams\b|\bping\b", re.I
)
# Any mention of mail makes the instruction ambiguous ("send a Teams message or
# email"), so infer nothing rather than pick a side.
_MAIL_TEXT_RE = re.compile(r"\bemail\b|\bmail\b|\boutlook\b", re.I)


def _infer_channel_from_text(task: dict) -> str | None:
    """Read an explicit delivery channel out of the task's own text."""
    blob = " ".join(
        (task.get(field) or "")
        for field in ("title", "description", "coaching_text")
    )
    if not _TEAMS_TEXT_RE.search(blob) or _MAIL_TEXT_RE.search(blob):
        return None
    return "teams"



def _people(task: dict) -> list:
    """Structured key_people entries, using the same JSON shape as _refs."""
    raw = (task.get("key_people") or "").strip()
    if not raw.startswith("[") and not raw.startswith("{"):
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(data, dict):
        data = [data]

    people = []
    for person in data:
        if not isinstance(person, dict):
            continue
        email = (person.get("email") or "").strip()
        name = (person.get("name") or "").strip()
        if email or name:
            people.append({"name": name or email, "email": email})
    return people


def _resolve_destination(task: dict, destination: dict) -> dict:
    """Best-effort audience binding for a new action row.

    A linked Teams thread is the delivery target, so it is stored in
    destination_ref. The Cowork conversation id is a different identifier and is
    never reused as an audience. Without a linked thread a single known person
    can seed the reference. If the source type carries no channel, the task's own
    wording is read as a last resort; failing that the channel stays unset so the
    user still chooses Teams or email.
    """
    people = _people(task)
    source_type = (task.get("source_type") or "").strip().lower()
    channel = _CHANNEL_BY_SOURCE.get(source_type)
    person = people[0] if len(people) == 1 else None

    if destination.get("conversation_id"):
        label = destination.get("audience_label") or "conversation"
        return {
            "delivery_channel": channel or "teams",
            "destination_ref": destination["conversation_id"],
            "destination_display": _conversation_display(label, people, person),
            "destination_source": "auto_source_url",
        }

    # Only ever a fallback: a channel-bearing source_type is stronger evidence
    # than prose, so it is never overridden here.
    inferred = _infer_channel_from_text(task) if channel is None else None

    # Last of all, the user's app-wide preference. Ordered below every piece of
    # evidence on purpose: a task from a Teams thread is a Teams message no
    # matter what the preference says. This only selects a voice register for
    # tasks that carry no signal at all; it can never bind an audience, because
    # destination_ref is not derived from it.
    fallback = default_delivery_channel()

    if person:
        return {
            "delivery_channel": channel or inferred or fallback,
            "destination_ref": person["email"] or person["name"],
            "destination_display": person["name"],
            "destination_source": "auto_key_people",
        }

    return {
        "delivery_channel": channel or inferred or fallback,
        "destination_ref": None,
        "destination_display": None,
        # Only claim text provenance when nothing else determined the binding,
        # so the audit trail never overstates what was read.
        "destination_source": "auto_task_text" if inferred else None,
    }


# At most this many names before collapsing to "+N". The card is one line and
# a wrapped destination row pushes the draft out of view.
_DEST_NAME_LIMIT = 2


def _conversation_display(label, people, person):
    """A human label for a bound conversation.

    "group chat" states the SHAPE of the audience but not who is in it, and the
    names were already on the task. Naming them also makes the broadcast
    warning concrete: "everyone in the chat" is easy to skim past, two named
    colleagues are not.

    The shape is always kept alongside the names, never replaced by them.
    """
    if person:
        return f"{person['name']} ({label})"

    names = [p["name"] for p in (people or []) if p.get("name")]
    if not names:
        return label

    shown = names[:_DEST_NAME_LIMIT]
    extra = len(names) - len(shown)
    if extra > 0:
        joined = ", ".join(shown) + f" +{extra}"
    elif len(shown) == 2:
        joined = f"{shown[0]} and {shown[1]}"
    else:
        joined = shown[0]
    return f"{label} with {joined}"


def _carry_forward_destination(task_id: int, resolved: dict) -> dict:
    """Keep a picker choice across a Redo.

    Auto-derived bindings are cheap to recompute, but a user_picker binding is
    explicit intent. A Redo builds a new row, so without this the user's chosen
    audience -- and the voice register that follows from it -- would silently
    revert to whatever the source URL implies.
    """
    previous = get_latest_task_action(task_id)
    if not previous or previous.get("destination_source") != "user_picker":
        return resolved
    return {
        "delivery_channel": previous.get("delivery_channel"),
        "destination_ref": previous.get("destination_ref"),
        "destination_display": previous.get("destination_display"),
        "destination_source": "user_picker",
    }


def _enrich(action: dict) -> dict:
    """Add the derived audience risk the UI needs but the row does not store.

    Also surfaces ``waiting_on_user`` WHILE A RUN IS STILL IN FLIGHT. Cowork can
    block mid-run asking the user something in the web app, and until it is
    answered nothing else happens. Phil hit this on task 2132: the card showed a
    spinner and "Working on your request" for 13 minutes while
    GET /v1/tasks had reported state=needs_user_input the whole time.

    The signal already existed; it was only read on a finished card, which is
    the one state where it does not matter. A spinner that means "waiting for
    you" is worse than no spinner, because it tells the user to keep waiting.
    """
    if action is None:
        return action

    action["interaction_request"] = _decode_interaction_request(
        action.get("blocked_question")
    )
    action["is_broadcast"] = action.get("destination_kind") in _BROADCAST_KINDS

    if action.get("state") in {"previewing", "executing"} and action.get(
        "conversation_id"
    ):
        try:
            status = (HANDOFF_FN or handoff_status)(action["conversation_id"])
        except Exception:  # noqa: BLE001
            status = None
        # Only ever claim the blocked case. An unreadable status must not be
        # rendered as "this is progressing normally".
        runtime_waiting = bool(
            status and status.get("waiting_on_user")
        )
        action["waiting_on_user"] = runtime_waiting
        if runtime_waiting and action.get("blocked_question") == "":
            # The answer POST succeeded; the cached task status can lag for up
            # to 30 seconds. Do not reopen the prompt during that stale window.
            action["waiting_on_user"] = False
            _schedule_blocked_question_recovery(action)
        elif runtime_waiting and action.get("blocked_question") is None:
            question = _schedule_blocked_question_recovery(action)
            if question:
                action["blocked_question"] = question
                action["interaction_request"] = question
        elif (
            status is not None
            and not runtime_waiting
            and action.get("blocked_question") is not None
        ):
            # Clear local-answer sentinels and externally answered questions
            # only after a readable runtime status confirms the run resumed.
            cleared = clear_blocked_question_if_unchanged(
                action["id"],
                action.get("blocked_question"),
                action.get("answered_interaction"),
            )
            if cleared:
                action["blocked_question"] = None
                action["answered_interaction"] = None
                action["interaction_request"] = None

    return action


def _decode_interaction_request(raw):
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _recover_blocked_question(action):
    action_id = action["id"]
    try:
        question = (BLOCKED_QUESTION_FN or read_blocked_question)(
            action["conversation_id"]
        )
        if question:
            encoded = json.dumps(question, separators=(",", ":"))
            stored = (
                BLOCKED_QUESTION_STORE_FN or set_blocked_question_if_missing
            )(action_id, encoded)
            return question if stored else None
        return None
    except Exception:
        logger.warning("could not recover blocked question", exc_info=True)
        return None
    finally:
        with _question_recovering_lock:
            _question_recovering.discard(action_id)


def _schedule_blocked_question_recovery(action):
    """Recover replay text off the Tornado request thread.

    Test seams run synchronously so unit tests stay deterministic. Production
    performs the streaming read on a daemon thread and the next card poll picks
    up the persisted question.
    """
    if BLOCKED_QUESTION_FN is not None:
        return _recover_blocked_question(action)

    action_id = action["id"]
    with _question_recovering_lock:
        if action_id in _question_recovering:
            return None
        _question_recovering.add(action_id)
    threading.Thread(
        target=_recover_blocked_question,
        args=(dict(action),),
        daemon=True,
        name=f"cowork-question-{action_id}",
    ).start()
    return None


def _finalise(action: dict) -> dict:
    """Fold a finished subprocess result into the action row.

    Called from GET rather than from the worker thread so the runner stays free
    of database coupling. A row whose poll never arrives is cleaned up at
    startup by models.recover_stuck_previews().
    """
    executing = action.get("state") == "executing"
    label = (
        execution_label(action["task_id"])
        if executing
        else preview_label(action["task_id"])
    )
    result = get_result(label)
    if result is None or is_running(label):
        return action

    parser = parse_execution_output if executing else parse_cowork_output
    parsed = parser(result["stdout"], stderr=result["stderr"])

    error = result.get("error") or parsed.get("error")
    failed = bool(error) or result.get("exit_code") not in (0, None)

    trace = parsed.get("tool_trace")
    if executing:
        confirmed = (
            bool(parsed.get("delivery_confirmed"))
            and _delivery_evidence_matches(action, parsed)
            and not failed
        )
        if not confirmed:
            detail = error or (
                "Cowork finished without positive delivery evidence."
            )
            error = (
                f"{detail} Delivery could not be confirmed. Check the "
                "destination before retrying."
            )
        fields = {
            "state": "executed" if confirmed else "execute_unconfirmed",
            "cost_credits": result.get("cost_credits"),
            "finding": parsed.get("finding") or parsed.get("raw_text"),
            "terminal_status": parsed.get("terminal_status"),
            "tool_trace": json.dumps(trace) if trace else None,
            "error": None if confirmed else error,
        }
        if parsed.get("conversation_id"):
            fields["conversation_id"] = parsed["conversation_id"]
        return update_task_action(
            action["id"], frozenset(fields), **fields
        ) or action

    fields = {
        "state": "failed" if failed else "ready",
        "cost_credits": result.get("cost_credits"),
        "finding": parsed.get("finding"),
        "draft": parsed.get("draft"),
        "terminal_status": parsed.get("terminal_status"),
        "tool_trace": json.dumps(trace) if trace else None,
        "error": error,
    }
    if parsed.get("conversation_id"):
        fields["conversation_id"] = parsed["conversation_id"]

    updated = update_task_action(action["id"], frozenset(fields), **fields)
    row = updated or action

    # Only an unambiguous 1:1 is safe to confirm without the user looking. Any
    # broadcast audience stays unconfirmed until it is reviewed in the picker.
    if (
        row.get("state") == "ready"
        and row.get("destination_kind") == "one_to_one"
        and not row.get("destination_confirmed_at")
        and row.get("destination_ref")
        and row.get("destination_display")
    ):
        confirmed = confirm_destination(
            row["id"],
            row.get("delivery_channel") or "teams",
            row["destination_ref"],
            row["destination_display"],
            row.get("destination_source") or "auto_source_url",
        )
        if confirmed:
            return confirmed
    return row


def _clean(action: dict) -> dict:
    for key in _NEVER_PERSIST:
        action.pop(key, None)
    return action


def _delivery_tool_matches_action(action: dict, name: str) -> bool:
    name = re.sub(r"[^a-z0-9]", "", str(name).lower())
    action_type = action.get("action_type")
    channel = action.get("delivery_channel")
    if action_type == "schedule-meeting":
        return "createevent" in name
    if action_type == "respond-email" or channel == "email":
        return any(
            marker in name
            for marker in ("sendemail", "replytomessage", "replyalltomessage")
        )
    return any(marker in name for marker in ("postmessage", "replytochannelmessage"))


def _tool_input_strings(value) -> list[str]:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return [value]
        return _tool_input_strings(decoded)
    if isinstance(value, dict):
        return [
            text
            for child in value.values()
            for text in _tool_input_strings(child)
        ]
    if isinstance(value, (list, tuple)):
        return [
            text
            for child in value
            for text in _tool_input_strings(child)
        ]
    return [] if value is None else [str(value)]


def _tool_input_values_for_keys(value, keys: frozenset[str]) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return []
    if isinstance(value, dict):
        matches = []
        for key, child in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized in keys:
                matches.extend(_tool_input_strings(child))
            else:
                matches.extend(_tool_input_values_for_keys(child, keys))
        return matches
    if isinstance(value, (list, tuple)):
        return [
            text
            for child in value
            for text in _tool_input_values_for_keys(child, keys)
        ]
    return []


def _tool_content_values(value) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return []
    if isinstance(value, dict):
        matches = []
        for key, child in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized in {"message", "body", "content"}:
                if isinstance(child, str):
                    matches.append(child)
                else:
                    matches.extend(_tool_content_values(child))
            elif isinstance(child, (dict, list, tuple)):
                matches.extend(_tool_content_values(child))
        return matches
    if isinstance(value, (list, tuple)):
        return [
            text
            for child in value
            for text in _tool_content_values(child)
        ]
    return []


def _delivery_evidence_matches(action: dict, parsed: dict) -> bool:
    successful_writes = [
        tool
        for tool in (parsed.get("tools") or [])
        if tool.get("ok") is True and _looks_like_write(tool.get("name"))
    ]
    if len(successful_writes) != 1:
        return False
    tool = successful_writes[0]
    if not _delivery_tool_matches_action(action, tool.get("name")):
        return False
    destination_values = _tool_input_values_for_keys(
        tool.get("input"),
        frozenset({
            "to", "cc", "bcc", "recipient", "recipients", "torecipient",
            "torecipients", "ccrecipient", "ccrecipients", "bccrecipient",
            "bccrecipients", "attendee", "attendees", "requiredattendee",
            "requiredattendees", "optionalattendee", "optionalattendees",
            "chat", "chatid", "channel", "channelid", "conversation",
            "conversationid",
        }),
    )
    destination = str(action.get("destination_ref") or "").strip().lower()
    normalized_destinations = [
        value.strip().lower() for value in destination_values if value.strip()
    ]
    if "@" in destination:
        normalized_destinations = [
            value for value in normalized_destinations if "@" in value
        ]
    if not destination or normalized_destinations != [destination]:
        return False
    draft = (action.get("draft_edited") or action.get("draft") or "").strip()
    if action.get("action_type") == "schedule-meeting":
        # The card does not yet approve structured subject/time/location fields.
        # The write may run, but cannot be called confirmed from body evidence.
        return False
    content_values = {
        value.strip() for value in _tool_content_values(tool.get("input"))
    }
    if (
        action.get("action_type") == "respond-email"
        or action.get("delivery_channel") == "email"
    ):
        lines = draft.splitlines()
        if not lines or not lines[0].lower().startswith("subject:"):
            return False
        subject = lines[0].split(":", 1)[1].strip()
        body = "\n".join(lines[1:]).strip()
        subjects = {
            value.strip()
            for value in _tool_input_values_for_keys(
                tool.get("input"), frozenset({"subject"})
            )
        }
        attachments = _tool_input_values_for_keys(
            tool.get("input"),
            frozenset({"attachment", "attachments", "file", "files"}),
        )
        return (
            bool(subject)
            and bool(body)
            and subjects == {subject}
            and content_values == {body}
            and not attachments
        )
    if not draft or not content_values or content_values != {draft}:
        return False
    return True


class CoworkHandler(tornado.web.RequestHandler):
    """Write-barriered preview lifecycle for one task."""

    def set_default_headers(self):
        self.set_header("Content-Type", "application/json")

    def _body(self):
        try:
            return json.loads(self.request.body or b"{}")
        except (json.JSONDecodeError, TypeError):
            return None

    def _fail(self, code, message):
        self.set_status(code)
        self.write(json.dumps({"error": message}))

    # ── POST ──

    def post(self, task_id):
        tid = int(task_id)
        task = get_task(tid)
        if not task:
            return self._fail(404, "Not found")

        body = self._body()
        if body is None:
            return self._fail(400, "Invalid JSON")

        # 409 is gated on the in-memory registry, never on task_actions.state:
        # the registry self-heals when a process exits, a database row does not.
        latest = get_latest_task_action(tid)
        if (
            is_running(preview_label(tid))
            or is_running(execution_label(tid))
            or (latest and latest.get("state") == "executing")
        ):
            return self._fail(409, "A Cowork action is already running for this task")

        redirect_text = (body.get("redirect_text") or "").strip() or None
        interaction_mode = body.get("interaction_mode")
        if interaction_mode is None:
            interaction_mode = (
                latest.get("interaction_mode") if latest else "interaction"
            ) or "interaction"
        if (
            not isinstance(interaction_mode, str)
            or interaction_mode not in {"interaction", "no_interaction"}
        ):
            return self._fail(400, "Invalid interaction mode")
        interaction_mode = "interaction"
        destination = parse_source_url(task.get("source_url"))
        resolved = _carry_forward_destination(
            tid, _resolve_destination(task, destination)
        )

        prompt = compose_prompt(
            task,
            destination,
            redirect_text=redirect_text,
            delivery_channel=resolved.get("delivery_channel"),
            interaction_mode=interaction_mode,
        )

        # Minted BEFORE the run so Stop is addressable from the first second.
        # Cancellation targets POST /v1/conversations/{id}/pause, and this id
        # used to be written only when the run FINISHED, so pressing Stop
        # during "Preparing workspace" had nothing to address: the run kept
        # going, kept spending credits, and the still-live worker put the row
        # back to 'previewing'. Only meaningful on the API transport; harmless
        # otherwise, because the runner mints its own if this is None.
        conversation_id = new_conversation_id() if api_transport_enabled() else None

        # A Redo is a NEW row, never an update: the original intent survives and
        # the correction chain stays auditable.
        action = create_task_action(
            tid,
            action_type=task.get("action_type") or "general",
            intent=task.get("coaching_text"),
            notes_snapshot=task.get("user_notes"),
            redirect_text=redirect_text,
            composed_prompt=prompt,
            destination_kind=destination.get("kind"),
            island_url=get_cached_cowork_island(),
            conversation_id=conversation_id,
            interaction_mode=interaction_mode,
            **resolved,
        )

        try:
            start_preview(
                tid,
                prompt,
                refs=_refs(task),
                spawn=SPAWN,
                log_dir=LOG_DIR_OVERRIDE,
                conversation_id=conversation_id,
            )
        except AlreadyRunning:
            return self._fail(409, "A preview is already running for this task")
        except Exception as exc:  # pragma: no cover - defensive
            update_task_action(
                action["id"],
                frozenset({"state", "error"}),
                state="failed",
                error=f"Could not start Cowork: {exc}",
            )
            return self._fail(500, f"Could not start Cowork: {exc}")

        self.set_status(202)
        self.write(json.dumps({"action": _enrich(_clean(action))}))

    # ── GET ──

    def get(self, task_id):
        tid = int(task_id)
        if self.get_argument("history", None):
            return self.write(
                json.dumps(
                    {"actions": [_enrich(_clean(a)) for a in list_task_actions(tid)]}
                )
            )

        action = get_latest_task_action(tid)
        if not action:
            return self._fail(404, "No Cowork preview for this task")

        pre_state = action["state"]
        if action["state"] in {"previewing", "executing"}:
            action = _finalise(action)

        if self.get_argument("mark_seen", None) and pre_state == "ready":
            action = mark_task_action_seen(action["id"])

        payload = _enrich(_clean(action))
        # Live liveness from the CLI's stderr. A preview runs for a median of
        # 119s, so the card needs something to say while it waits.
        label = (
            execution_label(tid)
            if action.get("state") == "executing"
            else preview_label(tid)
        )
        payload["progress"] = get_progress(label)

        # What happened after "Open in Cowork". Only meaningful once a preview
        # has finished and been handed over, so a run still in `previewing`
        # never triggers a lookup. Cached in the runner, and additive: when it
        # is None the card renders exactly as it does today.
        #
        # Guarded here as well as inside handoff_status, because this is
        # decoration on a card that is already complete without it and must
        # never be able to turn a working preview into a 500.
        if action.get("state") == "ready" and action.get("conversation_id"):
            try:
                lookup = HANDOFF_FN or handoff_status
                handoff = lookup(action["conversation_id"])
            except Exception:  # noqa: BLE001
                logging.getLogger(__name__).debug(
                    "handoff lookup failed", exc_info=True
                )
                handoff = None
            if handoff:
                payload["handoff"] = handoff

        self.write(json.dumps({"action": payload}))

    # ── DELETE ──

    def delete(self, task_id):
        """Stop a preview that is in flight.

        The ONLY thing here that reaches out to Cowork and changes something,
        and it strictly REDUCES what can happen: it stops work, it cannot start
        or send anything. That is why it is safe to expose while there is still
        deliberately no execute route.

        `proc.kill()` on the subprocess path only kills our local process; the
        server-side run keeps going and keeps spending credits. Cancellation is
        the capability the API transport adds, verified live: the run stopped
        3.0s after the request.
        """
        tid = int(task_id)
        action = get_latest_task_action(tid)
        if not action:
            return self._fail(404, "No Cowork preview for this task")
        if action["state"] != "previewing":
            return self._fail(409, "That preview is not running")

        conversation_id = action.get("conversation_id")
        stopped = False
        if conversation_id:
            stopped = (CANCEL_FN or cancel_run)(conversation_id)

        # Local bookkeeping happens either way. If the remote cancel did not
        # land we still stop showing a spinner, but we say so rather than
        # claiming we stopped something we did not.
        updated = update_task_action(
            action["id"],
            frozenset({"state", "error"}),
            state="failed",
            error=None if stopped else (
                "Stop was requested but Cowork did not confirm it. The run may "
                "still be finishing on the server."
            ),
        )
        self.write(json.dumps({
            "action": _enrich(_clean(updated or action)),
            "stopped": stopped,
        }))

    # ── PUT ──

    def put(self, task_id):
        action = get_latest_task_action(int(task_id))
        if not action:
            return self._fail(404, "No Cowork preview for this task")

        body = self._body()
        if body is None:
            return self._fail(400, "Invalid JSON")

        if not any(key in ACTION_EDITABLE_FIELDS for key in body):
            return self._fail(400, "No editable fields supplied")
        if action.get("state") not in {"previewing", "ready"}:
            return self._fail(409, "A completed action cannot be edited.")
        updated = update_task_action(
            action["id"],
            ACTION_EDITABLE_FIELDS,
            required_state=action["state"],
            **body,
        )
        if updated is None:
            return self._fail(409, "The draft changed state before it was saved.")

        self.write(json.dumps({"action": _enrich(_clean(updated))}))


class CoworkExecuteHandler(tornado.web.RequestHandler):
    """Execute the exact draft and destination approved in Riveter."""

    def set_default_headers(self):
        self.set_header("Content-Type", "application/json")

    def _fail(self, code, message):
        self.set_status(code)
        self.write(json.dumps({"error": message}))

    def post(self, task_id):
        tid = int(task_id)
        if self.request.headers.get("X-Riveter-Action") != "confirm":
            return self._fail(403, "Direct actions require Riveter confirmation.")
        try:
            body = json.loads(self.request.body or b"{}")
        except (json.JSONDecodeError, TypeError):
            return self._fail(400, "Invalid JSON")
        approved_snapshot = body.get("approved_snapshot")
        if not isinstance(approved_snapshot, dict):
            return self._fail(400, "The reviewed action snapshot is required.")
        task = get_task(tid)
        if not task:
            return self._fail(404, "Not found")

        transport_enabled = (
            EXECUTE_TRANSPORT_ENABLED_FN or api_transport_enabled
        )
        if not transport_enabled():
            return self._fail(
                409, "Direct actions require the Cowork API transport."
            )

        parent = get_latest_task_action(tid)
        if not parent or parent.get("state") != "ready":
            return self._fail(409, "There is no approved draft ready to send.")
        if not parent.get("conversation_id"):
            return self._fail(409, "This draft has no Cowork conversation.")
        if not parent.get("destination_confirmed_at"):
            return self._fail(409, "Review and confirm the destination first.")
        if not (
            (parent.get("draft_edited") or parent.get("draft") or "").strip()
        ):
            return self._fail(409, "The final draft is empty.")

        action = create_execution_action(parent["id"], approved_snapshot)
        if not action:
            return self._fail(
                409,
                "The draft or destination changed after review, or this action "
                "is already being handled. Review it again before sending.",
            )
        prompt = compose_execution_prompt(action)
        action = update_task_action(
            action["id"],
            frozenset({"composed_prompt"}),
            composed_prompt=prompt,
        ) or action

        try:
            execute = EXECUTE_FN or start_execution
            execute(
                tid,
                prompt,
                action["conversation_id"],
                approval_kind=(
                    "calendar"
                    if action.get("action_type") == "schedule-meeting"
                    else action.get("delivery_channel")
                ),
                approved_snapshot=approved_snapshot,
                log_dir=LOG_DIR_OVERRIDE,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("could not start approved Cowork action")
            message = (
                f"Could not start the action: {exc}. Delivery could not be "
                "confirmed. Check the destination before retrying."
            )
            action = update_task_action(
                action["id"],
                frozenset({"state", "error"}),
                state="execute_unconfirmed",
                error=message,
            ) or action
            return self._fail(502, message)

        self.set_status(202)
        self.write(json.dumps({"action": _enrich(_clean(action))}))


class CoworkRefineHandler(tornado.web.RequestHandler):
    """POST /api/tasks/<id>/cowork/refine — one more turn, same conversation.

    A Redo rebuilds the whole prompt and starts a BRAND NEW Cowork
    conversation, so it re-researches M365 from zero: measured at 27s to 6
    minutes and 69 to 355 credits every time. A refine turn continues the
    existing conversation, which still holds that research, and came back in
    about 30s in a live check.

    The barrier is rebuilt and re-sent on this turn (it travels per request),
    so a "send it now" instruction typed here is intercepted. Only the separate
    execute route can run an approved write without that barrier.
    """

    def _fail(self, code, message):
        self.set_status(code)
        self.write(json.dumps({"error": message}))

    def post(self, task_id):
        tid = int(task_id)
        task = get_task(tid)
        if not task:
            return self._fail(404, "Task not found")

        action = get_latest_task_action(tid)
        if not action:
            return self._fail(404, "No Cowork preview for this task")
        if (
            action["state"] in {"previewing", "executing"}
            or is_running(execution_label(tid))
        ):
            return self._fail(409, "A Cowork action is already running for this task")

        conversation_id = action.get("conversation_id")
        if not conversation_id:
            # Subprocess-produced rows carry no conversation id, so there is
            # nothing to continue. The UI hides the affordance in that case;
            # this is the server-side guard.
            return self._fail(
                409,
                "This preview cannot be continued. Use Redo to start a new "
                "Cowork conversation.",
            )

        try:
            body = json.loads(self.request.body or b"{}")
        except (json.JSONDecodeError, TypeError):
            return self._fail(400, "Invalid JSON")

        instruction = (body.get("instruction") or "").strip()
        if not instruction:
            return self._fail(400, "An instruction is required")

        # A refine is a NEW row for the same reason a Redo is: the original
        # attempt survives and the correction chain stays auditable. The
        # audience binding is carried forward wholesale — it was resolved on a
        # ready row, so re-deriving it could silently change who this is for.
        new_action = create_task_action(
            tid,
            action_type=action.get("action_type") or "general",
            intent=action.get("intent"),
            notes_snapshot=action.get("notes_snapshot"),
            redirect_text=instruction,
            composed_prompt=compose_refine_prompt(
                instruction,
                interaction_mode="interaction",
            ),
            conversation_id=conversation_id,
            island_url=action.get("island_url"),
            parent_action_id=action["id"],
            destination_kind=action.get("destination_kind"),
            destination_ref=action.get("destination_ref"),
            destination_display=action.get("destination_display"),
            destination_source=action.get("destination_source"),
            destination_confirmed_at=action.get("destination_confirmed_at"),
            delivery_channel=action.get("delivery_channel"),
            interaction_mode="interaction",
        )

        try:
            continue_preview(
                tid,
                conversation_id,
                instruction,
                interaction_mode="interaction",
                log_dir=LOG_DIR_OVERRIDE,
            )
        except AlreadyRunning:
            return self._fail(409, "A preview is already running for this task")
        except Exception as exc:  # pragma: no cover - defensive
            update_task_action(
                new_action["id"],
                frozenset({"state", "error"}),
                state="failed",
                error=f"Could not continue the conversation: {exc}",
            )
            return self._fail(500, f"Could not continue the conversation: {exc}")

        self.set_status(202)
        self.write(json.dumps({"action": _enrich(_clean(new_action))}))


class CoworkAnswerHandler(tornado.web.RequestHandler):
    """POST an answer into a preview currently blocked on the user."""

    def _fail(self, code, message):
        self.set_status(code)
        self.write(json.dumps({"error": message}))

    async def post(self, task_id):
        tid = int(task_id)
        action = get_latest_task_action(tid)
        if not action:
            return self._fail(404, "No Cowork preview for this task")

        action = _enrich(action)
        if action.get("state") not in {"previewing", "executing"}:
            return self._fail(409, "That Cowork turn is not running")
        if not action.get("conversation_id"):
            return self._fail(409, "That preview has no Cowork conversation")
        if not action.get("waiting_on_user"):
            return self._fail(409, "Cowork is not waiting for an answer")

        try:
            body = json.loads(self.request.body or b"{}")
        except (json.JSONDecodeError, TypeError):
            return self._fail(400, "Invalid JSON")
        answers = body.get("answers")
        invocation_id = str(body.get("invocation_id") or "")
        interaction = action.get("interaction_request")
        if not interaction:
            return self._fail(409, "The Cowork question is still loading")
        if invocation_id != str(interaction.get("invocation_id") or ""):
            return self._fail(409, "That Cowork question is no longer current")
        expected_ids = {
            str(question.get("id"))
            for question in interaction.get("questions") or []
            if question.get("id")
        }
        if not isinstance(answers, dict):
            return self._fail(400, "Answers are required")
        cleaned_answers = {
            str(key): str(value).strip()
            for key, value in answers.items()
            if str(key) in expected_ids and str(value).strip()
        }
        if set(cleaned_answers) != expected_ids:
            return self._fail(400, "Every Cowork question requires an answer")

        previous_question = action.get("blocked_question")
        if not claim_blocked_question_answer(action["id"], previous_question):
            return self._fail(409, "That Cowork question was already answered")

        try:
            await asyncio.to_thread(
                (ANSWER_FN or answer_interaction),
                action["conversation_id"],
                interaction["invocation_id"],
                cleaned_answers,
            )
        except Exception as exc:
            if isinstance(exc, CoworkAnswerRejected):
                restore_claimed_blocked_question(
                    action["id"], previous_question
                )
                logger.error("Cowork rejected interaction answer", exc_info=True)
                return self._fail(
                    exc.status_code,
                    f"Cowork rejected the answer: {exc}",
                )
            current_interaction = None
            try:
                current_interaction = await asyncio.to_thread(
                    (BLOCKED_QUESTION_FN or read_blocked_question),
                    action["conversation_id"],
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Could not reconcile Cowork interaction after answer failure",
                    exc_info=True,
                )
            if isinstance(current_interaction, dict):
                if current_interaction != interaction:
                    set_blocked_question_if_missing(
                        action["id"],
                        json.dumps(current_interaction, separators=(",", ":")),
                    )
            logger.error("could not answer Cowork interaction", exc_info=True)
            return self._fail(502, f"Could not send the answer to Cowork: {exc}")

        action = get_latest_task_action(tid) or action
        if action.get("state") in {"previewing", "executing"}:
            action["blocked_question"] = ""
            action["answered_interaction"] = previous_question
            action["interaction_request"] = None
            action["waiting_on_user"] = False
        self.set_status(202)
        self.write(json.dumps({"action": _clean(action)}))


class CoworkDestinationHandler(tornado.web.RequestHandler):
    """Confirm who a ready preview is addressed to. Nothing is delivered here.

    Phase 1 remains preview only: this records a reviewed audience so a future
    execution path has something exact to bind to, and so the card can warn
    before a draft is copied to a broadcast conversation.
    """

    def _fail(self, code, message):
        self.set_status(code)
        self.write(json.dumps({"error": message}))

    def post(self, task_id):
        action = get_latest_task_action(int(task_id))
        if not action:
            return self._fail(404, "No Cowork preview for this task")

        try:
            body = json.loads(self.request.body or b"{}")
        except (json.JSONDecodeError, TypeError):
            return self._fail(400, "Invalid JSON")

        channel = (body.get("delivery_channel") or "").strip().lower()
        ref = (body.get("destination_ref") or "").strip()
        display = (body.get("destination_display") or "").strip()
        if channel not in DELIVERY_CHANNELS or not ref or not display:
            return self._fail(
                400,
                "delivery_channel, destination_ref and destination_display "
                "are required",
            )

        # Provenance is server owned: anything arriving over HTTP is a picker
        # confirmation, never an automatic resolution.
        confirmed = confirm_destination(
            action["id"], channel, ref, display, "user_picker"
        )
        if not confirmed:
            return self._fail(409, "Only a ready preview can be confirmed")

        self.write(json.dumps({"action": _enrich(_clean(confirmed))}))
