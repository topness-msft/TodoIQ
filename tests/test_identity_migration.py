import sqlite3

import pytest

from src.db import init_db


def _legacy_connection(*, snoozed_supported: bool, error_supported: bool):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    statuses = (
        "'suggested','active','in_progress','waiting','snoozed',"
        "'completed','dismissed','deleted'"
        if snoozed_supported
        else "'suggested','active','in_progress','waiting',"
        "'completed','dismissed','deleted'"
    )
    parse_statuses = (
        "'unparsed','queued','parsing','parsed','error'"
        if error_supported
        else "'unparsed','queued','parsing','parsed'"
    )
    conn.executescript(
        f"""
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ({statuses})),
            parse_status TEXT NOT NULL DEFAULT 'parsed'
                CHECK(parse_status IN ({parse_statuses})),
            priority INTEGER NOT NULL DEFAULT 3,
            source_date TEXT,
            legacy_extra TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE task_context (
            id INTEGER PRIMARY KEY,
            task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
            context_type TEXT,
            content TEXT
        );
        CREATE TABLE refresh_schedule (
            task_id INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
            interval_minutes INTEGER,
            next_refresh_at TEXT
        );
        CREATE TABLE task_actions (
            id INTEGER PRIMARY KEY,
            task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
            action_type TEXT,
            state TEXT CHECK(state IN (
                'previewing','ready','failed','executing','executed',
                'execute_unconfirmed'
            )),
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE person (
            id INTEGER PRIMARY KEY,
            display_name TEXT NOT NULL,
            primary_email TEXT,
            canonical_person_id INTEGER
        );
        CREATE TABLE task_person (
            task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
            person_id INTEGER REFERENCES person(id),
            role TEXT,
            PRIMARY KEY(task_id, person_id, role)
        );
        INSERT INTO tasks (
            id, title, status, parse_status, priority, source_date, legacy_extra,
            created_at, updated_at
        ) VALUES (
            7, 'Preserve me', 'active', 'parsed', 3, '2026-08-01',
            'unknown-value', '2026-08-01', '2026-08-01'
        );
        INSERT INTO task_context VALUES (1, 7, 'suggestion', 'context');
        INSERT INTO refresh_schedule VALUES (7, 30, NULL);
        INSERT INTO task_actions VALUES (
            1, 7, 'general', 'ready', '2026-08-01', '2026-08-01'
        );
        INSERT INTO person VALUES (1, 'Known Person', NULL, NULL);
        INSERT INTO task_person VALUES (7, 1, 'key_people');
        """
    )
    return conn


@pytest.mark.parametrize(
    ("snoozed_supported", "error_supported"),
    [(False, True), (True, False)],
)
def test_legacy_task_rebuild_preserves_extra_columns_and_child_rows(
    snoozed_supported, error_supported
):
    conn = _legacy_connection(
        snoozed_supported=snoozed_supported,
        error_supported=error_supported,
    )

    init_db(conn)

    task = conn.execute("SELECT * FROM tasks WHERE id=7").fetchone()
    assert task["source_date"] == "2026-08-01"
    assert task["legacy_extra"] == "unknown-value"
    assert conn.execute("SELECT COUNT(*) FROM task_context").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM refresh_schedule").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM task_actions").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM task_person").fetchone()[0] == 1
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def _master_migrated_connection():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    init_db(conn)
    conn.execute(
        "INSERT INTO person (display_name, primary_email) VALUES (?, ?)",
        ("Existing Person", "existing@example.test"),
    )
    person_id = conn.execute("SELECT id FROM person").fetchone()[0]
    conn.execute(
        "INSERT INTO person_alias "
        "(person_id, alias_kind, alias_value, confidence) VALUES (?,?,?,?)",
        (person_id, "email", "existing@example.test", "email"),
    )
    conn.execute(
        "INSERT INTO tasks (id, title, key_people) VALUES (1, ?, ?)",
        (
            "Existing task",
            '[{"name":"Existing Person","email":"existing@example.test"}]',
        ),
    )
    conn.execute(
        "INSERT INTO task_person (task_id, person_id, role) VALUES (?,?,?)",
        (1, person_id, "key_people"),
    )
    conn.execute("DROP TABLE person_backfill_state")
    conn.commit()
    return conn


def test_init_db_on_master_migrated_db_preserves_identity_rows_and_adds_marker():
    conn = _master_migrated_connection()

    init_db(conn)

    assert conn.execute("SELECT COUNT(*) FROM person").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM person_alias").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM task_person").fetchone()[0] == 1
    marker = conn.execute("SELECT * FROM person_backfill_state").fetchone()
    assert marker["status"] == "legacy_untracked"
    assert marker["last_task_id"] == 0


def test_repeated_init_on_master_migrated_db_is_noop():
    conn = _master_migrated_connection()
    init_db(conn)
    before = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "person",
            "person_alias",
            "person_merge_history",
            "task_person",
            "person_backfill_state",
        )
    }

    init_db(conn)

    after = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in before
    }
    assert after == before
