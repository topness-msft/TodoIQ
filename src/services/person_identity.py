"""Canonical person identity resolution.

Design contract (see plan.md):
- Resolution order:
    1. aad_object_id (authoritative)
    2. exact normalized email or UPN (high confidence)
    3. user-confidence alias (authoritative; from manual merge)
    4. otherwise: CREATE a new person.

- Name aliases are recall-only — they NEVER resolve identity by themselves.
  Two unrelated "Steve Smith" persons must remain distinct.

- Merges are non-destructive: set losing.canonical_person_id = winning.id and
  insert a person_merge_history row. Reads walk via canonical_root() with
  cycle protection. Records are never deleted; merges can be undone.

- task_person is the recall index, derived from key_people JSON + the
  source_id sender. Recomputed on every task insert/update.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from typing import Iterable

logger = logging.getLogger(__name__)

_ROOT_WALK_MAX = 10


# ── Normalization ──────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_email(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip().lower()
    return v or None


def normalize_name(value: str | None) -> str | None:
    if not value:
        return None
    # Collapse whitespace, lowercase. Keep punctuation removed for matching
    # but preserve underlying display_name elsewhere.
    v = re.sub(r"\s+", " ", value.strip()).lower()
    return v or None


# ── Canonical root walk ──────────────────────────────────────────────────

def canonical_root(conn: sqlite3.Connection, person_id: int) -> int:
    """Walk canonical_person_id pointers to the root, cycle-protected."""
    visited: set[int] = set()
    current = person_id
    for _ in range(_ROOT_WALK_MAX):
        if current in visited:
            logger.warning(f"canonical_person_id cycle at {current}")
            return current
        visited.add(current)
        row = conn.execute(
            "SELECT canonical_person_id FROM person WHERE id = ?", (current,)
        ).fetchone()
        if row is None:
            return current
        nxt = row["canonical_person_id"]
        if nxt is None or nxt == current:
            return current
        current = nxt
    logger.warning(f"canonical_person_id walk exceeded depth from {person_id}")
    return current


# ── Resolution ───────────────────────────────────────────────────────────

def _find_by_aad(conn: sqlite3.Connection, aad: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM person WHERE aad_object_id = ?", (aad,)
    ).fetchone()
    return row["id"] if row else None


def _find_by_alias(
    conn: sqlite3.Connection, kind: str, value: str, *, allowed_confidences: tuple[str, ...]
) -> int | None:
    placeholders = ",".join("?" * len(allowed_confidences))
    rows = conn.execute(
        f"""SELECT person_id FROM person_alias
            WHERE alias_kind = ? AND alias_value = ?
              AND confidence IN ({placeholders})""",
        (kind, value, *allowed_confidences),
    ).fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        # Multiple persons share this alias (e.g. shared mailbox).
        # Identity-resolving aliases (email/aad/user) should be unique per
        # canonical root; if they aren't, log and pick the lowest id
        # deterministically. Caller may upgrade to a manual-merge prompt later.
        logger.warning(
            f"alias collision kind={kind} value={value} matches {len(rows)} persons"
        )
    return rows[0]["person_id"]


def resolve_person(
    conn: sqlite3.Connection,
    *,
    display_name: str | None = None,
    email: str | None = None,
    aad_object_id: str | None = None,
    upn: str | None = None,
    create_if_missing: bool = True,
) -> int | None:
    """Return the canonical-rooted person id for the given attributes.

    Resolution precedence:
        1. aad_object_id (kind='aad', any confidence)
        2. exact email (kind='email', confidence in ('email','user'))
        3. exact UPN (kind='upn', confidence in ('email','user'))
        4. user-confidence name alias (kind='name', confidence='user') — only
           explicit user merges, never inferred.

    Name-only matches with confidence='name' or 'inferred' are NEVER used to
    resolve identity here. They exist for recall in dedup queries only.

    Returns None if not found and create_if_missing=False.
    """
    e = normalize_email(email)
    u = normalize_email(upn)
    aad = aad_object_id.strip() if aad_object_id else None

    # 1. AAD
    if aad:
        pid = _find_by_aad(conn, aad)
        if pid is not None:
            return canonical_root(conn, pid)

    # 2. Email
    if e:
        pid = _find_by_alias(conn, "email", e, allowed_confidences=("email", "user"))
        if pid is not None:
            return canonical_root(conn, pid)

    # 3. UPN
    if u and u != e:
        pid = _find_by_alias(conn, "upn", u, allowed_confidences=("email", "user"))
        if pid is not None:
            return canonical_root(conn, pid)

    # 4. user-confidence name alias only
    n = normalize_name(display_name)
    if n:
        pid = _find_by_alias(conn, "name", n, allowed_confidences=("user",))
        if pid is not None:
            return canonical_root(conn, pid)

    if not create_if_missing:
        return None

    return create_person(
        conn,
        display_name=display_name or e or u or aad or "(unknown)",
        email=e,
        aad_object_id=aad,
        upn=u,
    )


def create_person(
    conn: sqlite3.Connection,
    *,
    display_name: str,
    email: str | None = None,
    aad_object_id: str | None = None,
    upn: str | None = None,
) -> int:
    """Create a new person row + initial aliases. Returns new person id."""
    now = _now()
    cur = conn.execute(
        """INSERT INTO person (display_name, primary_email, aad_object_id, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?)""",
        (display_name, email, aad_object_id, now, now),
    )
    pid = cur.lastrowid
    if aad_object_id:
        _add_alias(conn, pid, "aad", aad_object_id, "aad")
    if email:
        _add_alias(conn, pid, "email", email, "email")
    if upn and upn != email:
        _add_alias(conn, pid, "upn", upn, "email")
    n = normalize_name(display_name)
    if n:
        _add_alias(conn, pid, "name", n, "name")
    return pid


def _add_alias(
    conn: sqlite3.Connection,
    person_id: int,
    kind: str,
    value: str,
    confidence: str,
) -> None:
    if not value:
        return
    v = value.lower() if kind in ("email", "upn", "aad") else normalize_name(value)
    if not v:
        return
    conn.execute(
        """INSERT OR IGNORE INTO person_alias
           (person_id, alias_kind, alias_value, confidence, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (person_id, kind, v, confidence, _now()),
    )


def add_alias(
    conn: sqlite3.Connection,
    person_id: int,
    kind: str,
    value: str,
    confidence: str,
) -> None:
    """Public API for adding an alias to an existing person."""
    _add_alias(conn, person_id, kind, value, confidence)


# ── Merges (non-destructive) ─────────────────────────────────────────────

def merge_persons(
    conn: sqlite3.Connection,
    *,
    losing_id: int,
    winning_id: int,
    reason: str | None = None,
) -> None:
    """Point losing person at winning. Idempotent. Safe to undo via unmerge_persons."""
    if losing_id == winning_id:
        return
    losing_root = canonical_root(conn, losing_id)
    winning_root = canonical_root(conn, winning_id)
    if losing_root == winning_root:
        return  # already in same canonical cluster
    conn.execute(
        "UPDATE person SET canonical_person_id = ?, updated_at = ? WHERE id = ?",
        (winning_root, _now(), losing_root),
    )
    conn.execute(
        """INSERT INTO person_merge_history (losing_id, winning_id, reason, created_at)
           VALUES (?, ?, ?, ?)""",
        (losing_root, winning_root, reason, _now()),
    )


def unmerge_persons(conn: sqlite3.Connection, losing_id: int) -> None:
    """Clear the canonical pointer on losing_id and stamp undone_at."""
    conn.execute(
        "UPDATE person SET canonical_person_id = NULL, updated_at = ? WHERE id = ?",
        (_now(), losing_id),
    )
    conn.execute(
        """UPDATE person_merge_history SET undone_at = ?
           WHERE losing_id = ? AND undone_at IS NULL""",
        (_now(), losing_id),
    )


# ── Task ↔ person derivation ─────────────────────────────────────────────

def _parse_key_people(key_people_json: str | None) -> list[dict]:
    if not key_people_json:
        return []
    try:
        arr = json.loads(key_people_json)
    except (json.JSONDecodeError, TypeError):
        return []
    return [p for p in arr if isinstance(p, dict)]


def _parse_source_id_sender(source_id: str | None) -> tuple[str | None, str | None]:
    """Extract a sender identifier from source_id.

    source_id format observed: '<source_type>::<sender>::<rest>' where sender
    may be a full email or just a local part.

    Returns (email_if_full_email, local_part_or_name).
    """
    if not source_id:
        return None, None
    parts = source_id.split("::")
    if len(parts) < 2:
        return None, None
    raw = parts[1].strip()
    if not raw:
        return None, None
    if "@" in raw:
        return raw.lower(), raw.split("@")[0].lower()
    return None, raw.lower()


def derive_task_persons(
    conn: sqlite3.Connection,
    task_id: int,
    *,
    key_people_json: str | None,
    source_id: str | None,
) -> list[int]:
    """Resolve all persons referenced by the task and write task_person rows.

    Replaces existing task_person rows for this task (idempotent).
    Returns the list of canonical-rooted person ids written.
    """
    # Wipe + rewrite so updates stay consistent.
    conn.execute("DELETE FROM task_person WHERE task_id = ?", (task_id,))

    written: dict[tuple[int, str], None] = {}

    # source_id sender → role 'sender'. Only resolves identity if full email.
    sender_email, _local = _parse_source_id_sender(source_id)
    if sender_email:
        pid = resolve_person(conn, email=sender_email)
        if pid is not None:
            key = (pid, "sender")
            if key not in written:
                conn.execute(
                    "INSERT OR IGNORE INTO task_person (task_id, person_id, role) VALUES (?,?,?)",
                    (task_id, pid, "sender"),
                )
                written[key] = None

    # key_people array → role 'key_people'
    for p in _parse_key_people(key_people_json):
        pid = resolve_person(
            conn,
            display_name=p.get("name"),
            email=p.get("email"),
            aad_object_id=p.get("aad_object_id") or p.get("aadObjectId"),
            upn=p.get("upn"),
        )
        if pid is None:
            continue
        key = (pid, "key_people")
        if key not in written:
            conn.execute(
                "INSERT OR IGNORE INTO task_person (task_id, person_id, role) VALUES (?,?,?)",
                (task_id, pid, "key_people"),
            )
            written[key] = None

    return sorted({pid for (pid, _) in written})


def get_task_person_ids(conn: sqlite3.Connection, task_id: int) -> set[int]:
    """Return the set of canonical-rooted person ids associated with a task."""
    rows = conn.execute(
        "SELECT DISTINCT person_id FROM task_person WHERE task_id = ?", (task_id,)
    ).fetchall()
    if not rows:
        return set()
    return {canonical_root(conn, r["person_id"]) for r in rows}


def find_tasks_sharing_persons(
    conn: sqlite3.Connection,
    person_ids: Iterable[int],
    *,
    statuses: tuple[str, ...] = ("active", "in_progress", "waiting", "snoozed", "suggested"),
    exclude_task_ids: Iterable[int] = (),
    limit: int = 200,
) -> list[int]:
    """Return task ids that share any of the given (canonical-rooted) persons.

    Filters to live statuses by default; completed/dismissed/deleted are
    excluded as candidates per the resolved completed-task policy.
    """
    pids = list({canonical_root(conn, p) for p in person_ids})
    if not pids:
        return []
    excl = list(exclude_task_ids)
    pid_ph = ",".join("?" * len(pids))
    status_ph = ",".join("?" * len(statuses))
    sql = f"""SELECT DISTINCT t.id
              FROM task_person tp
              JOIN tasks t ON t.id = tp.task_id
              WHERE tp.person_id IN (
                  SELECT id FROM person
                  WHERE COALESCE(canonical_person_id, id) IN ({pid_ph})
              )
                AND t.status IN ({status_ph})
                AND t.shadow_dup_of IS NULL"""
    params: list = [*pids, *statuses]
    if excl:
        excl_ph = ",".join("?" * len(excl))
        sql += f" AND t.id NOT IN ({excl_ph})"
        params.extend(excl)
    sql += " ORDER BY t.created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [r["id"] for r in rows]
