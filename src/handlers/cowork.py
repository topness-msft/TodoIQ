"""Cowork preview API — PHASE 1 IS PREVIEW ONLY.

There is deliberately **no execute route** in this module. That absence is the
only hard safety guarantee in Phase 1: the tool denylist passed to the CLI is
defence in depth over an open set (see cowork_runner), but a route that cannot
be reached cannot send anything.

Routes:
    POST /api/tasks/<id>/cowork    start a preview (202); 409 if already running
    GET  /api/tasks/<id>/cowork    latest attempt; ?history=1 for the full chain
    PUT  /api/tasks/<id>/cowork    save the user's edited draft
"""

import json
import re

import tornado.web

from ..models import (
    ACTION_EDITABLE_FIELDS,
    DELIVERY_CHANNELS,
    confirm_destination,
    create_task_action,
    get_latest_task_action,
    get_task,
    list_task_actions,
    mark_task_action_seen,
    update_task_action,
)
from ..services.cowork_runner import (
    AlreadyRunning,
    compose_prompt,
    get_result,
    get_progress,
    get_cached_cowork_island,
    is_running,
    parse_cowork_output,
    parse_source_url,
    preview_label,
    start_preview,
)

# Test seams. Production leaves both None so the runner uses its own defaults.
SPAWN = None
LOG_DIR_OVERRIDE = None

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
            "destination_display": (
                f"{person['name']} ({label})" if person else label
            ),
            "destination_source": "auto_source_url",
        }

    # Only ever a fallback: a channel-bearing source_type is stronger evidence
    # than prose, so it is never overridden here.
    inferred = _infer_channel_from_text(task) if channel is None else None

    if person:
        return {
            "delivery_channel": channel or inferred,
            "destination_ref": person["email"] or person["name"],
            "destination_display": person["name"],
            "destination_source": "auto_key_people",
        }

    return {
        "delivery_channel": channel or inferred,
        "destination_ref": None,
        "destination_display": None,
        # Only claim text provenance when nothing else determined the binding,
        # so the audit trail never overstates what was read.
        "destination_source": "auto_task_text" if inferred else None,
    }


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
    """Add the derived audience risk the UI needs but the row does not store."""
    if action is not None:
        action["is_broadcast"] = action.get("destination_kind") in _BROADCAST_KINDS
    return action


def _finalise(action: dict) -> dict:
    """Fold a finished subprocess result into the action row.

    Called from GET rather than from the worker thread so the runner stays free
    of database coupling. A row whose poll never arrives is cleaned up at
    startup by models.recover_stuck_previews().
    """
    label = preview_label(action["task_id"])
    result = get_result(label)
    if result is None or is_running(label):
        return action

    parsed = parse_cowork_output(result["stdout"], stderr=result["stderr"])

    error = result.get("error") or parsed.get("error")
    failed = bool(error) or result.get("exit_code") not in (0, None)

    trace = parsed.get("tool_trace")
    fields = {
        "state": "failed" if failed else "ready",
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


class CoworkHandler(tornado.web.RequestHandler):
    """Preview lifecycle for one task. No write path exists."""

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
        if is_running(preview_label(tid)):
            return self._fail(409, "A preview is already running for this task")

        redirect_text = (body.get("redirect_text") or "").strip() or None
        destination = parse_source_url(task.get("source_url"))
        resolved = _carry_forward_destination(
            tid, _resolve_destination(task, destination)
        )

        prompt = compose_prompt(
            task,
            destination,
            redirect_text=redirect_text,
            delivery_channel=resolved.get("delivery_channel"),
        )

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
            **resolved,
        )

        try:
            start_preview(
                tid,
                prompt,
                refs=_refs(task),
                spawn=SPAWN,
                log_dir=LOG_DIR_OVERRIDE,
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
        if action["state"] == "previewing":
            action = _finalise(action)

        if self.get_argument("mark_seen", None) and pre_state == "ready":
            action = mark_task_action_seen(action["id"])

        payload = _enrich(_clean(action))
        # Live liveness from the CLI's stderr. A preview runs for a median of
        # 119s, so the card needs something to say while it waits.
        payload["progress"] = get_progress(preview_label(tid))
        self.write(json.dumps({"action": payload}))

    # ── PUT ──

    def put(self, task_id):
        action = get_latest_task_action(int(task_id))
        if not action:
            return self._fail(404, "No Cowork preview for this task")

        body = self._body()
        if body is None:
            return self._fail(400, "Invalid JSON")

        updated = update_task_action(action["id"], ACTION_EDITABLE_FIELDS, **body)
        if updated is None:
            return self._fail(400, "No editable fields supplied")

        self.write(json.dumps({"action": _enrich(_clean(updated))}))


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
