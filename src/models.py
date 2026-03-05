"""Task CRUD and lifecycle operations for TodoNess."""

import sqlite3
from datetime import datetime, timedelta, timezone
from .db import get_connection, init_db

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
        now = _now()
        cursor = conn.execute(
            """INSERT INTO tasks
               (title, description, status, parse_status, raw_input, priority,
                due_date, committed_date, source_type, source_id, source_url,
                source_snippet, coaching_text, action_type, skill_output, key_people,
                related_meeting, user_notes, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                title, description, status, parse_status, raw_input, priority,
                due_date, committed_date, source_type, source_id, source_url,
                source_snippet, coaching_text, action_type, skill_output, key_people,
                related_meeting, user_notes, now, now,
            ),
        )
        task_id = cursor.lastrowid
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
            f"SELECT * FROM tasks {where} ORDER BY priority ASC, created_at DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_task(task_id: int, **fields) -> dict | None:
    """Update arbitrary fields on a task. Returns updated task or None."""
    if not fields:
        return get_task(task_id)
    fields["updated_at"] = _now()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [task_id]
    conn = get_connection()
    try:
        conn.execute(f"UPDATE tasks SET {set_clause} WHERE id = ?", values)
        conn.commit()
        return get_task(task_id, conn)
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
                "SELECT * FROM sync_log WHERE sync_type = ? ORDER BY synced_at DESC LIMIT 1",
                (sync_type,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM sync_log ORDER BY synced_at DESC LIMIT 1"
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
