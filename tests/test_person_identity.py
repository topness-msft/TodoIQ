import sqlite3

import pytest

from src.db import init_db
from src.services import person_identity as identity


def connection():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def test_exact_email_and_aad_resolution_are_canonical():
    conn = connection()
    person_id = identity.create_person(
        conn,
        display_name="Alex Example",
        email="alex@example.test",
        aad_object_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        upn="aexample@example.test",
    )

    assert identity.resolve_person(
        conn, email=" ALEX@example.test ", create_if_missing=False
    ) == person_id
    assert identity.resolve_person(
        conn,
        aad_object_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        create_if_missing=False,
    ) == person_id


def test_equal_email_and_upn_resolve_through_both_alias_kinds():
    conn = connection()
    person_id = identity.create_person(
        conn,
        display_name="Alex Example",
        email="alex@example.test",
        upn="alex@example.test",
    )
    assert identity.resolve_person(
        conn, email="alex@example.test", create_if_missing=False
    ) == person_id
    assert identity.resolve_person(
        conn, upn="alex@example.test", create_if_missing=False
    ) == person_id


def test_alias_collision_with_multiple_roots_fails_closed():
    conn = connection()
    first = identity.create_person(conn, display_name="First")
    second = identity.create_person(conn, display_name="Second")
    conn.execute("DROP INDEX idx_person_alias_exact_identity")
    conn.execute(
        "INSERT INTO person_alias "
        "(person_id,alias_kind,alias_value,confidence) VALUES (?,?,?,?)",
        (first, "email", "shared@example.test", "email"),
    )
    conn.execute(
        "INSERT INTO person_alias "
        "(person_id,alias_kind,alias_value,confidence) VALUES (?,?,?,?)",
        (second, "email", "shared@example.test", "email"),
    )

    assert identity.resolve_person(
        conn, email="shared@example.test", create_if_missing=False
    ) is None
    assert identity.resolve_person(
        conn, email="shared@example.test", create_if_missing=True
    ) is None
    assert conn.execute("SELECT COUNT(*) FROM person").fetchone()[0] == 2


def test_conflicting_exact_aad_and_email_roots_fail_closed():
    conn = connection()
    first = identity.create_person(
        conn,
        display_name="First",
        email="first@example.test",
        aad_object_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    )
    second = identity.create_person(
        conn,
        display_name="Second",
        email="second@example.test",
        aad_object_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    )
    assert first != second
    assert identity.resolve_person(
        conn,
        email="first@example.test",
        aad_object_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        create_if_missing=False,
    ) is None


def test_canonical_root_walks_deep_chain_and_detects_cycle():
    conn = connection()
    people = [
        identity.create_person(conn, display_name=f"Person {index}")
        for index in range(4)
    ]
    conn.execute(
        "UPDATE person SET canonical_person_id=? WHERE id=?",
        (people[1], people[0]),
    )
    conn.execute(
        "UPDATE person SET canonical_person_id=? WHERE id=?",
        (people[2], people[1]),
    )
    conn.execute(
        "UPDATE person SET canonical_person_id=? WHERE id=?",
        (people[3], people[2]),
    )
    assert identity.canonical_root(conn, people[0]) == people[3]

    conn.execute(
        "UPDATE person SET canonical_person_id=? WHERE id=?",
        (people[0], people[3]),
    )
    assert identity.canonical_root(conn, people[0]) in set(people)


def test_unresolved_and_name_only_people_do_not_create_identity_rows():
    conn = connection()
    conn.execute(
        "INSERT INTO tasks (id,title,key_people) VALUES (1,?,?)",
        (
            "Ambiguous task",
            '[{"name":"Alex Example","email":"candidate@example.test",'
            '"unresolved":true},{"name":"Name Only"}]',
        ),
    )

    written = identity.derive_task_persons(
        conn,
        1,
        key_people_json=conn.execute(
            "SELECT key_people FROM tasks WHERE id=1"
        ).fetchone()[0],
        source_id=None,
    )

    assert written == []
    assert conn.execute("SELECT COUNT(*) FROM person").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM task_person").fetchone()[0] == 0


def test_task_links_are_replaced_from_confirmed_people_only():
    conn = connection()
    conn.execute(
        "INSERT INTO tasks (id,title,key_people,source_id) VALUES (1,?,?,?)",
        (
            "Known people",
            '[{"name":"Alex Example","email":"alex@example.test"}]',
            "chat::sender@example.test::topic",
        ),
    )
    identity.derive_task_persons(
        conn,
        1,
        key_people_json='[{"name":"Alex Example","email":"alex@example.test"}]',
        source_id="chat::sender@example.test::topic",
    )
    assert conn.execute("SELECT COUNT(*) FROM task_person").fetchone()[0] == 2

    identity.derive_task_persons(
        conn,
        1,
        key_people_json='[{"name":"Alex Example","unresolved":true}]',
        source_id=None,
    )
    assert conn.execute("SELECT COUNT(*) FROM task_person").fetchone()[0] == 0


def test_find_tasks_sharing_people_does_not_require_shadow_columns():
    conn = connection()
    person_id = identity.create_person(
        conn, display_name="Alex Example", email="alex@example.test"
    )
    conn.execute("INSERT INTO tasks (id,title,status) VALUES (1,'Task','active')")
    conn.execute(
        "INSERT INTO task_person (task_id,person_id,role) VALUES (1,?,'key_people')",
        (person_id,),
    )
    assert identity.find_tasks_sharing_persons(conn, [person_id]) == [1]


def test_confirmed_profile_enriches_existing_email_person_with_aad_alias():
    conn = connection()
    person_id = identity.create_person(
        conn, display_name="Alex", email="alex@example.test"
    )
    identity.enrich_confirmed_person(
        conn,
        person_id,
        display_name="Alex Example",
        email="alex@example.test",
        upn="aexample@example.test",
        aad_object_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        evidence_kind="email_exact",
        evidence_ref="task:1:person:0",
        lookup_kind="email_exact",
        confirmation_mode="exact",
    )
    row = conn.execute("SELECT * FROM person WHERE id=?", (person_id,)).fetchone()
    assert row["aad_object_id"] == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    aliases = {
        (row["alias_kind"], row["alias_value"])
        for row in conn.execute(
            "SELECT alias_kind,alias_value FROM person_alias WHERE person_id=?",
            (person_id,),
        )
    }
    assert ("aad", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa") in aliases
    assert ("upn", "aexample@example.test") in aliases


def test_legacy_primary_email_resolves_before_alias_backfill():
    conn = connection()
    cursor = conn.execute(
        "INSERT INTO person (display_name,primary_email) VALUES (?,?)",
        ("Legacy Person", "legacy@example.test"),
    )
    assert identity.resolve_person(
        conn, email="legacy@example.test", create_if_missing=False
    ) == cursor.lastrowid


def test_audited_alias_seed_never_creates_missing_person():
    conn = connection()
    assert identity.seed_audited_aliases(
        conn, [("alex.powell", "apowell@microsoft.com")]
    ) == 0
    assert conn.execute("SELECT COUNT(*) FROM person").fetchone()[0] == 0

    person_id = identity.create_person(
        conn, display_name="Alex Powell", email="apowell@microsoft.com"
    )
    assert identity.seed_audited_aliases(
        conn, [("alex.powell", "apowell@microsoft.com")], commit=True
    ) == 1
    alias = conn.execute(
        "SELECT * FROM person_alias WHERE person_id=? AND alias_value='alex.powell'",
        (person_id,),
    ).fetchone()
    assert alias["confidence"] == "inferred"
    assert alias["evidence_kind"] == "audited_alias_seed"


def test_authoritative_email_alias_is_unique_across_people():
    conn = connection()
    first = identity.create_person(
        conn, display_name="First", email="same@example.test"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        identity.create_person(
            conn, display_name="Second", email="same@example.test"
        )
    conn.rollback()
    assert identity.resolve_person(
        conn, email="same@example.test", create_if_missing=False
    ) == first
