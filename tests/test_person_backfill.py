import copy
import json
import sqlite3

import pytest

from src.db import init_db
from src.services import person_backfill


def connection():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    conn.execute(
        "INSERT INTO tasks (id,title,status,key_people,source_id,updated_at) "
        "VALUES (1,'First','active',?,?,'2026-08-01T00:00:00Z')",
        (
            '[{"name":"Alex Example","email":"alex@example.test"}]',
            "chat::sender@example.test::topic",
        ),
    )
    conn.execute(
        "INSERT INTO tasks (id,title,status,key_people,updated_at) "
        "VALUES (2,'Second','active',?,'2026-08-01T00:00:00Z')",
        ('[{"name":"Name Only","unresolved":true}]',),
    )
    conn.commit()
    return conn


def snapshot(conn):
    return {
        table: [
            tuple(row)
            for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
        ]
        for table in (
            "tasks",
            "person",
            "person_alias",
            "task_person",
            "person_backfill_state",
        )
    }


def test_backfill_dry_run_makes_zero_writes_and_leaves_marker_unchanged():
    conn = connection()
    before = snapshot(conn)

    plan = person_backfill.plan_batch(conn, batch_size=100)

    assert [task["task_id"] for task in plan["tasks"]] == [1, 2]
    assert plan["tasks"][1]["deferred_count"] == 1
    assert snapshot(conn) == before


def test_backfill_apply_is_atomic_and_resumes_from_marker(monkeypatch):
    conn = connection()
    plan = person_backfill.plan_batch(conn, batch_size=1)
    profiles = {
        1: [
            {
                "person_index": 0,
                "role": "key_people",
                "display_name": "Alex Example",
                "email": "alex@example.test",
                "upn": "alex@example.test",
                "aad_object_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "lookup_kind": "email_exact",
                "query_value": "alex@example.test",
            },
            {
                "person_index": None,
                "role": "sender",
                "display_name": "Sender Example",
                "email": "sender@example.test",
                "upn": "sender@example.test",
                "aad_object_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "lookup_kind": "email_exact",
                "query_value": "sender@example.test",
            },
        ]
    }
    before = snapshot(conn)
    original = person_backfill._apply_profile

    def fail_after_first(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected failure")

    monkeypatch.setattr(person_backfill, "_apply_profile", fail_after_first)
    with pytest.raises(RuntimeError, match="injected failure"):
        person_backfill.apply_exact_batch(conn, plan, profiles)
    assert snapshot(conn) == before

    monkeypatch.setattr(person_backfill, "_apply_profile", original)
    result = person_backfill.apply_exact_batch(conn, plan, profiles)
    assert result["last_task_id"] == 1
    assert result["revision"] == 1
    assert conn.execute("SELECT COUNT(*) FROM task_person").fetchone()[0] == 2

    resumed = person_backfill.plan_batch(conn, batch_size=100)
    assert [task["task_id"] for task in resumed["tasks"]] == [2]


def test_apply_rejects_stale_task_fingerprint():
    conn = connection()
    plan = person_backfill.plan_batch(conn, batch_size=1)
    conn.execute("UPDATE tasks SET key_people='[]' WHERE id=1")
    conn.commit()

    with pytest.raises(person_backfill.BackfillConflict):
        person_backfill.apply_exact_batch(conn, plan, {})


def test_completion_requires_empty_next_batch():
    conn = connection()
    first = person_backfill.plan_batch(conn, batch_size=100)
    person_backfill.apply_exact_batch(
        conn,
        first,
        {
            1: [
                {
                    "person_index": 0,
                    "role": "key_people",
                    "display_name": "Alex Example",
                    "email": "alex@example.test",
                    "upn": "alex@example.test",
                    "aad_object_id": None,
                    "lookup_kind": "email_exact",
                    "query_value": "alex@example.test",
                },
                {
                    "person_index": None,
                    "role": "sender",
                    "display_name": "Sender Example",
                    "email": "sender@example.test",
                    "upn": "sender@example.test",
                    "aad_object_id": None,
                    "lookup_kind": "email_exact",
                    "query_value": "sender@example.test",
                },
            ]
        },
    )
    empty = person_backfill.plan_batch(conn, batch_size=100)
    assert empty["tasks"] == []
    completed = person_backfill.apply_exact_batch(conn, empty, {})
    assert completed["status"] == "complete"
    assert person_backfill.apply_exact_batch(conn, empty, {}) == completed
    repeated = person_backfill.apply_exact_batch(
        conn, person_backfill.plan_batch(conn, batch_size=100), {}
    )
    assert repeated == completed


def test_backfill_never_mutates_task_rows():
    conn = connection()
    before = [
        dict(row)
        for row in conn.execute("SELECT * FROM tasks ORDER BY id").fetchall()
    ]
    plan = person_backfill.plan_batch(conn, batch_size=100)
    person_backfill.apply_exact_batch(
        conn,
        plan,
        {
            1: [
                {
                    "person_index": 0,
                    "role": "key_people",
                    "display_name": "Alex Example",
                    "email": "alex@example.test",
                    "upn": "alex@example.test",
                    "aad_object_id": None,
                    "lookup_kind": "email_exact",
                    "query_value": "alex@example.test",
                },
                {
                    "person_index": None,
                    "role": "sender",
                    "display_name": "Sender Example",
                    "email": "sender@example.test",
                    "upn": "sender@example.test",
                    "aad_object_id": None,
                    "lookup_kind": "email_exact",
                    "query_value": "sender@example.test",
                },
            ]
        },
    )
    after = [
        dict(row)
        for row in conn.execute("SELECT * FROM tasks ORDER BY id").fetchall()
    ]
    assert after == before


def test_apply_rejects_incomplete_exact_resolution_without_advancing():
    conn = connection()
    plan = person_backfill.plan_batch(conn, batch_size=1)
    marker_before = dict(
        conn.execute("SELECT * FROM person_backfill_state WHERE id=1").fetchone()
    )

    with pytest.raises(person_backfill.BackfillConflict, match="incomplete"):
        person_backfill.apply_exact_batch(conn, plan, {})

    marker_after = dict(
        conn.execute("SELECT * FROM person_backfill_state WHERE id=1").fetchone()
    )
    assert marker_after == marker_before
    assert conn.execute("SELECT COUNT(*) FROM person").fetchone()[0] == 0


def test_apply_rejects_profile_that_does_not_match_exact_query():
    conn = connection()
    plan = person_backfill.plan_batch(conn, batch_size=1)
    profiles = {
        1: [
            {
                "person_index": 0,
                "role": "key_people",
                "display_name": "Wrong Person",
                "email": "wrong@example.test",
                "upn": "wrong@example.test",
                "aad_object_id": None,
                "lookup_kind": "email_exact",
                "query_value": "alex@example.test",
            },
            {
                "person_index": None,
                "role": "sender",
                "display_name": "Sender Example",
                "email": "sender@example.test",
                "upn": "sender@example.test",
                "aad_object_id": None,
                "lookup_kind": "email_exact",
                "query_value": "sender@example.test",
            },
        ]
    }
    with pytest.raises(ValueError, match="does not match"):
        person_backfill.apply_exact_batch(conn, plan, profiles)
    assert conn.execute("SELECT COUNT(*) FROM person").fetchone()[0] == 0


def test_confirm_candidate_requires_and_validates_exact_query():
    conn = connection()
    plan = person_backfill.plan_batch(conn, batch_size=1)
    fingerprint = plan["tasks"][0]["fingerprint"]
    wrong = {
        "display_name": "Wrong Person",
        "email": "wrong@example.test",
        "upn": "wrong@example.test",
        "aad_object_id": None,
        "lookup_kind": "email_exact",
        "query_value": "alex@example.test",
    }
    with pytest.raises(ValueError, match="does not match"):
        person_backfill.confirm_candidate(
            conn,
            task_id=1,
            person_index=0,
            expected_fingerprint=fingerprint,
            profile=wrong,
        )
    assert conn.execute("SELECT COUNT(*) FROM person").fetchone()[0] == 0

    exact = {
        "display_name": "Alex Example",
        "email": "alex@example.test",
        "upn": "alex@example.test",
        "aad_object_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "lookup_kind": "email_exact",
        "query_value": "alex@example.test",
    }
    person_id = person_backfill.confirm_candidate(
        conn,
        task_id=1,
        person_index=0,
        expected_fingerprint=fingerprint,
        profile=exact,
    )
    row = conn.execute("SELECT * FROM person WHERE id=?", (person_id,)).fetchone()
    assert row["primary_email"] == "alex@example.test"
    assert row["aad_object_id"] == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    link = conn.execute(
        "SELECT * FROM task_person WHERE task_id=1 AND person_id=?",
        (person_id,),
    ).fetchone()
    assert link["confirmation_mode"] == "user"
    assert link["lookup_kind"] == "email_exact"
