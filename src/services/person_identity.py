"""Durable canonical identity and task-person recall.

Only exact AAD IDs, exact email/UPN values, or explicitly user-confirmed name
aliases resolve identity. Display names and inferred aliases are recall-only.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from typing import Iterable

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_email(value: str | None) -> str | None:
    normalized = (value or "").strip().lower()
    return normalized or None


def normalize_name(value: str | None) -> str | None:
    normalized = re.sub(r"\s+", " ", (value or "").strip()).lower()
    return normalized or None


def normalize_aad(value: str | None) -> str | None:
    normalized = (value or "").strip().lower()
    return normalized or None


def canonical_root(conn: sqlite3.Connection, person_id: int) -> int:
    visited = set()
    current = person_id
    while current not in visited:
        visited.add(current)
        row = conn.execute(
            "SELECT canonical_person_id FROM person WHERE id=?", (current,)
        ).fetchone()
        if row is None:
            return current
        next_id = row["canonical_person_id"] if isinstance(
            row, sqlite3.Row
        ) else row[0]
        if next_id is None or next_id == current:
            return current
        current = next_id
    logger.warning("canonical_person_id cycle involving %s", sorted(visited))
    return min(visited)


def _find_by_aad(conn: sqlite3.Connection, aad: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM person WHERE lower(aad_object_id)=?", (aad,)
    ).fetchone()
    return row["id"] if isinstance(row, sqlite3.Row) and row else (
        row[0] if row else None
    )


def _roots_by_primary_email(
    conn: sqlite3.Connection, email: str
) -> set[int]:
    rows = conn.execute(
        "SELECT id FROM person WHERE lower(primary_email)=? ORDER BY id",
        (email,),
    ).fetchall()
    return {
        canonical_root(
            conn, row["id"] if isinstance(row, sqlite3.Row) else row[0]
        )
        for row in rows
    }


def _roots_by_alias(
    conn: sqlite3.Connection,
    kind: str,
    value: str,
    *,
    allowed_confidences: tuple[str, ...],
) -> set[int]:
    placeholders = ",".join("?" for _ in allowed_confidences)
    rows = conn.execute(
        f"""
        SELECT person_id FROM person_alias
        WHERE alias_kind=? AND alias_value=?
          AND confidence IN ({placeholders})
        ORDER BY person_id
        """,
        (kind, value, *allowed_confidences),
    ).fetchall()
    return {
        canonical_root(
            conn,
            row["person_id"] if isinstance(row, sqlite3.Row) else row[0],
        )
        for row in rows
    }


def _find_by_alias(
    conn: sqlite3.Connection,
    kind: str,
    value: str,
    *,
    allowed_confidences: tuple[str, ...],
) -> int | None:
    roots = _roots_by_alias(
        conn,
        kind,
        value,
        allowed_confidences=allowed_confidences,
    )
    if len(roots) != 1:
        if len(roots) > 1:
            logger.warning(
                "Alias collision kind=%s value=%s roots=%s",
                kind,
                value,
                sorted(roots),
            )
        return None
    return next(iter(roots))


def resolve_person(
    conn: sqlite3.Connection,
    *,
    display_name: str | None = None,
    email: str | None = None,
    aad_object_id: str | None = None,
    upn: str | None = None,
    create_if_missing: bool = True,
    evidence_kind: str | None = None,
    evidence_ref: str | None = None,
    lookup_kind: str | None = None,
    confirmation_mode: str | None = None,
) -> int | None:
    email = normalize_email(email)
    upn = normalize_email(upn)
    aad = normalize_aad(aad_object_id)

    exact_matches = set()
    if aad:
        person_id = _find_by_aad(conn, aad)
        if person_id is not None:
            exact_matches.add(canonical_root(conn, person_id))
        aad_roots = _roots_by_alias(
            conn, "aad", aad, allowed_confidences=("aad", "user")
        )
        if len(aad_roots) > 1:
            return None
        exact_matches.update(aad_roots)
    if email:
        email_roots = _roots_by_alias(
            conn, "email", email, allowed_confidences=("email", "user")
        )
        email_roots.update(_roots_by_primary_email(conn, email))
        if len(email_roots) > 1:
            return None
        exact_matches.update(email_roots)
    if upn:
        upn_roots = _roots_by_alias(
            conn, "upn", upn, allowed_confidences=("email", "user")
        )
        if len(upn_roots) > 1:
            return None
        exact_matches.update(upn_roots)
    if len(exact_matches) > 1:
        logger.warning(
            "Exact identity attributes resolve to conflicting roots: %s",
            sorted(exact_matches),
        )
        return None
    if exact_matches:
        return next(iter(exact_matches))
    name = normalize_name(display_name)
    if name:
        person_id = _find_by_alias(
            conn, "name", name, allowed_confidences=("user",)
        )
        if person_id is not None:
            return person_id
    if not create_if_missing or not (aad or email or upn):
        return None
    conn.execute("SAVEPOINT resolve_person_create")
    try:
        person_id = create_person(
            conn,
            display_name=display_name or email or upn or aad,
            email=email,
            aad_object_id=aad,
            upn=upn,
            evidence_kind=evidence_kind,
            evidence_ref=evidence_ref,
            lookup_kind=lookup_kind,
            confirmation_mode=confirmation_mode,
        )
        conn.execute("RELEASE SAVEPOINT resolve_person_create")
        return person_id
    except sqlite3.IntegrityError:
        conn.execute("ROLLBACK TO SAVEPOINT resolve_person_create")
        conn.execute("RELEASE SAVEPOINT resolve_person_create")
        # Another exact writer may have won after our read. Re-resolve against
        # the unique authoritative alias index rather than creating a fork.
        return resolve_person(
            conn,
            display_name=display_name,
            email=email,
            aad_object_id=aad,
            upn=upn,
            create_if_missing=False,
        )


def enrich_confirmed_person(
    conn: sqlite3.Connection,
    person_id: int,
    *,
    display_name: str | None,
    email: str | None,
    aad_object_id: str | None,
    upn: str | None,
    evidence_kind: str,
    evidence_ref: str,
    lookup_kind: str,
    confirmation_mode: str,
) -> int:
    root = canonical_root(conn, person_id)
    row = conn.execute(
        "SELECT * FROM person WHERE id=?", (root,)
    ).fetchone()
    if not row:
        raise ValueError("Confirmed person does not exist")
    existing_aad = normalize_aad(row["aad_object_id"])
    aad = normalize_aad(aad_object_id)
    if existing_aad and aad and existing_aad != aad:
        raise ValueError("Confirmed AAD ID conflicts with canonical person")
    email = normalize_email(email)
    upn = normalize_email(upn)
    conn.execute(
        """
        UPDATE person
        SET display_name=COALESCE(NULLIF(?,''),display_name),
            primary_email=CASE WHEN ? IS NOT NULL THEN ? ELSE primary_email END,
            aad_object_id=COALESCE(aad_object_id,?),
            updated_at=?
        WHERE id=?
        """,
        (display_name, email, email, aad, _now(), root),
    )
    for kind, value, confidence in (
        ("aad", aad, "aad"),
        ("email", email, "email"),
        ("upn", upn, "email"),
    ):
        if value:
            add_alias(
                conn,
                root,
                kind,
                value,
                confidence,
                evidence_kind=evidence_kind,
                evidence_ref=evidence_ref,
                lookup_kind=lookup_kind,
                confirmation_mode=confirmation_mode,
                confirmed_at=_now(),
            )
    return root


def create_person(
    conn: sqlite3.Connection,
    *,
    display_name: str,
    email: str | None = None,
    aad_object_id: str | None = None,
    upn: str | None = None,
    evidence_kind: str | None = None,
    evidence_ref: str | None = None,
    lookup_kind: str | None = None,
    confirmation_mode: str | None = None,
) -> int:
    now = _now()
    email = normalize_email(email)
    upn = normalize_email(upn)
    aad = normalize_aad(aad_object_id)
    cursor = conn.execute(
        """
        INSERT INTO person (
            display_name, primary_email, aad_object_id, created_at, updated_at
        ) VALUES (?,?,?,?,?)
        """,
        (display_name, email, aad, now, now),
    )
    person_id = cursor.lastrowid
    if aad:
        add_alias(
            conn,
            person_id,
            "aad",
            aad,
            "aad",
            evidence_kind=evidence_kind,
            evidence_ref=evidence_ref,
            lookup_kind=lookup_kind or "aad_exact",
            confirmation_mode=confirmation_mode,
        )
    if email:
        add_alias(
            conn,
            person_id,
            "email",
            email,
            "email",
            evidence_kind=evidence_kind,
            evidence_ref=evidence_ref,
            lookup_kind=lookup_kind or "email_exact",
            confirmation_mode=confirmation_mode,
        )
    if upn:
        add_alias(
            conn,
            person_id,
            "upn",
            upn,
            "email",
            evidence_kind=evidence_kind,
            evidence_ref=evidence_ref,
            lookup_kind=lookup_kind or "email_exact",
            confirmation_mode=confirmation_mode,
        )
    name = normalize_name(display_name)
    if name:
        add_alias(
            conn,
            person_id,
            "name",
            name,
            "name",
            evidence_kind=evidence_kind,
            evidence_ref=evidence_ref,
            lookup_kind=lookup_kind,
            confirmation_mode=confirmation_mode,
        )
    return person_id


def add_alias(
    conn: sqlite3.Connection,
    person_id: int,
    kind: str,
    value: str,
    confidence: str,
    *,
    evidence_kind: str | None = None,
    evidence_ref: str | None = None,
    observed_at: str | None = None,
    confirmation_mode: str | None = None,
    confirmed_at: str | None = None,
    lookup_kind: str | None = None,
) -> None:
    normalized = (
        normalize_email(value)
        if kind in {"email", "upn"}
        else normalize_aad(value)
        if kind == "aad"
        else normalize_name(value)
    )
    if not normalized:
        return
    root = canonical_root(conn, person_id)
    conn.execute(
        """
        INSERT OR IGNORE INTO person_alias (
            person_id, alias_kind, alias_value, confidence,
            evidence_kind, evidence_ref, observed_at, confirmation_mode,
            confirmed_at, lookup_kind, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            root,
            kind,
            normalized,
            confidence,
            evidence_kind,
            evidence_ref,
            observed_at or _now(),
            confirmation_mode,
            confirmed_at,
            lookup_kind,
            _now(),
        ),
    )
    if kind in {"aad", "email", "upn"} and confidence in {
        "aad",
        "email",
        "user",
    }:
        roots = _roots_by_alias(
            conn,
            kind,
            normalized,
            allowed_confidences=("aad", "email", "user"),
        )
        if roots != {root}:
            raise sqlite3.IntegrityError(
                f"Authoritative alias already belongs to another person: {normalized}"
            )


def merge_persons(
    conn: sqlite3.Connection,
    *,
    losing_id: int,
    winning_id: int,
    reason: str | None = None,
) -> None:
    losing_root = canonical_root(conn, losing_id)
    winning_root = canonical_root(conn, winning_id)
    if losing_root == winning_root:
        return
    if canonical_root(conn, winning_root) == losing_root:
        raise ValueError("Person merge would create a cycle")
    conn.execute(
        "UPDATE person SET canonical_person_id=?, updated_at=? WHERE id=?",
        (winning_root, _now(), losing_root),
    )
    conn.execute(
        """
        INSERT INTO person_merge_history (
            losing_id, winning_id, reason, created_at
        ) VALUES (?,?,?,?)
        """,
        (losing_root, winning_root, reason, _now()),
    )


def unmerge_persons(conn: sqlite3.Connection, losing_id: int) -> None:
    conn.execute(
        "UPDATE person SET canonical_person_id=NULL, updated_at=? WHERE id=?",
        (_now(), losing_id),
    )
    conn.execute(
        "UPDATE person_merge_history SET undone_at=? "
        "WHERE losing_id=? AND undone_at IS NULL",
        (_now(), losing_id),
    )


def _parse_key_people(value: str | None) -> list[dict]:
    if not value:
        return []
    try:
        people = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(people, dict):
        people = [people]
    return [person for person in people if isinstance(person, dict)]


def _source_sender(source_id: str | None) -> str | None:
    if not source_id:
        return None
    parts = source_id.split("::")
    if len(parts) < 2:
        return None
    sender = normalize_email(parts[1])
    return sender if sender and "@" in sender else None


def link_task_person(
    conn: sqlite3.Connection,
    task_id: int,
    person_id: int,
    role: str,
    *,
    evidence_kind: str | None = None,
    evidence_ref: str | None = None,
    lookup_kind: str | None = None,
    confirmation_mode: str | None = None,
    confirmed_at: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO task_person (
            task_id, person_id, role, evidence_kind, evidence_ref, observed_at,
            confirmation_mode, confirmed_at, lookup_kind
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            task_id,
            canonical_root(conn, person_id),
            role,
            evidence_kind,
            evidence_ref,
            _now(),
            confirmation_mode,
            confirmed_at,
            lookup_kind,
        ),
    )


def derive_task_persons(
    conn: sqlite3.Connection,
    task_id: int,
    *,
    key_people_json: str | None,
    source_id: str | None,
) -> list[int]:
    conn.execute("DELETE FROM task_person WHERE task_id=?", (task_id,))
    written = set()
    sender = _source_sender(source_id)
    if sender:
        person_id = resolve_person(
            conn,
            email=sender,
            evidence_kind="legacy_task_exact",
            evidence_ref=f"task:{task_id}:sender",
            lookup_kind="email_exact",
        )
        if person_id is not None:
            link_task_person(
                conn,
                task_id,
                person_id,
                "sender",
                evidence_kind="legacy_task_exact",
                evidence_ref=f"task:{task_id}:sender",
                lookup_kind="email_exact",
            )
            written.add(person_id)

    for index, person in enumerate(_parse_key_people(key_people_json)):
        if person.get("unresolved") is True:
            continue
        email = normalize_email(person.get("email"))
        upn = normalize_email(person.get("upn"))
        aad = normalize_aad(
            person.get("aad_object_id") or person.get("aadObjectId")
        )
        if not (email or upn or aad):
            continue
        person_id = resolve_person(
            conn,
            display_name=person.get("name"),
            email=email,
            upn=upn,
            aad_object_id=aad,
            evidence_kind="legacy_task_exact",
            evidence_ref=f"task:{task_id}:person:{index}",
            lookup_kind="aad_exact" if aad else "email_exact",
        )
        if person_id is None:
            continue
        link_task_person(
            conn,
            task_id,
            person_id,
            "key_people",
            evidence_kind="legacy_task_exact",
            evidence_ref=f"task:{task_id}:person:{index}",
            lookup_kind="aad_exact" if aad else "email_exact",
        )
        written.add(person_id)
    return sorted(written)


def get_task_person_ids(conn: sqlite3.Connection, task_id: int) -> set[int]:
    rows = conn.execute(
        "SELECT DISTINCT person_id FROM task_person WHERE task_id=?", (task_id,)
    ).fetchall()
    return {
        canonical_root(
            conn,
            row["person_id"] if isinstance(row, sqlite3.Row) else row[0],
        )
        for row in rows
    }


def find_tasks_sharing_persons(
    conn: sqlite3.Connection,
    person_ids: Iterable[int],
    *,
    statuses: tuple[str, ...] = (
        "active",
        "in_progress",
        "waiting",
        "snoozed",
        "suggested",
    ),
    exclude_task_ids: Iterable[int] = (),
    limit: int = 200,
) -> list[int]:
    roots = {canonical_root(conn, person_id) for person_id in person_ids}
    if not roots:
        return []
    status_marks = ",".join("?" for _ in statuses)
    excluded = list(exclude_task_ids)
    sql = (
        "SELECT t.id, t.created_at, tp.person_id "
        "FROM task_person tp JOIN tasks t ON t.id=tp.task_id "
        f"WHERE t.status IN ({status_marks})"
    )
    params = list(statuses)
    if excluded:
        marks = ",".join("?" for _ in excluded)
        sql += f" AND t.id NOT IN ({marks})"
        params.extend(excluded)
    task_columns = {
        row["name"] if isinstance(row, sqlite3.Row) else row[1]
        for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
    }
    if "shadow_dup_of" in task_columns:
        sql += " AND t.shadow_dup_of IS NULL"
    sql += " ORDER BY t.created_at DESC, t.id DESC"
    result = []
    seen = set()
    for row in conn.execute(sql, params).fetchall():
        task_id = row["id"] if isinstance(row, sqlite3.Row) else row[0]
        person_id = row["person_id"] if isinstance(row, sqlite3.Row) else row[2]
        if canonical_root(conn, person_id) in roots and task_id not in seen:
            result.append(task_id)
            seen.add(task_id)
            if len(result) >= limit:
                break
    return result


AUDITED_ALIAS_SEEDS = (
    ("kanikaramji", "kanika.ramji@microsoft.com"),
    ("ruih", "rui.hu@microsoft.com"),
    ("justw", "justin.walker@microsoft.com"),
    ("spant", "saurabh.pant@microsoft.com"),
    ("alex.powell", "apowell@microsoft.com"),
)


def seed_audited_aliases(
    conn: sqlite3.Connection,
    seeds: Iterable[tuple[str, str]] = AUDITED_ALIAS_SEEDS,
    *,
    commit: bool = False,
) -> int:
    """Attach recall-only aliases only when the canonical email already exists."""
    added = 0
    for alias, canonical_email in seeds:
        person_id = resolve_person(
            conn, email=canonical_email, create_if_missing=False
        )
        if person_id is None:
            continue
        before = conn.total_changes
        add_alias(
            conn,
            person_id,
            "name",
            alias,
            "inferred",
            evidence_kind="audited_alias_seed",
            evidence_ref=f"alias-seed:{alias}",
            lookup_kind="email_exact",
            confirmation_mode="audited",
        )
        if conn.total_changes > before:
            added += 1
    if commit:
        conn.commit()
    return added
