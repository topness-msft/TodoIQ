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
    deferred_queue = {
        row["status"]: row["count"]
        for row in conn.execute(
            "SELECT status,COUNT(*) AS count FROM person_backfill_deferred "
            "GROUP BY status"
        ).fetchall()
    }
    return {
        **marker,
        "max_eligible_task_id": max_task_id,
        "pending": max_task_id > marker["last_task_id"],
        "deferred_count": deferred,
        "deferred_queue": {
            key: deferred_queue.get(key, 0)
            for key in ("pending", "resolved", "stale")
        },
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
        query_person = person_identity.resolve_person(
            conn, email=query_value, create_if_missing=False
        )
        profile_person = person_identity.resolve_person(
            conn,
            email=email,
            upn=upn,
            aad_object_id=aad,
            create_if_missing=False,
        )
        if (
            query_person is None
            or profile_person is None
            or query_person != profile_person
        ):
            raise ValueError(
                f"Email profile does not match the exact query: {query_value}"
            )
    confirmed_alias = person_identity.normalize_email(
        profile.get("confirmed_alias")
    )
    evidence_ref = (
        f"task:{task_id}:person:{profile.get('person_index')}"
        if profile.get("person_index") is not None
        else f"task:{task_id}:sender"
    )
    if aad:
        person_identity.reconcile_exact_profile(
            conn,
            display_name=profile.get("display_name"),
            email=email,
            upn=upn,
            aad_object_id=aad,
            evidence_ref=evidence_ref,
            lookup_kind=lookup_kind,
        )
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
        evidence_ref=evidence_ref,
        lookup_kind=lookup_kind,
        confirmation_mode=profile.get("confirmation_mode") or "exact",
        create_if_missing=False,
    )
    if person_id is None:
        raise ValueError(
            "Exact profile could not be resolved: "
            f"{profile.get('query_value')}"
        )
    person_id = person_identity.enrich_confirmed_person(
        conn,
        person_id,
        display_name=profile.get("display_name"),
        email=email,
        upn=upn,
        aad_object_id=aad,
        evidence_kind=(
            "user_confirmed_name"
            if profile.get("confirmation_mode") == "user"
            else lookup_kind
        ),
        evidence_ref=evidence_ref,
        lookup_kind=lookup_kind,
        confirmation_mode=profile.get("confirmation_mode") or "exact",
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
        evidence_ref=evidence_ref,
        lookup_kind=lookup_kind,
        confirmation_mode=profile.get("confirmation_mode") or "exact",
        confirmed_at=_now(),
    )
    if confirmed_alias:
        person_identity.confirm_alias(
            conn,
            person_id,
            "email",
            confirmed_alias,
            evidence_ref=evidence_ref,
            lookup_kind=lookup_kind,
        )
    return person_id


def apply_exact_batch(
    conn: sqlite3.Connection,
    plan: dict,
    profiles_by_task: dict[int, list[dict]],
    deferred_by_task: dict[int, list[dict]] | None = None,
) -> dict:
    deferred_by_task = deferred_by_task or {}
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
        if (set(profiles_by_task) | set(deferred_by_task)) - planned_task_ids:
            raise BackfillConflict("Resolution data contains an unplanned task")
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
            deferred = deferred_by_task.get(planned["task_id"], [])
            deferred_map = {
                (item.get("role"), item.get("person_index")): item
                for item in deferred
            }
            if len(deferred_map) != len(deferred):
                raise BackfillConflict("Deferred lookups contain duplicate slots")
            if set(profile_map) & set(deferred_map):
                raise BackfillConflict("A lookup cannot be resolved and deferred")
            if set(profile_map) | set(deferred_map) != set(lookup_map):
                raise BackfillConflict(
                    f"Task {planned['task_id']} lookup outcomes are incomplete"
                )
            for key, profile in profile_map.items():
                lookup = lookup_map[key]
                if profile.get("confirmation_mode") == "user":
                    if person_identity.normalize_name(
                        profile.get("display_name")
                    ) != person_identity.normalize_name(
                        lookup.get("display_name")
                    ):
                        raise BackfillConflict(
                            "Confirmed candidate name does not match the planned person"
                        )
                    confirmed_alias = person_identity.normalize_email(
                        profile.get("confirmed_alias")
                    )
                    if confirmed_alias:
                        if confirmed_alias != lookup["query_value"]:
                            raise BackfillConflict(
                                "Confirmed alias does not match the planned lookup"
                            )
                    elif (
                        profile.get("lookup_kind") != lookup["lookup_kind"]
                        or profile.get("query_value") != lookup["query_value"]
                    ):
                        raise BackfillConflict(
                            "Confirmed profile does not satisfy the planned lookup"
                        )
                elif profile.get("lookup_kind") != lookup["lookup_kind"]:
                    raise BackfillConflict("Profile lookup kind changed")
                if profile.get("confirmation_mode") != "user" or not profile.get(
                    "confirmed_alias"
                ):
                    if (
                        person_identity.normalize_email(profile.get("query_value"))
                        if lookup["lookup_kind"] == "email_exact"
                        else person_identity.normalize_aad(profile.get("query_value"))
                    ) != lookup["query_value"]:
                        raise BackfillConflict("Profile query value changed")
                _apply_profile(conn, planned["task_id"], profile)
            for key, item in deferred_map.items():
                lookup = lookup_map[key]
                if (
                    item.get("lookup_kind") != lookup["lookup_kind"]
                    or item.get("query_value") != lookup["query_value"]
                ):
                    raise BackfillConflict("Deferred lookup changed after planning")
                reason = item.get("defer_reason")
                if reason not in {
                    "not_found",
                    "ambiguous",
                    "external_unresolved",
                    "mcp_unavailable",
                }:
                    raise ValueError("Unsupported defer reason")
                existing = conn.execute(
                    """
                    SELECT id FROM person_backfill_deferred
                    WHERE task_id=? AND role=? AND ifnull(person_index,-1)=?
                      AND lookup_kind=? AND query_value=? AND task_fingerprint=?
                    """,
                    (
                        planned["task_id"],
                        lookup["role"],
                        lookup["person_index"]
                        if lookup["person_index"] is not None
                        else -1,
                        lookup["lookup_kind"],
                        lookup["query_value"],
                        planned["fingerprint"],
                    ),
                ).fetchone()
                if existing:
                    conn.execute(
                        """
                        UPDATE person_backfill_deferred
                        SET defer_reason=?, status='pending',
                            attempts=attempts+1, last_attempt_at=?, updated_at=?
                        WHERE id=?
                        """,
                        (reason, _now(), _now(), existing["id"]),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO person_backfill_deferred (
                            task_id,person_index,role,lookup_kind,query_value,
                            display_name,task_fingerprint,defer_reason,
                            last_attempt_at,updated_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            planned["task_id"],
                            lookup["person_index"],
                            lookup["role"],
                            lookup["lookup_kind"],
                            lookup["query_value"],
                            lookup.get("display_name"),
                            planned["fingerprint"],
                            reason,
                            _now(),
                            _now(),
                        ),
                    )

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
    deferred_id: int | None = None,
) -> int:
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT id,key_people,source_id,updated_at FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()
        if row is None or _fingerprint(_row_dict(row)) != expected_fingerprint:
            if deferred_id is not None:
                conn.execute(
                    "UPDATE person_backfill_deferred SET status='stale', "
                    "updated_at=? WHERE id=? AND status='pending'",
                    (_now(), deferred_id),
                )
                conn.commit()
            raise BackfillConflict("Task changed before identity confirmation")
        people = _people(row["key_people"])
        if person_index < 0 or person_index >= len(people):
            raise ValueError("Person index is no longer valid")
        original = people[person_index]
        original_query = (
            person_identity.normalize_aad(
                original.get("aad_object_id") or original.get("aadObjectId")
            )
            or person_identity.normalize_email(original.get("email"))
            or person_identity.normalize_email(original.get("upn"))
        )
        if not original_query:
            raise ValueError("Task person has no historical exact identifier")
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
            "confirmed_alias": profile.get("confirmed_alias"),
            "confirmation_mode": "user",
        }
        supplied_alias = person_identity.normalize_email(
            profile.get("confirmed_alias")
        )
        if supplied_alias and supplied_alias != original_query:
            raise ValueError(
                "Confirmed alias does not match the historical identity"
            )
        if (
            person_identity.normalize_aad(query_value)
            if lookup_kind == "aad_exact"
            else person_identity.normalize_email(query_value)
        ) != original_query:
            if supplied_alias != original_query:
                raise ValueError(
                    "Confirmed candidate does not replace the historical identity"
                )
        person_id = _apply_profile(conn, task_id, confirmed)
        if deferred_id is not None:
            deferred = conn.execute(
                "SELECT * FROM person_backfill_deferred WHERE id=?",
                (deferred_id,),
            ).fetchone()
            if (
                not deferred
                or deferred["status"] != "pending"
                or deferred["task_id"] != task_id
                or deferred["person_index"] != person_index
                or deferred["task_fingerprint"] != expected_fingerprint
            ):
                raise BackfillConflict("Deferred identity no longer matches")
            conn.execute(
                "UPDATE person_backfill_deferred "
                "SET status='resolved',resolved_at=?,updated_at=? WHERE id=?",
                (_now(), _now(), deferred_id),
            )
        conn.commit()
        return person_id
    except Exception:
        conn.rollback()
        raise


def resolve_deferred_identity(
    conn: sqlite3.Connection,
    *,
    deferred_id: int,
    profile: dict,
) -> int:
    """Resolve one deferred sender/key-person slot after explicit confirmation."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        deferred = conn.execute(
            "SELECT * FROM person_backfill_deferred WHERE id=?",
            (deferred_id,),
        ).fetchone()
        if not deferred or deferred["status"] != "pending":
            raise BackfillConflict("Deferred identity is not pending")
        task = conn.execute(
            "SELECT id,key_people,source_id,updated_at FROM tasks WHERE id=?",
            (deferred["task_id"],),
        ).fetchone()
        if task is None or _fingerprint(_row_dict(task)) != deferred[
            "task_fingerprint"
        ]:
            conn.execute(
                "UPDATE person_backfill_deferred SET status='stale',updated_at=? "
                "WHERE id=?",
                (_now(), deferred_id),
            )
            conn.commit()
            raise BackfillConflict("Task changed before deferred resolution")
        if deferred["display_name"] and person_identity.normalize_name(
            profile.get("display_name")
        ) != person_identity.normalize_name(deferred["display_name"]):
            raise ValueError("Confirmed candidate name does not match deferred name")
        historical_kind = deferred["lookup_kind"]
        historical_query = (
            person_identity.normalize_aad(deferred["query_value"])
            if historical_kind == "aad_exact"
            else person_identity.normalize_email(deferred["query_value"])
        )
        selected_kind = profile.get("lookup_kind")
        selected_query = (
            person_identity.normalize_aad(profile.get("query_value"))
            if selected_kind == "aad_exact"
            else person_identity.normalize_email(profile.get("query_value"))
        )
        supplied_alias = person_identity.normalize_email(
            profile.get("confirmed_alias")
        )
        if supplied_alias and supplied_alias != historical_query:
            raise ValueError(
                "Confirmed alias does not match the historical identity"
            )
        if historical_kind == "aad_exact":
            if selected_kind != "aad_exact" or selected_query != historical_query:
                raise ValueError(
                    "Confirmed candidate does not match the historical identity"
                )
        elif selected_query != historical_query and supplied_alias != historical_query:
            raise ValueError(
                "Confirmed candidate does not replace the historical identity"
            )
        confirmed = {
            **profile,
            "person_index": deferred["person_index"],
            "role": deferred["role"],
            "confirmed_alias": supplied_alias,
            "confirmation_mode": "user",
        }
        person_id = _apply_profile(conn, deferred["task_id"], confirmed)
        conn.execute(
            "UPDATE person_backfill_deferred "
            "SET status='resolved',resolved_at=?,updated_at=? WHERE id=?",
            (_now(), _now(), deferred_id),
        )
        conn.commit()
        return person_id
    except Exception:
        conn.rollback()
        raise
