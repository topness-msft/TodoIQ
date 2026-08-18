import copy
import json
import sqlite3

import pytest

from src.db import init_db
from src.services import person_backfill, person_identity


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


def test_partial_apply_records_deferred_lookup_and_advances_marker():
    conn = connection()
    plan = person_backfill.plan_batch(conn, batch_size=1)
    profiles = {
        1: [{
            "person_index": 0,
            "role": "key_people",
            "display_name": "Alex Example",
            "email": "alex@example.test",
            "upn": "alex@example.test",
            "aad_object_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "lookup_kind": "email_exact",
            "query_value": "alex@example.test",
        }]
    }
    deferred = {
        1: [{
            "person_index": None,
            "role": "sender",
            "lookup_kind": "email_exact",
            "query_value": "sender@example.test",
            "defer_reason": "not_found",
        }]
    }

    result = person_backfill.apply_exact_batch(
        conn, plan, profiles, deferred
    )

    assert result["last_task_id"] == 1
    row = conn.execute("SELECT * FROM person_backfill_deferred").fetchone()
    assert row["task_id"] == 1
    assert row["status"] == "pending"
    assert row["query_value"] == "sender@example.test"
    status = person_backfill.backfill_status(conn)
    assert status["deferred_queue"] == {
        "pending": 1,
        "resolved": 0,
        "stale": 0,
    }


def test_deferred_apply_is_idempotent_for_same_slot():
    conn = connection()
    plan = person_backfill.plan_batch(conn, batch_size=1)
    profiles = {
        1: [{
            "person_index": 0,
            "role": "key_people",
            "display_name": "Alex Example",
            "email": "alex@example.test",
            "upn": "alex@example.test",
            "aad_object_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "lookup_kind": "email_exact",
            "query_value": "alex@example.test",
        }]
    }
    deferred = {
        1: [{
            "person_index": None,
            "role": "sender",
            "lookup_kind": "email_exact",
            "query_value": "sender@example.test",
            "defer_reason": "not_found",
        }]
    }
    person_backfill.apply_exact_batch(conn, plan, profiles, deferred)
    assert conn.execute(
        "SELECT COUNT(*) FROM person_backfill_deferred"
    ).fetchone()[0] == 1


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
                "aad_object_id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
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
    with pytest.raises(ValueError, match="does not match"):
        person_backfill.apply_exact_batch(conn, plan, profiles)
    assert conn.execute("SELECT COUNT(*) FROM person").fetchone()[0] == 0


def test_apply_reuses_previously_confirmed_alias_for_same_canonical_root():
    conn = connection()
    legacy = person_identity.create_person(
        conn, display_name="Legacy", email="legacy@example.test"
    )
    canonical = person_identity.create_person(
        conn,
        display_name="Canonical",
        email="canonical@example.test",
        aad_object_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    )
    person_identity.confirm_alias(
        conn,
        canonical,
        "email",
        "legacy@example.test",
        evidence_ref="task:1:person:0",
        lookup_kind="aad_exact",
    )
    assert person_identity.canonical_root(conn, legacy) == canonical
    profile = {
        "person_index": 0,
        "role": "key_people",
        "display_name": "Canonical",
        "email": "canonical@example.test",
        "upn": "canonical@example.test",
        "aad_object_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "lookup_kind": "email_exact",
        "query_value": "legacy@example.test",
    }
    assert person_backfill._apply_profile(conn, 1, profile) == canonical


def test_apply_profile_never_creates_without_reconciled_exact_identity(
    monkeypatch,
):
    conn = connection()
    monkeypatch.setattr(
        person_identity,
        "reconcile_exact_profile",
        lambda *_args, **_kwargs: None,
    )
    profile = {
        "person_index": 0,
        "role": "key_people",
        "display_name": "Missing",
        "email": "missing@example.test",
        "upn": "missing@example.test",
        "aad_object_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "lookup_kind": "aad_exact",
        "query_value": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    }
    with pytest.raises(ValueError, match="could not be resolved"):
        person_backfill._apply_profile(conn, 1, profile)
    assert conn.execute("SELECT COUNT(*) FROM person").fetchone()[0] == 0


def test_confirm_candidate_requires_and_validates_exact_query():
    conn = connection()
    plan = person_backfill.plan_batch(conn, batch_size=1)
    fingerprint = plan["tasks"][0]["fingerprint"]
    wrong = {
        "display_name": "Wrong Person",
        "email": "wrong@example.test",
        "upn": "wrong@example.test",
        "aad_object_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
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


def test_confirm_candidate_rejects_unrelated_supplied_alias():
    conn = connection()
    plan = person_backfill.plan_batch(conn, batch_size=1)
    fingerprint = plan["tasks"][0]["fingerprint"]
    with pytest.raises(ValueError, match="historical identity"):
        person_backfill.confirm_candidate(
            conn,
            task_id=1,
            person_index=0,
            expected_fingerprint=fingerprint,
            profile={
                "display_name": "Alex Example",
                "email": "alex@example.test",
                "upn": "alex@example.test",
                "aad_object_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "lookup_kind": "email_exact",
                "query_value": "alex@example.test",
                "confirmed_alias": "bob@example.test",
            },
        )
    assert conn.execute("SELECT COUNT(*) FROM person").fetchone()[0] == 0


def test_batch_confirmation_rejects_unrelated_candidate_name():
    conn = connection()
    plan = person_backfill.plan_batch(conn, batch_size=1)
    profiles = {
        1: [{
            "person_index": 0,
            "role": "key_people",
            "display_name": "Wrong Person",
            "email": "canonical@example.test",
            "upn": "canonical@example.test",
            "aad_object_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "lookup_kind": "aad_exact",
            "query_value": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "confirmed_alias": "alex@example.test",
            "confirmation_mode": "user",
        }, {
            "person_index": None,
            "role": "sender",
            "display_name": "Sender Example",
            "email": "sender@example.test",
            "upn": "sender@example.test",
            "aad_object_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            "lookup_kind": "email_exact",
            "query_value": "sender@example.test",
        }]
    }
    with pytest.raises(person_backfill.BackfillConflict, match="name"):
        person_backfill.apply_exact_batch(conn, plan, profiles)


def test_confirmed_candidate_records_stale_historical_email_alias():
    conn = connection()
    plan = person_backfill.plan_batch(conn, batch_size=1)
    fingerprint = plan["tasks"][0]["fingerprint"]
    profile = {
        "display_name": "Alex Example",
        "email": "canonical@example.test",
        "upn": "canonical@example.test",
        "aad_object_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "lookup_kind": "aad_exact",
        "query_value": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "confirmed_alias": "alex@example.test",
    }
    person_id = person_backfill.confirm_candidate(
        conn,
        task_id=1,
        person_index=0,
        expected_fingerprint=fingerprint,
        profile=profile,
    )
    person = conn.execute(
        "SELECT * FROM person WHERE id=?", (person_id,)
    ).fetchone()
    assert person["primary_email"] == "canonical@example.test"
    alias = conn.execute(
        "SELECT * FROM person_alias WHERE person_id=? AND alias_value=?",
        (person_id, "alex@example.test"),
    ).fetchone()
    assert alias["confidence"] == "user"
    assert alias["evidence_kind"] == "user_confirmed_name"


def test_confirm_candidate_closes_matching_deferred_row():
    conn = connection()
    plan = person_backfill.plan_batch(conn, batch_size=1)
    profiles = {
        1: [{
            "person_index": None,
            "role": "sender",
            "display_name": "Sender Example",
            "email": "sender@example.test",
            "upn": "sender@example.test",
            "aad_object_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            "lookup_kind": "email_exact",
            "query_value": "sender@example.test",
        }]
    }
    deferred = {
        1: [{
            "person_index": 0,
            "role": "key_people",
            "lookup_kind": "email_exact",
            "query_value": "alex@example.test",
            "defer_reason": "ambiguous",
        }]
    }
    person_backfill.apply_exact_batch(conn, plan, profiles, deferred)
    row = conn.execute("SELECT * FROM person_backfill_deferred").fetchone()
    profile = {
        "display_name": "Alex Example",
        "email": "canonical@example.test",
        "upn": "canonical@example.test",
        "aad_object_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "lookup_kind": "aad_exact",
        "query_value": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "confirmed_alias": "alex@example.test",
    }
    person_backfill.confirm_candidate(
        conn,
        task_id=1,
        person_index=0,
        expected_fingerprint=row["task_fingerprint"],
        profile=profile,
        deferred_id=row["id"],
    )
    resolved = conn.execute(
        "SELECT * FROM person_backfill_deferred WHERE id=?", (row["id"],)
    ).fetchone()
    assert resolved["status"] == "resolved"
    assert resolved["resolved_at"] is not None


def test_confirm_candidate_marks_deferred_row_stale_after_task_change():
    conn = connection()
    plan = person_backfill.plan_batch(conn, batch_size=1)
    profiles = {
        1: [{
            "person_index": None,
            "role": "sender",
            "display_name": "Sender Example",
            "email": "sender@example.test",
            "upn": "sender@example.test",
            "aad_object_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            "lookup_kind": "email_exact",
            "query_value": "sender@example.test",
        }]
    }
    deferred = {
        1: [{
            "person_index": 0,
            "role": "key_people",
            "lookup_kind": "email_exact",
            "query_value": "alex@example.test",
            "defer_reason": "ambiguous",
        }]
    }
    person_backfill.apply_exact_batch(conn, plan, profiles, deferred)
    row = conn.execute("SELECT * FROM person_backfill_deferred").fetchone()
    conn.execute("UPDATE tasks SET key_people='[]' WHERE id=1")
    conn.commit()
    with pytest.raises(person_backfill.BackfillConflict):
        person_backfill.confirm_candidate(
            conn,
            task_id=1,
            person_index=0,
            expected_fingerprint=row["task_fingerprint"],
            profile={
                "display_name": "Alex Example",
                "email": "alex@example.test",
                "upn": "alex@example.test",
                "aad_object_id": None,
                "lookup_kind": "email_exact",
                "query_value": "alex@example.test",
            },
            deferred_id=row["id"],
        )
    assert conn.execute(
        "SELECT status FROM person_backfill_deferred WHERE id=?", (row["id"],)
    ).fetchone()[0] == "stale"


def test_resolve_deferred_identity_supports_sender_slots():
    conn = connection()
    plan = person_backfill.plan_batch(conn, batch_size=1)
    profiles = {
        1: [{
            "person_index": 0,
            "role": "key_people",
            "display_name": "Alex Example",
            "email": "alex@example.test",
            "upn": "alex@example.test",
            "aad_object_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "lookup_kind": "email_exact",
            "query_value": "alex@example.test",
        }]
    }
    deferred = {
        1: [{
            "person_index": None,
            "role": "sender",
            "lookup_kind": "email_exact",
            "query_value": "sender@example.test",
            "defer_reason": "not_found",
        }]
    }
    person_backfill.apply_exact_batch(conn, plan, profiles, deferred)
    row = conn.execute("SELECT * FROM person_backfill_deferred").fetchone()
    person_id = person_backfill.resolve_deferred_identity(
        conn,
        deferred_id=row["id"],
        profile={
            "display_name": None,
            "email": "canonical-sender@example.test",
            "upn": "canonical-sender@example.test",
            "aad_object_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            "lookup_kind": "aad_exact",
            "query_value": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            "confirmed_alias": "sender@example.test",
        },
    )
    assert person_id
    resolved = conn.execute(
        "SELECT * FROM person_backfill_deferred WHERE id=?", (row["id"],)
    ).fetchone()
    assert resolved["status"] == "resolved"
    assert conn.execute(
        "SELECT COUNT(*) FROM task_person "
        "WHERE task_id=1 AND person_id=? AND role='sender'",
        (person_id,),
    ).fetchone()[0] == 1
    alias = conn.execute(
        "SELECT * FROM person_alias WHERE person_id=? AND alias_value=?",
        (person_id, "sender@example.test"),
    ).fetchone()
    assert alias["confidence"] == "user"
    assert alias["evidence_kind"] == "user_confirmed_name"


@pytest.mark.parametrize("confirmed_alias", [None, "different@example.test"])
def test_resolve_deferred_identity_rejects_unrelated_profile(confirmed_alias):
    conn = connection()
    plan = person_backfill.plan_batch(conn, batch_size=1)
    profiles = {
        1: [{
            "person_index": 0,
            "role": "key_people",
            "display_name": "Alex Example",
            "email": "alex@example.test",
            "upn": "alex@example.test",
            "aad_object_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "lookup_kind": "email_exact",
            "query_value": "alex@example.test",
        }]
    }
    deferred = {
        1: [{
            "person_index": None,
            "role": "sender",
            "lookup_kind": "email_exact",
            "query_value": "sender@example.test",
            "defer_reason": "not_found",
        }]
    }
    person_backfill.apply_exact_batch(conn, plan, profiles, deferred)
    row = conn.execute("SELECT * FROM person_backfill_deferred").fetchone()

    with pytest.raises(ValueError, match="historical identity"):
        person_backfill.resolve_deferred_identity(
            conn,
            deferred_id=row["id"],
            profile={
                "display_name": None,
                "email": "wrong@example.test",
                "upn": "wrong@example.test",
                "aad_object_id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
                "lookup_kind": "aad_exact",
                "query_value": "cccccccc-cccc-cccc-cccc-cccccccccccc",
                "confirmed_alias": confirmed_alias,
            },
        )

    assert conn.execute(
        "SELECT status FROM person_backfill_deferred WHERE id=?", (row["id"],)
    ).fetchone()[0] == "pending"
    assert conn.execute(
        "SELECT COUNT(*) FROM task_person WHERE task_id=1 AND role='sender'"
    ).fetchone()[0] == 0


def test_resolve_deferred_identity_rejects_stale_task():
    conn = connection()
    plan = person_backfill.plan_batch(conn, batch_size=1)
    profiles = {
        1: [{
            "person_index": 0,
            "role": "key_people",
            "display_name": "Alex Example",
            "email": "alex@example.test",
            "upn": "alex@example.test",
            "aad_object_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "lookup_kind": "email_exact",
            "query_value": "alex@example.test",
        }]
    }
    deferred = {
        1: [{
            "person_index": None,
            "role": "sender",
            "lookup_kind": "email_exact",
            "query_value": "sender@example.test",
            "defer_reason": "not_found",
        }]
    }
    person_backfill.apply_exact_batch(conn, plan, profiles, deferred)
    row = conn.execute("SELECT * FROM person_backfill_deferred").fetchone()
    conn.execute(
        "UPDATE tasks SET source_id='chat::changed@example.test::topic' WHERE id=1"
    )
    conn.commit()

    with pytest.raises(person_backfill.BackfillConflict, match="Task changed"):
        person_backfill.resolve_deferred_identity(
            conn,
            deferred_id=row["id"],
            profile={
                "display_name": None,
                "email": "sender@example.test",
                "upn": "sender@example.test",
                "aad_object_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "lookup_kind": "email_exact",
                "query_value": "sender@example.test",
            },
        )

    assert conn.execute(
        "SELECT status FROM person_backfill_deferred WHERE id=?", (row["id"],)
    ).fetchone()[0] == "stale"


def test_resolve_deferred_identity_validates_aad_and_display_name():
    conn = connection()
    aad = "dddddddd-dddd-dddd-dddd-dddddddddddd"
    conn.execute(
        "UPDATE tasks SET key_people=?,updated_at=? WHERE id=1",
        (
            json.dumps([{"name": "Dana Example", "aad_object_id": aad}]),
            "2026-08-02T00:00:00Z",
        ),
    )
    conn.commit()
    plan = person_backfill.plan_batch(conn, batch_size=1)
    profiles = {
        1: [{
            "person_index": None,
            "role": "sender",
            "display_name": "Sender Example",
            "email": "sender@example.test",
            "upn": "sender@example.test",
            "aad_object_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            "lookup_kind": "email_exact",
            "query_value": "sender@example.test",
        }]
    }
    deferred = {
        1: [{
            "person_index": 0,
            "role": "key_people",
            "lookup_kind": "aad_exact",
            "query_value": aad,
            "display_name": "Dana Example",
            "defer_reason": "ambiguous",
        }]
    }
    person_backfill.apply_exact_batch(conn, plan, profiles, deferred)
    row = conn.execute("SELECT * FROM person_backfill_deferred").fetchone()
    profile = {
        "display_name": "Wrong Person",
        "email": "dana@example.test",
        "upn": "dana@example.test",
        "aad_object_id": aad,
        "lookup_kind": "aad_exact",
        "query_value": aad,
    }

    with pytest.raises(ValueError, match="name"):
        person_backfill.resolve_deferred_identity(
            conn, deferred_id=row["id"], profile=profile
        )
    profile["display_name"] = "Dana Example"
    person_id = person_backfill.resolve_deferred_identity(
        conn, deferred_id=row["id"], profile=profile
    )

    assert person_id
    assert conn.execute(
        "SELECT status FROM person_backfill_deferred WHERE id=?", (row["id"],)
    ).fetchone()[0] == "resolved"
