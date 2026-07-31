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

import tornado.web

from ..models import (
    ACTION_EDITABLE_FIELDS,
    create_task_action,
    get_latest_task_action,
    get_task,
    list_task_actions,
    update_task_action,
)
from ..services.cowork_runner import (
    AlreadyRunning,
    compose_prompt,
    get_result,
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
    return updated or action


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

        prompt = compose_prompt(task, destination, redirect_text=redirect_text)

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
            destination_ref=destination.get("counterparty_id"),
            conversation_id=destination.get("conversation_id"),
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
        self.write(json.dumps({"action": _clean(action)}))

    # ── GET ──

    def get(self, task_id):
        tid = int(task_id)
        if self.get_argument("history", None):
            return self.write(
                json.dumps({"actions": [_clean(a) for a in list_task_actions(tid)]})
            )

        action = get_latest_task_action(tid)
        if not action:
            return self._fail(404, "No Cowork preview for this task")

        if action["state"] == "previewing":
            action = _finalise(action)

        self.write(json.dumps({"action": _clean(action)}))

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

        self.write(json.dumps({"action": _clean(updated)}))
