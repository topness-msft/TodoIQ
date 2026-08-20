"""Task CRUD and lifecycle operations for TodoNess."""

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from .db import get_connection, init_db
from .services import person_identity as _person_identity

logger = logging.getLogger(__name__)

# Valid status transitions
VALID_TRANSITIONS = {
    "suggested": {"active", "waiting", "snoozed", "dismissed", "deleted"},
    "active": {"in_progress", "waiting", "snoozed", "completed", "dismissed", "deleted"},
    "in_progress": {"active", "waiting", "snoozed", "completed", "deleted"},
    "waiting": {"active", "in_progress", "snoozed", "completed", "deleted"},
    "snoozed": {"active", "completed", "dismissed", "deleted"},
    "completed": {"active", "deleted"},
    "dismissed": {"active", "suggested", "deleted"},
    "deleted": {"active"},
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return dict(row)


def ensure_db():
    """Initialize the database if needed."""
    conn = get_connection()
    init_db(conn)
    conn.close()


# ── Source-ID Fuzzy Dedup ──────────────────────────────────────────────────

_STOP_WORDS = frozenset(
    {'a', 'an', 'the', 'to', 'for', 'of', 'on', 'in', 'at', 'and', 'or',
     'with', 'my', 're', 'fwd', 'up', 'is', 'be', 'do', 'it', 'we'}
)


def normalize_source_id(source_id: str) -> tuple[str, str, set[str]] | None:
    """Split a source_id into (source_type, person_alias, keyword_tokens).

    Returns None if the source_id doesn't have the expected format.
    """
    if not source_id:
        return None
    parts = source_id.split("::")
    if len(parts) < 3:
        return None
    source_type = parts[0].lower().strip()
    person_raw = parts[1].lower().strip()
    person_alias = person_raw.split("@")[0] if "@" in person_raw else person_raw
    keyword_str = "::".join(parts[2:]).lower()
    tokens = {w for w in keyword_str.split() if w not in _STOP_WORDS and len(w) > 1}
    return source_type, person_alias, tokens


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _person_match(a: str, b: str) -> bool:
    """Check if two person aliases refer to the same person.

    After normalizing (strip @domain), compare exact match only.
    The refresh commands ask WorkIQ to resolve all aliases to
    first.last@microsoft.com format, so exact match is reliable
    for new tasks. Legacy tasks may still have short aliases.
    """
    if a == b:
        return True
    return False


def find_similar_source(
    conn: sqlite3.Connection,
    source_id: str,
    source_type: str | None = None,
    threshold: float = 0.5,
) -> dict | None:
    """Find an existing non-deleted task whose source_id fuzzy-matches.

    Returns the matching task dict, or None.
    """
    parsed = normalize_source_id(source_id)
    if parsed is None:
        return None
    src_type, person_alias, new_tokens = parsed

    # Fetch candidate tasks: same source_type, not deleted, having a source_id
    type_filter = source_type or src_type
    rows = conn.execute(
        "SELECT * FROM tasks WHERE source_type = ? AND status != 'deleted' AND source_id IS NOT NULL",
        (type_filter,),
    ).fetchall()

    for row in rows:
        existing_parsed = normalize_source_id(row["source_id"])
        if existing_parsed is None:
            continue
        ex_type, ex_person, ex_tokens = existing_parsed
        # Person match: exact, or one alias contains the other's last-name part
        # Handles spant vs saurabh.pant, phtopnes vs peter.topness
        if not _person_match(person_alias, ex_person):
            continue
        if _jaccard(new_tokens, ex_tokens) >= threshold:
            return dict(row)
    return None


# ── Task CRUD ──────────────────────────────────────────────────────────────

def create_task(
    title: str,
    description: str = "",
    status: str = "active",
    parse_status: str = "parsed",
    raw_input: str | None = None,
    priority: int = 3,
    due_date: str | None = None,
    committed_date: str | None = None,
    source_type: str = "manual",
    source_id: str | None = None,
    source_url: str | None = None,
    source_date: str | None = None,
    source_snippet: str | None = None,
    coaching_text: str | None = None,
    action_type: str = "general",
    skill_output: str | None = None,
    key_people: str | None = None,
    related_meeting: str | None = None,
    user_notes: str = "",
) -> dict:
    """Create a new task and return it as a dict."""
    conn = get_connection()
    try:
        # ── Dedup: exact match first, then fuzzy fallback ──
        if source_id:
            exact = conn.execute(
                "SELECT * FROM tasks WHERE source_id = ? AND status != 'deleted'",
                (source_id,),
            ).fetchone()
            if exact:
                logger.debug("Exact source_id match → existing task #%s", exact["id"])
                return dict(exact)

            fuzzy_match = find_similar_source(conn, source_id, source_type)
            if fuzzy_match:
                logger.info(
                    "Fuzzy source_id dedup: new '%s' matched existing task #%s '%s'",
                    source_id, fuzzy_match["id"], fuzzy_match["source_id"],
                )
                return fuzzy_match

        now = _now()
        cursor = conn.execute(
            """INSERT INTO tasks
               (title, description, status, parse_status, raw_input, priority,
                due_date, committed_date, source_type, source_id, source_url,
                source_date, source_snippet, coaching_text, action_type, skill_output, key_people,
                related_meeting, user_notes, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                title, description, status, parse_status, raw_input, priority,
                due_date, committed_date, source_type, source_id, source_url,
                source_date, source_snippet, coaching_text, action_type, skill_output, key_people,
                related_meeting, user_notes, now, now,
            ),
        )
        task_id = cursor.lastrowid
        _person_identity.derive_task_persons(
            conn,
            task_id,
            key_people_json=key_people,
            source_id=source_id,
        )
        conn.commit()
        return get_task(task_id, conn)
    finally:
        conn.close()


def get_task(task_id: int, conn: sqlite3.Connection | None = None) -> dict | None:
    """Get a single task by ID."""
    close = conn is None
    if close:
        conn = get_connection()
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if close:
        conn.close()
    return _row_to_dict(row)


def list_tasks(
    status: str | None = None,
    parse_status: str | None = None,
    exclude_statuses: list[str] | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict]:
    """List tasks with optional filters, ordered by priority then created_at."""
    conn = get_connection()
    try:
        clauses, params = [], []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if parse_status:
            clauses.append("parse_status = ?")
            params.append(parse_status)
        if exclude_statuses:
            placeholders = ",".join("?" for _ in exclude_statuses)
            clauses.append(f"status NOT IN ({placeholders})")
            params.extend(exclude_statuses)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        rows = conn.execute(
            f"""
            SELECT t.*,
              (SELECT state FROM task_actions
               WHERE task_id = t.id
                 AND cowork_revision = t.cowork_revision
                 AND action_type = t.action_type
               ORDER BY id DESC LIMIT 1) AS cw_state,
              (SELECT seen_at FROM task_actions
               WHERE task_id = t.id
                 AND cowork_revision = t.cowork_revision
                 AND action_type = t.action_type
               ORDER BY id DESC LIMIT 1) AS cw_seen_at
            FROM tasks t {where}
            ORDER BY priority ASC, created_at DESC LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_task(task_id: int, **fields) -> dict | None:
    """Update arbitrary fields on a task. Returns updated task or None."""
    if not fields:
        return get_task(task_id)
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "SELECT * FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        if not current:
            conn.rollback()
            return None
        if any(
            key in fields and fields[key] != current[key]
            for key in _COWORK_REVISION_FIELDS
        ):
            fields["cowork_revision"] = current["cowork_revision"] + 1
        fields["updated_at"] = _now()
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [task_id]
        conn.execute(f"UPDATE tasks SET {set_clause} WHERE id = ?", values)
        if {"key_people", "source_id"} & set(fields):
            row = conn.execute(
                "SELECT key_people,source_id FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if row:
                _person_identity.derive_task_persons(
                    conn,
                    task_id,
                    key_people_json=row["key_people"],
                    source_id=row["source_id"],
                )
        conn.commit()
        return get_task(task_id, conn)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def update_task_for_action_type(task_id: int, **fields) -> dict | None:
    """Update task fields and retire prior Cowork generations on a real type change."""
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if not row:
            conn.rollback()
            return None
        task = dict(row)
        requested_type = fields.get("action_type", task.get("action_type"))
        type_changed = requested_type != task.get("action_type")
        input_changed = any(
            key in fields and fields[key] != task.get(key)
            for key in _COWORK_REVISION_FIELDS
        )
        if type_changed or input_changed:
            in_flight = conn.execute(
                """
                SELECT state FROM task_actions
                WHERE task_id = ?
                  AND cowork_revision = ?
                  AND action_type = ?
                  AND state IN ('executing','execute_unconfirmed')
                ORDER BY id DESC LIMIT 1
                """,
                (
                    task_id,
                    task.get("cowork_revision", 0),
                    task.get("action_type"),
                ),
            ).fetchone()
            if in_flight:
                conn.rollback()
                raise ValueError(
                    "Cannot change action type while an approved action may be delivering."
                )
            fields["cowork_revision"] = task.get("cowork_revision", 0) + 1
        fields["updated_at"] = _now()
        set_clause = ", ".join(f"{key} = ?" for key in fields)
        conn.execute(
            f"UPDATE tasks SET {set_clause} WHERE id = ?",
            (*fields.values(), task_id),
        )
        if {"key_people", "source_id"} & set(fields):
            row = conn.execute(
                "SELECT key_people,source_id FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if row:
                _person_identity.derive_task_persons(
                    conn,
                    task_id,
                    key_people_json=row["key_people"],
                    source_id=row["source_id"],
                )
        conn.commit()
        updated = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return _row_to_dict(updated)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def delete_task(task_id: int) -> bool:
    """Delete a task. Returns True if a row was deleted."""
    conn = get_connection()
    try:
        cursor = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


# ── Task Lifecycle ─────────────────────────────────────────────────────────

def transition_task(task_id: int, new_status: str) -> dict | None:
    """Transition a task to a new status if the transition is valid."""
    task = get_task(task_id)
    if task is None:
        return None
    current = task["status"]
    if new_status not in VALID_TRANSITIONS.get(current, set()):
        raise ValueError(
            f"Cannot transition from '{current}' to '{new_status}'. "
            f"Valid: {VALID_TRANSITIONS.get(current, set())}"
        )
    return update_task(task_id, status=new_status)


def promote_task(task_id: int) -> dict | None:
    """Promote a suggested task to active."""
    return transition_task(task_id, "active")


def dismiss_task(task_id: int) -> dict | None:
    """Dismiss a suggested or active task."""
    return transition_task(task_id, "dismissed")


def complete_task(task_id: int) -> dict | None:
    """Mark a task as completed."""
    task = get_task(task_id)
    if task is None:
        return None
    if task["status"] in ("active", "in_progress", "waiting", "snoozed"):
        return update_task(task_id, status="completed", snoozed_until=None)
    raise ValueError(f"Cannot complete task in status '{task['status']}'")


def start_task(task_id: int) -> dict | None:
    """Move an active task to in_progress."""
    return transition_task(task_id, "in_progress")


def snooze_task(
    task_id: int,
    minutes: int | None = None,
    until: str | None = None,
) -> dict | None:
    """Snooze a task. Provide either minutes or an ISO timestamp for until."""
    if until:
        # Normalize to consistent ISO format for reliable SQLite comparison
        try:
            dt = datetime.fromisoformat(until.replace("Z", "+00:00"))
            snoozed_until = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, AttributeError):
            snoozed_until = until
    else:
        mins = minutes or 60
        wake_time = datetime.now(timezone.utc) + timedelta(minutes=mins)
        snoozed_until = wake_time.strftime("%Y-%m-%dT%H:%M:%SZ")
    task = get_task(task_id)
    if task is None:
        return None
    current = task["status"]
    if "snoozed" not in VALID_TRANSITIONS.get(current, set()):
        raise ValueError(
            f"Cannot snooze from '{current}'. "
            f"Valid: {VALID_TRANSITIONS.get(current, set())}"
        )
    return update_task(task_id, status="snoozed", snoozed_until=snoozed_until)


def unsnooze_task(task_id: int) -> dict | None:
    """Wake a snoozed task — move to active and clear snoozed_until."""
    return update_task(task_id, status="active", snoozed_until=None)


def get_expired_snoozed() -> list[int]:
    """Return IDs of snoozed tasks whose snoozed_until has passed."""
    now_iso = _now()
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id FROM tasks WHERE status='snoozed' AND replace(replace(snoozed_until,'.000Z','Z'),'.000+00:00','Z') <= ?",
            (now_iso,),
        ).fetchall()
        return [r["id"] for r in rows]
    finally:
        conn.close()


# ── Task Context ───────────────────────────────────────────────────────────

def add_context(
    task_id: int,
    context_type: str,
    content: str,
    query_used: str | None = None,
) -> dict:
    """Append a context entry for a task."""
    conn = get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO task_context (task_id, context_type, content, query_used) VALUES (?,?,?,?)",
            (task_id, context_type, content, query_used),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM task_context WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return dict(row)
    finally:
        conn.close()


def get_contexts(task_id: int) -> list[dict]:
    """Get all context entries for a task, newest first."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM task_context WHERE task_id = ? ORDER BY fetched_at DESC",
            (task_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ── Sync Log ───────────────────────────────────────────────────────────────

def log_sync(
    sync_type: str,
    result_summary: str = "",
    tasks_created: int = 0,
    tasks_updated: int = 0,
) -> dict:
    """Record a sync event."""
    conn = get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO sync_log (sync_type, result_summary, tasks_created, tasks_updated) VALUES (?,?,?,?)",
            (sync_type, result_summary, tasks_created, tasks_updated),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM sync_log WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return dict(row)
    finally:
        conn.close()


def get_last_sync(sync_type: str | None = None) -> dict | None:
    """Get the most recent sync log entry."""
    conn = get_connection()
    try:
        if sync_type:
            row = conn.execute(
                "SELECT * FROM sync_log WHERE sync_type = ? "
                "ORDER BY synced_at DESC, id DESC LIMIT 1",
                (sync_type,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM sync_log ORDER BY synced_at DESC, id DESC LIMIT 1"
            ).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


# ── Stats ──────────────────────────────────────────────────────────────────

def get_stats() -> dict:
    """Get task count statistics."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) as count FROM tasks GROUP BY status"
        ).fetchall()
        stats = {r["status"]: r["count"] for r in rows}
        stats["total"] = sum(stats.values())
        return stats
    finally:
        conn.close()


# -- Cowork task actions -----------------------------------------------------
#
# Preview and execution turns share an audit chain. Execution is always a child
# row so approval, transport progress, and the terminal delivery verdict remain
# separate from the draft that the user reviewed.

_ACTION_INSERT_FIELDS = (
    "action_type", "intent", "notes_snapshot", "redirect_text",
    "composed_prompt", "destination_kind", "destination_ref", "conversation_id",
    "island_url", "delivery_channel", "destination_display", "destination_source",
    "destination_confirmed_at", "parent_action_id",
    "blocked_question", "answered_interaction",
    "interaction_mode",
)

# Teams and email are the only transports TodoIQ can describe today. The value
# is orthogonal to destination_kind, which describes audience size, not channel.
DELIVERY_CHANNELS = frozenset({"teams", "email"})
_COWORK_REVISION_FIELDS = frozenset({
    "title",
    "description",
    "coaching_text",
    "user_notes",
    "key_people",
})

# Only the draft the user typed is editable. Everything Cowork produced, and
# the state machine itself, is off limits from the API.
ACTION_EDITABLE_FIELDS = frozenset({"draft_edited"})

_ACTION_RESULT_FIELDS = frozenset({
    "state", "finding", "draft", "terminal_status", "tool_trace", "error",
    "conversation_id", "destination_kind", "destination_ref",
})


def create_task_action(task_id: int, **fields) -> dict:
    """Insert a new task_actions row in state 'previewing'."""
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        task = conn.execute(
            "SELECT action_type, cowork_revision FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if not task:
            conn.rollback()
            raise ValueError("Task not found")
        cols = ["task_id", "action_type", "cowork_revision"]
        vals = [task_id, task["action_type"], task["cowork_revision"]]
        for name in _ACTION_INSERT_FIELDS:
            if name == "action_type":
                continue
            if fields.get(name) is not None:
                cols.append(name)
                vals.append(fields[name])
        placeholders = ",".join("?" * len(cols))
        cursor = conn.execute(
            f"INSERT INTO task_actions ({','.join(cols)}) VALUES ({placeholders})",
            vals,
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM task_actions WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def final_action_draft(action: dict) -> str:
    """Return the exact reviewed text that an action will execute."""
    draft = (
        (action.get("draft_edited") or "").strip()
        or (action.get("draft") or "").strip()
    )
    if not draft and action.get("action_type") == "schedule-meeting":
        draft = (action.get("finding") or "").strip()
    return draft


def create_execution_action(
    parent_action_id: int, approved_snapshot: dict
) -> dict | None:
    """Atomically claim one approved preview for execution.

    The partial unique index on parent_action_id is the crash-safe idempotency
    guard. A browser double-click or a second process can never create a second
    delivery turn for the same approved draft.
    """
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        parent = conn.execute(
            "SELECT * FROM task_actions WHERE id = ?", (parent_action_id,)
        ).fetchone()
        if not parent:
            conn.rollback()
            return None
        parent = dict(parent)
        final_draft = final_action_draft(parent)
        expected = {
            "parent_action_id": parent["id"],
            "draft": final_draft,
            "destination_ref": parent.get("destination_ref") or "",
            "destination_display": parent.get("destination_display") or "",
            "delivery_channel": parent.get("delivery_channel") or "",
            "destination_confirmed_at": parent.get("destination_confirmed_at") or "",
        }
        if approved_snapshot != expected:
            conn.rollback()
            return None
        cursor = conn.execute(
            """
            INSERT INTO task_actions (
                task_id, action_type, cowork_revision, state, intent, notes_snapshot,
                redirect_text, composed_prompt, finding, draft, draft_edited,
                destination_kind, destination_ref, conversation_id,
                island_url, delivery_channel, destination_display,
                destination_confirmed_at, destination_source, parent_action_id,
                interaction_mode, execution_requested_at
            )
            SELECT
                task_id, action_type, cowork_revision, 'executing', intent, notes_snapshot,
                redirect_text, NULL, finding,
                ?, ?,
                destination_kind, destination_ref, conversation_id,
                island_url, delivery_channel, destination_display,
                destination_confirmed_at, destination_source, id,
                'interaction', strftime('%Y-%m-%dT%H:%M:%SZ','now')
            FROM task_actions parent
            WHERE parent.id = ?
              AND parent.state = 'ready'
              AND EXISTS (
                  SELECT 1 FROM tasks current
                  WHERE current.id = parent.task_id
                    AND current.cowork_revision = parent.cowork_revision
                    AND current.action_type = parent.action_type
              )
              AND parent.id = (
                  SELECT MAX(latest.id)
                  FROM task_actions latest
                  WHERE latest.task_id = parent.task_id
              )
              AND parent.destination_confirmed_at IS NOT NULL
              AND COALESCE(TRIM(parent.destination_ref), '') <> ''
              AND COALESCE(TRIM(parent.destination_display), '') <> ''
              AND COALESCE(TRIM(parent.conversation_id), '') <> ''
              AND ? <> ''
              AND NOT EXISTS (
                  SELECT 1 FROM task_actions child
                  WHERE child.parent_action_id = parent.id
                    AND child.state IN (
                        'executing','executed','execute_unconfirmed'
                    )
              )
            """,
            (final_draft, final_draft, parent_action_id, final_draft),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return None
        action_id = cursor.lastrowid
        conn.commit()
        row = conn.execute(
            "SELECT * FROM task_actions WHERE id = ?", (action_id,)
        ).fetchone()
        return _row_to_dict(row)
    except sqlite3.IntegrityError:
        conn.rollback()
        return None
    finally:
        conn.close()


def get_latest_task_action(task_id: int) -> dict | None:
    """Most recent attempt, ordered by id.

    NOT created_at: those are second-precision TEXT and tie, which is exactly
    the defect found live in get_last_sync. Two Redos inside one second would
    otherwise return an arbitrary row.
    """
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT a.* FROM task_actions a
            JOIN tasks t ON t.id = a.task_id
            WHERE a.task_id = ?
              AND a.cowork_revision = t.cowork_revision
              AND a.action_type = t.action_type
            ORDER BY a.id DESC LIMIT 1
            """,
            (task_id,),
        ).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


def mark_task_action_seen(action_id: int) -> dict | None:
    """Mark a ready Cowork action as seen using a server-generated timestamp."""
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE task_actions SET seen_at = ?, updated_at = ? "
            "WHERE id = ? AND state = 'ready' AND seen_at IS NULL "
            "AND EXISTS (SELECT 1 FROM tasks t WHERE t.id = task_actions.task_id "
            "AND t.cowork_revision = task_actions.cowork_revision "
            "AND t.action_type = task_actions.action_type)",
            (_now(), _now(), action_id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM task_actions WHERE id = ?", (action_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def confirm_destination(
    action_id: int,
    delivery_channel: str | None,
    destination_ref: str,
    destination_display: str,
    destination_source: str,
) -> dict | None:
    """Bind a reviewed audience to a ready action, timestamped by the server.

    The state guard lives in SQL, mirroring mark_task_action_seen, so a caller
    can never confirm a preview that is still running or has already failed.
    Confirming records who an action is for; it does not deliver anything.
    """
    if delivery_channel is not None and delivery_channel not in DELIVERY_CHANNELS:
        return None
    ref = (destination_ref or "").strip()
    display = (destination_display or "").strip()
    if not ref or not display:
        return None

    conn = get_connection()
    try:
        cursor = conn.execute(
            "UPDATE task_actions SET delivery_channel = ?, destination_ref = ?, "
            "destination_display = ?, destination_source = ?, "
            "destination_confirmed_at = ?, updated_at = ? "
            "WHERE id = ? AND state = 'ready' "
            "AND EXISTS (SELECT 1 FROM tasks t WHERE t.id = task_actions.task_id "
            "AND t.cowork_revision = task_actions.cowork_revision "
            "AND t.action_type = task_actions.action_type) "
            "AND (? IS NOT NULL OR action_type = 'schedule-meeting')",
            (
                delivery_channel, ref, display, destination_source,
                _now(), _now(), action_id, delivery_channel,
            ),
        )
        conn.commit()
        if cursor.rowcount == 0:
            return None
        row = conn.execute(
            "SELECT * FROM task_actions WHERE id = ?", (action_id,)
        ).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


def list_task_actions(task_id: int) -> list[dict]:
    """Full attempt chain, oldest first. The Redo chain is the audit trail."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM task_actions WHERE task_id = ? ORDER BY id ASC",
            (task_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_task_action(
    action_id: int,
    allowed: frozenset,
    *,
    required_state: str | None = None,
    **fields,
) -> dict | None:
    """Update an action row, restricted to `allowed` field names."""
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return None

    sets = ", ".join(f"{k} = ?" for k in updates)
    if updates.get("state") in {
        "ready", "failed", "executed", "execute_unconfirmed"
    }:
        sets += (
            ", completed_at = COALESCE("
            "completed_at, strftime('%Y-%m-%dT%H:%M:%SZ','now'))"
        )
    if updates.get("state") == "executed":
        sets += (
            ", delivery_confirmed_at = COALESCE("
            "delivery_confirmed_at, strftime('%Y-%m-%dT%H:%M:%SZ','now'))"
        )
    conn = get_connection()
    try:
        where = "id = ?"
        params = list(updates.values()) + [action_id]
        if required_state is not None:
            where += " AND state = ?"
            params.append(required_state)
        cursor = conn.execute(
            f"UPDATE task_actions SET {sets}, "
            f"updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE {where}",
            params,
        )
        conn.commit()
        if cursor.rowcount == 0:
            return None
        row = conn.execute(
            "SELECT * FROM task_actions WHERE id = ?", (action_id,)
        ).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


def set_blocked_question_if_missing(action_id: int, question: str) -> dict | None:
    """Persist a new interaction unless it is the one just answered."""
    conn = get_connection()
    try:
        cursor = conn.execute(
            "UPDATE task_actions SET blocked_question = ?, "
            "had_interaction = 1, "
            "answered_interaction = NULL, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') "
            "WHERE id = ? AND (blocked_question IS NULL OR "
            "(blocked_question = '' AND COALESCE("
            "CASE WHEN json_valid(answered_interaction) "
            "THEN json_extract(answered_interaction, '$.question_raw') "
            "ELSE answered_interaction END, '') <> ?))",
            (question, action_id, question),
        )
        conn.commit()
        if cursor.rowcount == 0:
            return None
        row = conn.execute(
            "SELECT * FROM task_actions WHERE id = ?", (action_id,)
        ).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


def claim_blocked_question_answer(
    action_id: int,
    expected_question: str,
    answered_payload: str | None = None,
) -> bool:
    """Atomically claim the pending interaction so only one answer is sent."""
    conn = get_connection()
    try:
        cursor = conn.execute(
            "UPDATE task_actions SET answered_interaction = ?, "
            "had_interaction = 1, "
            "blocked_question = '', "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') "
            "WHERE id = ? AND state IN ('previewing','executing') "
            "AND blocked_question = ?",
            (answered_payload or expected_question, action_id, expected_question),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def restore_claimed_blocked_question(
    action_id: int,
    question: str,
    answered_payload: str | None = None,
) -> bool:
    """Restore a definitively rejected answer if its claim is still current."""
    conn = get_connection()
    try:
        cursor = conn.execute(
            "UPDATE task_actions SET blocked_question = ?, "
            "answered_interaction = NULL, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') "
            "WHERE id = ? AND blocked_question = '' "
            "AND answered_interaction = ?",
            (question, action_id, answered_payload or question),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def clear_blocked_question_if_unchanged(
    action_id: int,
    blocked_question: str | None,
    answered_interaction: str | None,
    *,
    preserve_answer: bool = False,
) -> bool:
    """Clear a resumed interaction without erasing a concurrent state change."""
    conn = get_connection()
    try:
        answer_update = (
            "answered_interaction = answered_interaction, "
            if preserve_answer
            else "answered_interaction = NULL, "
        )
        cursor = conn.execute(
            "UPDATE task_actions SET blocked_question = NULL, "
            + answer_update
            +
            "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') "
            "WHERE id = ? AND blocked_question IS ? "
            "AND answered_interaction IS ?",
            (action_id, blocked_question, answered_interaction),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def recover_stuck_previews() -> int:
    """Recover any in-flight turn left behind by a restart.

    Without this a browser close or server restart strands the row in
    'previewing' forever and the task can never be previewed again.
    """
    conn = get_connection()
    try:
        preview_cursor = conn.execute(
            "UPDATE task_actions SET state = 'failed', "
            "error = 'Interrupted by a server restart.', "
            "completed_at = COALESCE("
            "completed_at, strftime('%Y-%m-%dT%H:%M:%SZ','now')), "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') "
            "WHERE state = 'previewing'"
        )
        execution_cursor = conn.execute(
            "UPDATE task_actions SET state = 'execute_unconfirmed', "
            "error = 'Server restarted while sending. Check the destination to "
            "confirm whether it was delivered before retrying.', "
            "completed_at = COALESCE("
            "completed_at, strftime('%Y-%m-%dT%H:%M:%SZ','now')), "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') "
            "WHERE state = 'executing'"
        )
        conn.commit()
        return preview_cursor.rowcount + execution_cursor.rowcount
    finally:
        conn.close()
