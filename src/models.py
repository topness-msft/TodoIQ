"""Task CRUD and lifecycle operations for TodoNess."""

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from .db import get_connection, init_db

logger = logging.getLogger(__name__)

# Valid status transitions
VALID_TRANSITIONS = {
    "suggested": {"active", "waiting", "snoozed", "dismissed", "deleted"},
    "active": {"in_progress", "waiting", "snoozed", "completed", "dismissed", "deleted"},
    "in_progress": {"active", "waiting", "snoozed", "completed", "dismissed", "deleted"},
    "waiting": {"active", "in_progress", "snoozed", "completed", "dismissed", "deleted"},
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

# Unresolved statuses — title dedup only matches against these
_UNRESOLVED = frozenset({'suggested', 'active', 'in_progress', 'waiting', 'snoozed'})


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


def _extract_people_aliases(key_people_json: str | None) -> set[str]:
    """Extract lowercase email-prefix aliases from key_people JSON.

    Given '[{"name": "John Wheat", "email": "john.wheat@microsoft.com"}]',
    returns {'john.wheat'}.
    """
    if not key_people_json:
        return set()
    try:
        people = json.loads(key_people_json)
        return {
            p["email"].lower().split("@")[0]
            for p in people
            if isinstance(p, dict) and p.get("email")
        }
    except (json.JSONDecodeError, TypeError):
        return set()


def _person_match(
    a: str,
    b: str,
    people_a: set[str] | None = None,
    people_b: set[str] | None = None,
) -> bool:
    """Check if two person aliases refer to the same person.

    First tries exact alias match.  Falls back to cross-checking
    each alias against the other task's key_people email prefixes,
    and finally checks for any shared person across both key_people
    sets (same conversation, different attributed sender).
    """
    if a == b:
        return True
    # Cross-check: alias appears in the other task's key_people
    if people_b and a in people_b:
        return True
    if people_a and b in people_a:
        return True
    # Shared person across both tasks' key_people
    if people_a and people_b and (people_a & people_b):
        return True
    return False


def find_similar_source(
    conn: sqlite3.Connection,
    source_id: str,
    source_type: str | None = None,
    threshold: float = 0.5,
    key_people: str | None = None,
) -> dict | None:
    """Find an existing non-deleted task whose source_id fuzzy-matches.

    Returns the matching task dict, or None.
    """
    parsed = normalize_source_id(source_id)
    if parsed is None:
        return None
    src_type, person_alias, new_tokens = parsed
    new_people = _extract_people_aliases(key_people)

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
        ex_people = _extract_people_aliases(row["key_people"])
        if not _person_match(person_alias, ex_person, new_people, ex_people):
            continue
        if _jaccard(new_tokens, ex_tokens) >= threshold:
            return dict(row)
    return None


# ── Title-Based Fuzzy Dedup ────────────────────────────────────────────────

import re as _re

_TITLE_STOP_WORDS = _STOP_WORDS | frozenset({
    'follow', 'confirm', 'check', 'share', 'reply', 'respond', 'schedule',
    'discuss', 'review', 'send', 'prepare', 'set', 'ask', 'ping', 'meet',
    'draft', 'complete', 'submit', 'test', 'try', 'provide',
    'address', 'update', 'finalize', 'next', 'steps', 'status',
})

_SUFFIX_MAP = {
    'tion': '', 'sion': '', 'ing': '', 'ment': '', 'ness': '',
    'ting': 't', 'ning': 'n', 'ming': 'm',
}

_SYNONYM_MAP = {
    'repo': 'repository', 'github': 'repository',
    'intake': 'collection', 'collected': 'collection',
    'collect': 'collection', 'gathering': 'collection',
    'collection': 'collection',
    'question': 'question', 'questions': 'question',
    'golive': 'golive', 'go-live': 'golive',
    'timing': 'time', 'timeline': 'time', 'timed': 'time',
    'speaker': 'speak', 'speaking': 'speak', 'talk': 'speak',
    'allotted': 'speak',
    'trimming': 'trim', 'trim': 'trim',
    'webinar': 'webinar', 'webinars': 'webinar',
    'deck': 'deck', 'slides': 'deck', 'slide': 'deck', 'presentation': 'deck',
    'template': 'templates', 'templates': 'templates',
    'feedback': 'feedback', 'report': 'feedback',
    'conf': 'conference',
}


def _stem_token(word: str) -> str:
    """Very simple suffix-stripping stemmer."""
    if len(word) <= 5:
        return word
    for suffix, repl in _SUFFIX_MAP.items():
        result = word[:-len(suffix)] + repl
        if word.endswith(suffix) and len(result) >= 4:
            return result
    return word


def _normalize_title_tokens(title: str) -> set[str]:
    """Extract meaningful, normalized tokens from a task title."""
    t = title.lower()
    # Preserve compound terms before splitting
    t = t.replace('go-live', 'golive').replace('go live', 'golive')
    t = t.replace('-', ' ')
    # Remove possessives before tokenizing
    t = _re.sub(r"'s\b", '', t)
    words = _re.findall(r'[a-z0-9]+', t)
    tokens = set()
    for w in words:
        if w in _TITLE_STOP_WORDS or len(w) < 3:
            continue
        # Apply synonym mapping first, then stem
        canonical = _SYNONYM_MAP.get(w)
        if canonical:
            tokens.add(canonical)
        else:
            tokens.add(_stem_token(w))
    return tokens


def find_similar_by_title(
    conn: sqlite3.Connection,
    title: str,
    key_people: str | None = None,
    source_id: str | None = None,
    threshold: float = 0.35,
) -> dict | None:
    """Find an existing unresolved task with matching people and similar title.

    Falls back to person alias from source_id when key_people is empty.
    Returns the matching task dict, or None.
    """
    new_tokens = _normalize_title_tokens(title)
    if len(new_tokens) < 2:
        return None  # title too generic to dedup safely

    # Gather person aliases from key_people and/or source_id
    new_people = _extract_people_aliases(key_people)
    source_person = None
    if source_id:
        parsed = normalize_source_id(source_id)
        if parsed:
            source_person = parsed[1]  # person_alias

    if not new_people and not source_person:
        return None  # need at least one person signal

    # Fetch unresolved candidates
    placeholders = ','.join('?' for _ in _UNRESOLVED)
    rows = conn.execute(
        f"SELECT * FROM tasks WHERE status IN ({placeholders}) AND title IS NOT NULL",
        tuple(_UNRESOLVED),
    ).fetchall()

    for row in rows:
        ex_people = _extract_people_aliases(row["key_people"])
        ex_source_person = None
        if row["source_id"]:
            ex_parsed = normalize_source_id(row["source_id"])
            if ex_parsed:
                ex_source_person = ex_parsed[1]

        # Require person overlap
        person_overlap = False
        if new_people and ex_people and (new_people & ex_people):
            person_overlap = True
        elif source_person and ex_source_person and source_person == ex_source_person:
            person_overlap = True
        elif source_person and ex_people and source_person in ex_people:
            person_overlap = True
        elif new_people and ex_source_person and ex_source_person in new_people:
            person_overlap = True
        if not person_overlap:
            continue

        ex_tokens = _normalize_title_tokens(row["title"])
        if len(ex_tokens) < 2:
            continue

        overlap = new_tokens & ex_tokens
        if len(overlap) < 2:
            continue  # require at least 2 meaningful words in common

        score = _jaccard(new_tokens, ex_tokens)
        if score >= threshold:
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
    source_snippet: str | None = None,
    coaching_text: str | None = None,
    action_type: str = "general",
    skill_output: str | None = None,
    key_people: str | None = None,
    related_meeting: str | None = None,
    user_notes: str = "",
    source_date: str | None = None,
) -> dict:
    """Create a new task and return it as a dict."""
    conn = get_connection()
    try:
        # ── Dedup: exact source_id → fuzzy source_id → title similarity ──
        dedup_match = None
        dedup_reason = None

        if source_id:
            exact = conn.execute(
                "SELECT * FROM tasks WHERE source_id = ? AND status != 'deleted'",
                (source_id,),
            ).fetchone()
            if exact:
                dedup_match = dict(exact)
                dedup_reason = "exact source_id"

            if not dedup_match:
                fuzzy_match = find_similar_source(conn, source_id, source_type, key_people=key_people)
                if fuzzy_match:
                    dedup_match = fuzzy_match
                    dedup_reason = "fuzzy source_id"

        if not dedup_match:
            title_match = find_similar_by_title(conn, title, key_people=key_people, source_id=source_id)
            if title_match:
                dedup_match = title_match
                dedup_reason = "title similarity"

        if dedup_match:
            logger.info(
                "Dedup (%s): new '%s' matched existing task #%s '%s'",
                dedup_reason, title or source_id, dedup_match["id"], dedup_match["title"],
            )
            # Augment with new source context so we don't lose provenance
            if source_snippet and dedup_reason != "exact source_id":
                ctx = f"[Dedup — {dedup_reason}] Also surfaced as: {title}"
                if source_id:
                    ctx += f"\nSource: {source_id}"
                if source_snippet:
                    ctx += f"\nSnippet: {source_snippet[:300]}"
                conn.execute(
                    "INSERT INTO task_context (task_id, context_type, content, query_used) VALUES (?,?,?,?)",
                    (dedup_match["id"], "dedup", ctx, None),
                )
                conn.commit()
            return dedup_match

        # Staleness guard: if source predates last refresh, it was already
        # available and either skipped or not surfaced — downgrade to P5.
        # Flagged emails are exempt (flag = explicit intent regardless of age).
        # Falls back to 14-day hard cap when no sync history exists.
        if status == "suggested" and source_date and source_type != "email":
            try:
                sd = datetime.strptime(source_date, "%Y-%m-%d")
                last = conn.execute(
                    "SELECT synced_at FROM sync_log ORDER BY synced_at DESC LIMIT 1"
                ).fetchone()
                if last and last["synced_at"]:
                    cutoff = datetime.strptime(last["synced_at"][:10], "%Y-%m-%d")
                else:
                    cutoff = datetime.utcnow() - timedelta(days=14)
                if sd < cutoff:
                    age_days = (datetime.utcnow() - sd).days
                    orig_priority = priority
                    priority = 5
                    note = f"[Auto-downgraded from P{orig_priority} — source is {age_days}d old, predates last refresh] "
                    coaching_text = note + (coaching_text or "")
            except (ValueError, TypeError):
                pass  # malformed date, skip guard

        now = _now()
        cursor = conn.execute(
            """INSERT INTO tasks
               (title, description, status, parse_status, raw_input, priority,
                due_date, committed_date, source_type, source_id, source_url,
                source_date, source_snippet, coaching_text, action_type, skill_output,
                key_people, related_meeting, user_notes, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                title, description, status, parse_status, raw_input, priority,
                due_date, committed_date, source_type, source_id, source_url,
                source_date, source_snippet, coaching_text, action_type, skill_output,
                key_people, related_meeting, user_notes, now, now,
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
