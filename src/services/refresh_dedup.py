"""Refresh-specific creation/augmentation service.

The `/todo-refresh` slash command surfaces M365 signals (Teams messages,
meeting action items, awaiting-response checks). For each signal we must
decide whether to:
  - skip it (matches a dismissed/completed task — don't reanimate)
  - augment an existing live task (active/in_progress/waiting/snoozed/suggested)
  - create a fresh suggestion

This service unifies that decision in one transactional code path so the
prompt can simply pass parsed signal fields and get back a typed outcome.

Why a separate service from create_task():
  * status-conditional behavior (dismissed-skip, completed-skip,
    suggested-floor) doesn't fit the generic CRUD path.
  * exact-source_id check must scan ALL non-deleted (including dismissed/
    completed) so we can detect a previously-resolved task and skip.
  * augmentation has refresh-specific semantics (MAX source_date, MIN
    priority, append context).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from ..db import get_connection
from ..models import (
    find_similar_by_title,
    find_similar_source,
    get_task,
)
from . import person_identity as _pi

logger = logging.getLogger(__name__)


Outcome = Literal[
    "created",
    "augmented",
    "skipped_dismissed",
    "skipped_completed",
]


@dataclass
class CreateRefreshResult:
    task: dict | None
    outcome: Outcome
    dedup_reason: str | None
    matched_task_id: int | None


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _max_date(a: str | None, b: str | None) -> str | None:
    """Return the lexicographically-greater ISO date string. NULL-safe."""
    if not a:
        return b
    if not b:
        return a
    return a if a >= b else b


def create_or_refresh_suggestion(
    *,
    title: str,
    description: str = "",
    source_type: str,
    source_id: str | None,
    source_snippet: str | None,
    source_date: str | None,
    source_url: str | None = None,
    key_people: str | None,
    priority: int,
    action_type: str = "general",
    coaching_text: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> CreateRefreshResult:
    """Atomically create OR refresh a refresh-sourced suggestion.

    Returns a CreateRefreshResult with one of these outcomes:
      created            - fresh task inserted as 'suggested'
      augmented          - existing live task updated with newer context,
                           source_date promoted to MAX, priority floored to MIN
      skipped_dismissed  - matches a dismissed task; do nothing
      skipped_completed  - matches a completed task; do nothing

    The dedup-check + insert/update is wrapped in BEGIN IMMEDIATE so
    parallel refreshes can't race each other into duplicate rows.
    """
    close = False
    if conn is None:
        conn = get_connection()
        close = True
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = _decide_and_apply(
                conn,
                title=title, description=description,
                source_type=source_type, source_id=source_id,
                source_snippet=source_snippet, source_date=source_date,
                source_url=source_url, key_people=key_people,
                priority=priority, action_type=action_type,
                coaching_text=coaching_text,
            )
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
    finally:
        if close:
            conn.close()


def _decide_and_apply(
    conn: sqlite3.Connection,
    *,
    title: str,
    description: str,
    source_type: str,
    source_id: str | None,
    source_snippet: str | None,
    source_date: str | None,
    source_url: str | None,
    key_people: str | None,
    priority: int,
    action_type: str,
    coaching_text: str | None,
) -> CreateRefreshResult:
    # ── Dedup search: exact source_id → fuzzy → title similarity ────────
    matched: sqlite3.Row | dict | None = None
    reason: str | None = None
    if source_id:
        # Scan ALL non-deleted (including dismissed/completed) so we can
        # detect previously-resolved items and skip.
        row = conn.execute(
            "SELECT * FROM tasks WHERE source_id = ? AND status != 'deleted' ORDER BY id DESC LIMIT 1",
            (source_id,),
        ).fetchone()
        if row is not None:
            matched = dict(row)
            reason = "exact source_id"
        else:
            fuzzy = find_similar_source(conn, source_id, source_type, key_people=key_people)
            if fuzzy is not None:
                matched = fuzzy
                reason = "fuzzy source_id"

    if matched is None:
        title_match = find_similar_by_title(
            conn, title, key_people=key_people, source_id=source_id,
        )
        if title_match is not None:
            matched = title_match
            reason = "title similarity"

    # ── Status-conditional handling ─────────────────────────────────────
    if matched is not None:
        st = matched["status"]
        if st == "dismissed":
            return CreateRefreshResult(
                task=dict(matched), outcome="skipped_dismissed",
                dedup_reason=reason, matched_task_id=matched["id"],
            )
        if st == "completed":
            # Per resolved policy: completed tasks close the loop. New
            # signals get a fresh suggested task instead of reanimating.
            # We still log the match for telemetry but proceed to create.
            logger.info(
                "refresh: source matches completed task #%s ('%s'); "
                "creating new suggestion instead",
                matched["id"], matched["title"],
            )
            matched = None
            reason = None
        else:
            # active / in_progress / waiting / snoozed / suggested → augment
            return _augment(
                conn, matched, source_snippet=source_snippet,
                source_date=source_date, priority=priority,
                title=title, source_id=source_id, dedup_reason=reason or "match",
            )

    # ── Create new suggested task ───────────────────────────────────────
    now = _now()
    cur = conn.execute(
        """INSERT INTO tasks
           (title, description, status, parse_status, priority,
            source_type, source_id, source_snippet, source_date, source_url,
            key_people, action_type, coaching_text, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            title, description, "suggested", "parsed", priority,
            source_type, source_id, source_snippet, source_date, source_url,
            key_people, action_type, coaching_text, now, now,
        ),
    )
    new_id = cur.lastrowid
    try:
        _pi.derive_task_persons(
            conn, new_id, key_people_json=key_people, source_id=source_id,
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"derive_task_persons failed for new task {new_id}: {e}")
    new_task = dict(conn.execute(
        "SELECT * FROM tasks WHERE id = ?", (new_id,),
    ).fetchone())
    return CreateRefreshResult(
        task=new_task, outcome="created",
        dedup_reason=None, matched_task_id=None,
    )


def _augment(
    conn: sqlite3.Connection,
    existing: dict,
    *,
    source_snippet: str | None,
    source_date: str | None,
    priority: int,
    title: str,
    source_id: str | None,
    dedup_reason: str,
) -> CreateRefreshResult:
    """Update an existing live task with newer refresh context.

    Semantics:
      - source_date → MAX(existing, new). Older signal NEVER clobbers newer.
      - priority    → MIN(existing, new). Urgency only ratchets up.
      - source_snippet → overwrite with newer (older context preserved as a
                         task_context row of type 'dedup' below).
      - updated_at → now.
    """
    now = _now()
    new_source_date = _max_date(existing.get("source_date"), source_date)
    new_priority = min(existing["priority"], priority)
    conn.execute(
        """UPDATE tasks
           SET source_snippet = COALESCE(?, source_snippet),
               source_date = ?,
               priority = ?,
               updated_at = ?
           WHERE id = ?""",
        (source_snippet, new_source_date, new_priority, now, existing["id"]),
    )
    # Append refresh context so we don't lose provenance on overwrite.
    ctx_lines = [f"[Refresh dedup — {dedup_reason}] Also surfaced as: {title}"]
    if source_id:
        ctx_lines.append(f"Source: {source_id}")
    if source_snippet:
        ctx_lines.append(f"Snippet: {source_snippet[:300]}")
    conn.execute(
        "INSERT INTO task_context (task_id, context_type, content, query_used) VALUES (?,?,?,?)",
        (existing["id"], "dedup", "\n".join(ctx_lines), None),
    )
    refreshed = dict(conn.execute(
        "SELECT * FROM tasks WHERE id = ?", (existing["id"],),
    ).fetchone())
    return CreateRefreshResult(
        task=refreshed, outcome="augmented",
        dedup_reason=dedup_reason, matched_task_id=existing["id"],
    )
