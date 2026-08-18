"""Command-driven, resumable canonical identity backfill.

This module is SQLite-only. WorkIQ resolution happens in the `/users` command
before `apply_exact_batch` acquires a write lock.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from . import person_identity


class BackfillConflict(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row_dict(row) -> dict:
    return dict(row) if isinstance(row, sqlite3.Row) else {
        "id": row[0],
        "key_people": row[1],
        "source_id": row[2],
        "updated_at": row[3],
    }


def _fingerprint(task: dict) -> str:
    payload = json.dumps(
        {
            "id": task["id"],
            "key_people": task.get("key_people"),
            "source_id": task.get("source_id"),
            "updated_at": task.get("updated_at"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _people(value: str | None) -> list[dict]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(parsed, dict):
        parsed = [parsed]
    return [item for item in parsed if isinstance(item, dict)]


def _source_sender(source_id: str | None) -> str | None:
    if not source_id:
        return None
    parts = source_id.split("::")
    if len(parts) < 2:
        return None
    value = person_identity.normalize_email(parts[1])
    return value if value and "@" in value else None


def _marker(conn: sqlite3.Connection):
    row = conn.execute(
        "SELECT * FROM person_backfill_state WHERE id=1"
    ).fetchone()
    if not row:
        raise RuntimeError("Identity schema is not initialized")
    return dict(row) if isinstance(row, sqlite3.Row) else {
        "id": row[0],
        "status": row[1],
        "last_task_id": row[2],
        "revision": row[3],
        "completed_at": row[4],
        "updated_at": row[5],
    }


def backfill_status(conn: sqlite3.Connection) -> dict:
    marker = _marker(conn)
    max_task_id = conn.execute(
        "SELECT COALESCE(MAX(id),0) FROM tasks WHERE status!='deleted'"
    ).fetchone()[0]
    deferred = 0
    rows = conn.execute(
        "SELECT key_people FROM tasks WHERE status!='deleted'"
    ).fetchall()
    for row in rows:
        value = row["key_people"] if isinstance(row, sqlite3.Row) else row[0]
        for person in _people(value):
            if person.get("unresolved") is True or not any(
                person.get(key)
                for key in ("email", "upn", "aad_object_id", "aadObjectId")
            ):
                deferred += 1
    return {
        **marker,
        "max_eligible_task_id": max_task_id,
        "pending": max_task_id > marker["last_task_id"],
        "deferred_count": deferred,
    }


def plan_batch(
    conn: sqlite3.Connection,
    *,
    batch_size: int = 100,
) -> dict:
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    marker = _marker(conn)
    rows = conn.execute(
        """
        SELECT id, key_people, source_id, updated_at
        FROM tasks
        WHERE id>? AND status!='deleted'
        ORDER BY id
        LIMIT ?
        """,
        (marker["last_task_id"], batch_size),
    ).fetchall()
    tasks = []
    for row in rows:
        task = _row_dict(row)
        lookups = []
        deferred = 0
        for index, person in enumerate(_people(task.get("key_people"))):
            if person.get("unresolved") is True:
                deferred += 1
                continue
            email = person_identity.normalize_email(person.get("email"))
            upn = person_identity.normalize_email(person.get("upn"))
            aad = person_identity.normalize_aad(
                person.get("aad_object_id") or person.get("aadObjectId")
            )
            if not (email or upn or aad):
                deferred += 1
                continue
            lookups.append({
                "person_index": index,
                "role": "key_people",
                "display_name": person.get("name"),
                "email": email,
                "upn": upn,
                "aad_object_id": aad,
                "lookup_kind": "aad_exact" if aad else "email_exact",
                "query_value": aad or email or upn,
            })
        sender = _source_sender(task.get("source_id"))
        if sender:
            lookups.append({
                "person_index": None,
                "role": "sender",
                "display_name": None,
                "email": sender,
                "upn": sender,
                "aad_object_id": None,
                "lookup_kind": "email_exact",
                "query_value": sender,
            })
        tasks.append({
            "task_id": task["id"],
            "fingerprint": _fingerprint(task),
            "lookups": lookups,
            "deferred_count": deferred,
        })
    return {
        "marker_revision": marker["revision"],
        "last_task_id": marker["last_task_id"],
        "tasks": tasks,
    }


def _apply_profile(
    conn: sqlite3.Connection,
    task_id: int,
    profile: dict,
) -> int:
    email = person_identity.normalize_email(profile.get("email"))
    upn = person_identity.normalize_email(profile.get("upn"))
    aad = person_identity.normalize_aad(profile.get("aad_object_id"))
    if not (email or upn or aad):
        raise ValueError("Confirmed profile requires an exact identifier")
    lookup_kind = profile.get("lookup_kind")
    if lookup_kind not in {"aad_exact", "email_exact"}:
        raise ValueError("Unsupported identity lookup kind")
    query_value = profile.get("query_value")
    if lookup_kind == "aad_exact" and person_identity.normalize_aad(
        query_value
    ) != aad:
        raise ValueError("AAD profile does not match the exact query")
    if lookup_kind == "email_exact" and person_identity.normalize_email(
        query_value
    ) not in {email, upn}:
        raise ValueError("Email profile does not match the exact query")
    person_id = person_identity.resolve_person(
        conn,
        display_name=profile.get("display_name"),
        email=email,
        upn=upn,
        aad_object_id=aad,
        evidence_kind=(
            "user_confirmed_name"
            if profile.get("confirmation_mode") == "user"
            else lookup_kind
        ),
        evidence_ref=(
            f"task:{task_id}:person:{profile.get('person_index')}"
            if profile.get("person_index") is not None
            else f"task:{task_id}:sender"
        ),
        lookup_kind=lookup_kind,
        confirmation_mode=profile.get("confirmation_mode") or "exact",
    )
    if person_id is None:
        raise ValueError("Exact profile could not be resolved")
    person_id = person_identity.enrich_confirmed_person(
        conn,
        person_id,
        display_name=profile.get("display_name"),
        email=email,
        upn=upn,
        aad_object_id=aad,
        evidence_kind=(
            "user_confirmed_name"
            if lookup_kind == "user_confirmed_name"
            else lookup_kind
        ),
        evidence_ref=(
            f"task:{task_id}:person:{profile.get('person_index')}"
            if profile.get("person_index") is not None
            else f"task:{task_id}:sender"
        ),
        lookup_kind=lookup_kind,
        confirmation_mode=(
            "user" if lookup_kind == "user_confirmed_name" else "exact"
        ),
    )
    person_identity.link_task_person(
        conn,
        task_id,
        person_id,
        profile["role"],
        evidence_kind=(
            "user_confirmed_name"
            if profile.get("confirmation_mode") == "user"
            else lookup_kind
        ),
        evidence_ref=(
            f"task:{task_id}:person:{profile.get('person_index')}"
            if profile.get("person_index") is not None
            else f"task:{task_id}:sender"
        ),
        lookup_kind=lookup_kind,
        confirmation_mode=profile.get("confirmation_mode") or "exact",
        confirmed_at=_now(),
    )
    return person_id


def apply_exact_batch(
    conn: sqlite3.Connection,
    plan: dict,
    profiles_by_task: dict[int, list[dict]],
) -> dict:
    conn.execute("BEGIN IMMEDIATE")
    try:
        marker = _marker(conn)
        if not plan["tasks"] and marker["status"] == "complete":
            max_task_id = conn.execute(
                "SELECT COALESCE(MAX(id),0) FROM tasks WHERE status!='deleted'"
            ).fetchone()[0]
            if max_task_id <= marker["last_task_id"]:
                conn.rollback()
                return {
                    "status": "complete",
                    "last_task_id": marker["last_task_id"],
                    "revision": marker["revision"],
                    "tasks_applied": 0,
                }
        if (
            marker["revision"] != plan["marker_revision"]
            or marker["last_task_id"] != plan["last_task_id"]
        ):
            raise BackfillConflict("Backfill marker changed after planning")
        tasks = plan["tasks"]
        planned_task_ids = {task["task_id"] for task in tasks}
        if set(profiles_by_task) - planned_task_ids:
            raise BackfillConflict("Profiles contain an unplanned task")
        for planned in tasks:
            row = conn.execute(
                "SELECT id,key_people,source_id,updated_at FROM tasks WHERE id=?",
                (planned["task_id"],),
            ).fetchone()
            if row is None or _fingerprint(_row_dict(row)) != planned["fingerprint"]:
                raise BackfillConflict(
                    f"Task {planned['task_id']} changed after planning"
                )
        for planned in tasks:
            profiles = profiles_by_task.get(planned["task_id"], [])
            lookup_map = {
                (lookup["role"], lookup["person_index"]): lookup
                for lookup in planned["lookups"]
            }
            profile_map = {
                (profile.get("role"), profile.get("person_index")): profile
                for profile in profiles
            }
            if len(profile_map) != len(profiles):
                raise BackfillConflict("Profiles contain duplicate task roles")
            if set(profile_map) != set(lookup_map):
                raise BackfillConflict(
                    f"Task {planned['task_id']} exact lookups are incomplete"
                )
            for key, profile in profile_map.items():
                lookup = lookup_map[key]
                if profile.get("lookup_kind") != lookup["lookup_kind"]:
                    raise BackfillConflict("Profile lookup kind changed")
                if (
                    person_identity.normalize_email(profile.get("query_value"))
                    if lookup["lookup_kind"] == "email_exact"
                    else person_identity.normalize_aad(profile.get("query_value"))
                ) != lookup["query_value"]:
                    raise BackfillConflict("Profile query value changed")
                _apply_profile(conn, planned["task_id"], profile)

        now = _now()
        if tasks:
            last_task_id = tasks[-1]["task_id"]
            status = "in_progress"
            completed_at = None
        else:
            max_task_id = conn.execute(
                "SELECT COALESCE(MAX(id),0) FROM tasks WHERE status!='deleted'"
            ).fetchone()[0]
            if max_task_id > marker["last_task_id"]:
                raise BackfillConflict("Backfill has unplanned eligible tasks")
            last_task_id = marker["last_task_id"]
            status = "complete"
            completed_at = now
        revision = marker["revision"] + 1
        cursor = conn.execute(
            """
            UPDATE person_backfill_state
            SET status=?, last_task_id=?, revision=?, completed_at=?, updated_at=?
            WHERE id=1 AND revision=? AND last_task_id=?
            """,
            (
                status,
                last_task_id,
                revision,
                completed_at,
                now,
                marker["revision"],
                marker["last_task_id"],
            ),
        )
        if cursor.rowcount != 1:
            raise BackfillConflict("Backfill marker compare-and-swap failed")
        conn.commit()
        return {
            "status": status,
            "last_task_id": last_task_id,
            "revision": revision,
            "tasks_applied": len(tasks),
        }
    except Exception:
        conn.rollback()
        raise


def confirm_candidate(
    conn: sqlite3.Connection,
    *,
    task_id: int,
    person_index: int,
    expected_fingerprint: str,
    profile: dict,
) -> int:
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT id,key_people,source_id,updated_at FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()
        if row is None or _fingerprint(_row_dict(row)) != expected_fingerprint:
            raise BackfillConflict("Task changed before identity confirmation")
        lookup_kind = profile.get("lookup_kind")
        query_value = profile.get("query_value")
        if lookup_kind not in {"aad_exact", "email_exact"} or not query_value:
            raise ValueError("Confirmation requires the exact query identity")
        confirmed = {
            **profile,
            "person_index": person_index,
            "role": "key_people",
            "lookup_kind": lookup_kind,
            "query_value": query_value,
            "confirmation_mode": "user",
        }
        person_id = _apply_profile(conn, task_id, confirmed)
        conn.commit()
        return person_id
    except Exception:
        conn.rollback()
        raise
