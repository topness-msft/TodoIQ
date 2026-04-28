"""Tests for create_or_refresh_suggestion (Change A)."""
import sqlite3
import tempfile
from pathlib import Path

import pytest

from src import db as db_module
from src.services import refresh_dedup as rd
from src.services import person_identity as pi


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
        yield path
        conn.close()


def _signal(**overrides):
    base = dict(
        title="Test signal",
        description="",
        source_type="chat",
        source_id="chat::alice@x.com::msg-123",
        source_snippet="hello",
        source_date="2026-04-28",
        key_people='[{"name":"Alice","email":"alice@x.com"}]',
        priority=3,
        action_type="general",
        coaching_text=None,
    )
    base.update(overrides)
    return base


def test_create_new_when_no_match(tmp_db):
    r = rd.create_or_refresh_suggestion(**_signal())
    assert r.outcome == "created"
    assert r.task["status"] == "suggested"
    assert r.task["title"] == "Test signal"
    # task_person should be derived
    conn = db_module.get_connection()
    rows = conn.execute(
        "SELECT * FROM task_person WHERE task_id=?", (r.task["id"],)
    ).fetchall()
    conn.close()
    assert len(rows) >= 1


def test_exact_source_id_augments_active(tmp_db):
    r1 = rd.create_or_refresh_suggestion(**_signal(priority=3, source_snippet="old"))
    # promote to active to test live-augment branch
    conn = db_module.get_connection()
    conn.execute("UPDATE tasks SET status='active' WHERE id=?", (r1.task["id"],))
    conn.commit()
    conn.close()
    r2 = rd.create_or_refresh_suggestion(**_signal(
        priority=2, source_snippet="newer context", source_date="2026-04-29"))
    assert r2.outcome == "augmented"
    assert r2.matched_task_id == r1.task["id"]
    assert r2.task["priority"] == 2  # MIN
    assert r2.task["source_date"] == "2026-04-29"  # MAX
    assert r2.task["source_snippet"] == "newer context"


def test_dismissed_match_is_skipped(tmp_db):
    r1 = rd.create_or_refresh_suggestion(**_signal())
    conn = db_module.get_connection()
    conn.execute("UPDATE tasks SET status='dismissed' WHERE id=?", (r1.task["id"],))
    conn.commit()
    n_before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    conn.close()
    r2 = rd.create_or_refresh_suggestion(**_signal())
    assert r2.outcome == "skipped_dismissed"
    assert r2.matched_task_id == r1.task["id"]
    conn = db_module.get_connection()
    n_after = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    conn.close()
    assert n_after == n_before


def test_completed_match_creates_new(tmp_db):
    """Per resolved policy: completed tasks don't dedup; new signal becomes
    a fresh suggestion (re-occurrence is a legitimate new signal)."""
    r1 = rd.create_or_refresh_suggestion(**_signal())
    conn = db_module.get_connection()
    conn.execute("UPDATE tasks SET status='completed' WHERE id=?", (r1.task["id"],))
    conn.commit()
    conn.close()
    r2 = rd.create_or_refresh_suggestion(**_signal())
    assert r2.outcome == "created"
    assert r2.task["id"] != r1.task["id"]


def test_max_date_never_clobbers_with_older(tmp_db):
    """Augmentation with an OLDER source_date must not overwrite the newer."""
    r1 = rd.create_or_refresh_suggestion(**_signal(source_date="2026-04-28"))
    conn = db_module.get_connection()
    conn.execute("UPDATE tasks SET status='active' WHERE id=?", (r1.task["id"],))
    conn.commit()
    conn.close()
    r2 = rd.create_or_refresh_suggestion(**_signal(source_date="2026-04-01"))
    assert r2.outcome == "augmented"
    assert r2.task["source_date"] == "2026-04-28"  # MAX preserved


def test_priority_floors_to_min(tmp_db):
    r1 = rd.create_or_refresh_suggestion(**_signal(priority=4))
    r2 = rd.create_or_refresh_suggestion(**_signal(priority=2))
    assert r2.outcome == "augmented"
    assert r2.task["priority"] == 2

    r3 = rd.create_or_refresh_suggestion(**_signal(priority=5))
    assert r3.outcome == "augmented"
    assert r3.task["priority"] == 2  # never raised by lower-urgency signal


def test_augmentation_appends_context_row(tmp_db):
    r1 = rd.create_or_refresh_suggestion(**_signal(source_snippet="first"))
    rd.create_or_refresh_suggestion(**_signal(source_snippet="second"))
    conn = db_module.get_connection()
    rows = conn.execute(
        "SELECT * FROM task_context WHERE task_id=? AND context_type='dedup'",
        (r1.task["id"],)
    ).fetchall()
    conn.close()
    assert len(rows) == 1


def test_create_derives_task_person(tmp_db):
    r = rd.create_or_refresh_suggestion(**_signal(
        source_id="chat::bob@x.com::m",
        key_people='[{"name":"Bob","email":"bob@x.com"}]',
    ))
    conn = db_module.get_connection()
    pids = pi.get_task_person_ids(conn, r.task["id"])
    conn.close()
    assert len(pids) >= 1


def test_concurrent_inserts_dedup(tmp_db):
    """Two parallel refreshes with same source_id should produce 1 task,
    not 2. BEGIN IMMEDIATE serializes the dedup-check + insert."""
    import threading
    results = []

    def call():
        results.append(rd.create_or_refresh_suggestion(**_signal()))

    t1 = threading.Thread(target=call)
    t2 = threading.Thread(target=call)
    t1.start(); t2.start()
    t1.join(); t2.join()

    outcomes = sorted(r.outcome for r in results)
    # Exactly one created, one augmented (or skipped if both raced)
    assert "created" in outcomes
    conn = db_module.get_connection()
    n = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE source_id=?",
        (_signal()["source_id"],)
    ).fetchone()[0]
    conn.close()
    assert n == 1
