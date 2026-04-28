"""Tests for the canonical person identity layer (Change B)."""
import sqlite3
import tempfile
from pathlib import Path

import pytest

from src import db as db_module
from src.services import person_identity as pi
from src.services import person_bootstrap as pb


@pytest.fixture
def tmp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td) / "data"
        data_dir.mkdir()
        path = data_dir / "test.db"
        monkeypatch.setattr(db_module, "DB_DIR", data_dir)
        monkeypatch.setattr(db_module, "DB_PATH", path)
        conn = db_module.get_connection()
        db_module.init_db(conn)
        yield conn
        conn.close()


def _insert_task(conn, **kw):
    kw.setdefault("status", "suggested")
    kw.setdefault("parse_status", "parsed")
    kw.setdefault("title", "t")
    fields = ",".join(kw.keys())
    placeholders = ",".join("?" * len(kw))
    cur = conn.execute(
        f"INSERT INTO tasks ({fields}) VALUES ({placeholders})",
        tuple(kw.values()),
    )
    conn.commit()
    return cur.lastrowid


# ── Resolution precedence ────────────────────────────────────────────────

def test_resolve_creates_new_when_unknown(tmp_db):
    pid = pi.resolve_person(tmp_db, display_name="Steve Smith", email="steve@x.com")
    assert pid is not None
    row = tmp_db.execute("SELECT * FROM person WHERE id=?", (pid,)).fetchone()
    assert row["primary_email"] == "steve@x.com"


def test_resolve_finds_by_email(tmp_db):
    pid1 = pi.resolve_person(tmp_db, display_name="Steve Smith", email="steve@x.com")
    pid2 = pi.resolve_person(tmp_db, display_name="S Smith", email="steve@x.com")
    assert pid1 == pid2


def test_resolve_email_case_insensitive(tmp_db):
    pid1 = pi.resolve_person(tmp_db, email="Steve@X.com")
    pid2 = pi.resolve_person(tmp_db, email="steve@x.com")
    assert pid1 == pid2


def test_resolve_aad_takes_precedence(tmp_db):
    a = pi.resolve_person(tmp_db, display_name="Alice", email="alice.old@x.com",
                          aad_object_id="aad-123")
    # New email, same AAD → resolves to same person
    b = pi.resolve_person(tmp_db, display_name="Alice New", email="alice.new@x.com",
                          aad_object_id="aad-123")
    assert a == b


def test_two_steve_smiths_stay_distinct(tmp_db):
    """Critical false-positive guard: same display name with different emails
    must produce two distinct persons. Name aliases are recall-only."""
    a = pi.resolve_person(tmp_db, display_name="Steve Smith", email="steve.smith@x.com")
    b = pi.resolve_person(tmp_db, display_name="Steve Smith", email="ssmith@y.com")
    assert a != b


def test_name_only_does_not_resolve_to_existing(tmp_db):
    """Name-only lookup with no email must NOT resolve to an existing person
    even if a person with that name exists. Creates a new person instead."""
    a = pi.resolve_person(tmp_db, display_name="Steve Smith", email="steve@x.com")
    b = pi.resolve_person(tmp_db, display_name="Steve Smith")  # no email
    assert a != b


def test_user_confidence_name_alias_does_resolve(tmp_db):
    """Explicit user-merge name aliases (confidence='user') ARE used for
    resolution. This is the manual-merge path."""
    a = pi.resolve_person(tmp_db, display_name="Mariam Kariakos", email="mariam.kariakos@x.com")
    pi.add_alias(tmp_db, a, "name", "Mariam Sawers", "user")
    b = pi.resolve_person(tmp_db, display_name="Mariam Sawers")
    assert a == b


def test_resolve_no_create_returns_none(tmp_db):
    assert pi.resolve_person(tmp_db, email="nobody@x.com", create_if_missing=False) is None


# ── Merges ───────────────────────────────────────────────────────────────

def test_merge_is_non_destructive(tmp_db):
    a = pi.resolve_person(tmp_db, email="mariam.kariakos@x.com")
    b = pi.resolve_person(tmp_db, email="mariamsawers@x.com")
    pi.merge_persons(tmp_db, losing_id=b, winning_id=a, reason="marriage rename")
    # Both rows still exist
    assert tmp_db.execute("SELECT id FROM person WHERE id=?", (b,)).fetchone() is not None
    # b now points at a
    assert pi.canonical_root(tmp_db, b) == a
    assert pi.canonical_root(tmp_db, a) == a
    # Resolving by either email returns canonical root a
    assert pi.resolve_person(tmp_db, email="mariam.kariakos@x.com") == a
    assert pi.resolve_person(tmp_db, email="mariamsawers@x.com") == a
    # History recorded
    h = tmp_db.execute("SELECT * FROM person_merge_history").fetchone()
    assert h["losing_id"] == b and h["winning_id"] == a


def test_merge_idempotent(tmp_db):
    a = pi.resolve_person(tmp_db, email="x@x.com")
    b = pi.resolve_person(tmp_db, email="y@x.com")
    pi.merge_persons(tmp_db, losing_id=b, winning_id=a)
    pi.merge_persons(tmp_db, losing_id=b, winning_id=a)  # second merge is noop
    rows = tmp_db.execute("SELECT COUNT(*) FROM person_merge_history").fetchone()[0]
    assert rows == 1


def test_unmerge(tmp_db):
    a = pi.resolve_person(tmp_db, email="x@x.com")
    b = pi.resolve_person(tmp_db, email="y@x.com")
    pi.merge_persons(tmp_db, losing_id=b, winning_id=a)
    pi.unmerge_persons(tmp_db, b)
    assert pi.canonical_root(tmp_db, b) == b
    h = tmp_db.execute("SELECT undone_at FROM person_merge_history").fetchone()
    assert h["undone_at"] is not None


def test_canonical_root_handles_cycle(tmp_db):
    a = pi.resolve_person(tmp_db, email="a@x.com")
    b = pi.resolve_person(tmp_db, email="b@x.com")
    tmp_db.execute("UPDATE person SET canonical_person_id=? WHERE id=?", (b, a))
    tmp_db.execute("UPDATE person SET canonical_person_id=? WHERE id=?", (a, b))
    tmp_db.commit()
    # Should return without raising
    root = pi.canonical_root(tmp_db, a)
    assert root in (a, b)


# ── Task ↔ person derivation ─────────────────────────────────────────────

def test_derive_task_persons_from_key_people(tmp_db):
    tid = _insert_task(
        tmp_db,
        key_people='[{"name":"Alice","email":"alice@x.com"},{"name":"Bob","email":"bob@x.com"}]',
    )
    pids = pi.derive_task_persons(tmp_db, tid, key_people_json=
        '[{"name":"Alice","email":"alice@x.com"},{"name":"Bob","email":"bob@x.com"}]',
        source_id=None)
    assert len(pids) == 2
    rows = tmp_db.execute(
        "SELECT person_id, role FROM task_person WHERE task_id=?", (tid,)
    ).fetchall()
    assert len(rows) == 2
    assert {r["role"] for r in rows} == {"key_people"}


def test_derive_task_persons_from_source_id_sender(tmp_db):
    tid = _insert_task(tmp_db, source_id="chat::alice@x.com::msgid")
    pi.derive_task_persons(tmp_db, tid,
                           key_people_json=None,
                           source_id="chat::alice@x.com::msgid")
    rows = tmp_db.execute(
        "SELECT role FROM task_person WHERE task_id=?", (tid,)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["role"] == "sender"


def test_derive_task_persons_idempotent(tmp_db):
    """Re-running derive on the same task should not duplicate rows."""
    tid = _insert_task(
        tmp_db,
        source_id="chat::alice@x.com::m",
        key_people='[{"name":"Alice","email":"alice@x.com"}]',
    )
    pi.derive_task_persons(tmp_db, tid,
                           key_people_json='[{"name":"Alice","email":"alice@x.com"}]',
                           source_id="chat::alice@x.com::m")
    n1 = tmp_db.execute(
        "SELECT COUNT(*) FROM task_person WHERE task_id=?", (tid,)
    ).fetchone()[0]
    pi.derive_task_persons(tmp_db, tid,
                           key_people_json='[{"name":"Alice","email":"alice@x.com"}]',
                           source_id="chat::alice@x.com::m")
    n2 = tmp_db.execute(
        "SELECT COUNT(*) FROM task_person WHERE task_id=?", (tid,)
    ).fetchone()[0]
    assert n1 == n2


def test_derive_task_persons_replaces_old(tmp_db):
    """Updating a task's key_people should remove stale task_person rows."""
    tid = _insert_task(tmp_db,
                      key_people='[{"name":"Alice","email":"alice@x.com"}]')
    pi.derive_task_persons(tmp_db, tid,
                           key_people_json='[{"name":"Alice","email":"alice@x.com"}]',
                           source_id=None)
    pi.derive_task_persons(tmp_db, tid,
                           key_people_json='[{"name":"Bob","email":"bob@x.com"}]',
                           source_id=None)
    rows = tmp_db.execute(
        """SELECT p.primary_email FROM task_person tp
           JOIN person p ON p.id = tp.person_id
           WHERE tp.task_id = ?""", (tid,)
    ).fetchall()
    emails = {r["primary_email"] for r in rows}
    assert emails == {"bob@x.com"}


# ── Candidate finding ────────────────────────────────────────────────────

def test_find_tasks_sharing_persons_excludes_completed(tmp_db):
    pid = pi.resolve_person(tmp_db, email="alice@x.com")
    t_active = _insert_task(tmp_db, status="active",
                            key_people='[{"name":"Alice","email":"alice@x.com"}]')
    t_completed = _insert_task(tmp_db, status="completed",
                               key_people='[{"name":"Alice","email":"alice@x.com"}]')
    t_dismissed = _insert_task(tmp_db, status="dismissed",
                               key_people='[{"name":"Alice","email":"alice@x.com"}]')
    for tid in (t_active, t_completed, t_dismissed):
        pi.derive_task_persons(tmp_db, tid,
                               key_people_json='[{"name":"Alice","email":"alice@x.com"}]',
                               source_id=None)
    out = pi.find_tasks_sharing_persons(tmp_db, [pid])
    assert t_active in out
    assert t_completed not in out
    assert t_dismissed not in out


def test_find_tasks_sharing_persons_excludes_shadow_dups(tmp_db):
    pid = pi.resolve_person(tmp_db, email="alice@x.com")
    a = _insert_task(tmp_db, status="active",
                    key_people='[{"name":"Alice","email":"alice@x.com"}]')
    b = _insert_task(tmp_db, status="active",
                    key_people='[{"name":"Alice","email":"alice@x.com"}]')
    for tid in (a, b):
        pi.derive_task_persons(tmp_db, tid,
                               key_people_json='[{"name":"Alice","email":"alice@x.com"}]',
                               source_id=None)
    tmp_db.execute("UPDATE tasks SET shadow_dup_of=? WHERE id=?", (a, b))
    tmp_db.commit()
    out = pi.find_tasks_sharing_persons(tmp_db, [pid])
    assert a in out
    assert b not in out


def test_find_tasks_uses_canonical_root(tmp_db):
    """If person A is merged into B, tasks tagged with either resolve via B."""
    a = pi.resolve_person(tmp_db, email="mariam.kariakos@x.com")
    b = pi.resolve_person(tmp_db, email="mariamsawers@x.com")
    t1 = _insert_task(tmp_db, status="active",
                      key_people='[{"name":"M","email":"mariam.kariakos@x.com"}]')
    t2 = _insert_task(tmp_db, status="active",
                      key_people='[{"name":"M","email":"mariamsawers@x.com"}]')
    pi.derive_task_persons(tmp_db, t1,
                           key_people_json='[{"name":"M","email":"mariam.kariakos@x.com"}]',
                           source_id=None)
    pi.derive_task_persons(tmp_db, t2,
                           key_people_json='[{"name":"M","email":"mariamsawers@x.com"}]',
                           source_id=None)
    pi.merge_persons(tmp_db, losing_id=b, winning_id=a)
    out = pi.find_tasks_sharing_persons(tmp_db, [a])
    assert t1 in out and t2 in out


# ── Bootstrap ────────────────────────────────────────────────────────────

def test_bootstrap_idempotent(tmp_db):
    _insert_task(tmp_db,
                 source_id="chat::alice@x.com::m",
                 key_people='[{"name":"Alice","email":"alice@x.com"}]')
    _insert_task(tmp_db,
                 source_id="chat::bob@x.com::m",
                 key_people='[{"name":"Bob","email":"bob@x.com"}]')
    r1 = pb.bootstrap(conn=tmp_db, batch_size=10)
    r2 = pb.bootstrap(conn=tmp_db, batch_size=10)
    assert r1["tasks_processed"] == 2
    assert r2["tasks_processed"] == 2
    assert r2["persons_created"] == 0
    # No duplicate task_person rows
    counts = tmp_db.execute(
        "SELECT task_id, COUNT(*) c FROM task_person GROUP BY task_id"
    ).fetchall()
    for r in counts:
        # Each task has at most 2 entries (sender + key_people for Alice/Bob)
        assert r["c"] <= 2
