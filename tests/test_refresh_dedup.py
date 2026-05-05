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


# ── Fix A: title dedup must skip dismissed (no ghost re-creation) ─────────

def test_title_dedup_skips_dismissed_match(tmp_db):
    """When a paraphrased re-surface of a dismissed conversation comes in,
    Pass 3 (title similarity) should detect the dismissed match and skip.
    Without this, every refresh would create a new ghost suggestion.
    """
    # First task — creates and gets dismissed
    r1 = rd.create_or_refresh_suggestion(**_signal(
        title="Send Power Up OKRs and numbers to Saurabh",
        source_id="chat::saurabh.pant@microsoft.com::power up okrs",
        key_people='[{"name":"Saurabh Pant","email":"saurabh.pant@microsoft.com"}]',
    ))
    conn = db_module.get_connection()
    conn.execute("UPDATE tasks SET status='dismissed' WHERE id=?", (r1.task["id"],))
    conn.commit()
    n_before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    conn.close()

    # Second task — same person, paraphrased title, different source_id keywords
    # so Pass 1 (exact source_id) and Pass 2 (fuzzy source_id keywords) both miss.
    # Pass 3 (title similarity) is the only line of defense.
    r2 = rd.create_or_refresh_suggestion(**_signal(
        title="Send latest Power Up OKRs and progress data to Saurabh Pant",
        source_id="chat::saurabh.pant@microsoft.com::m365 conference status",
        key_people='[{"name":"Saurabh Pant","email":"saurabh.pant@microsoft.com"}]',
    ))
    assert r2.outcome == "skipped_dismissed"
    assert r2.matched_task_id == r1.task["id"]
    conn = db_module.get_connection()
    n_after = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    conn.close()
    assert n_after == n_before  # no ghost duplicate created


def test_title_dedup_completed_match_creates_new(tmp_db):
    """Completed tasks close the loop — re-occurrence is a legitimate new
    signal, so a paraphrased title match against a completed task should
    still create a fresh suggestion (not skip)."""
    r1 = rd.create_or_refresh_suggestion(**_signal(
        title="Send Power Up OKRs and numbers to Saurabh",
        source_id="chat::saurabh.pant@microsoft.com::power up okrs",
        key_people='[{"name":"Saurabh Pant","email":"saurabh.pant@microsoft.com"}]',
    ))
    conn = db_module.get_connection()
    conn.execute("UPDATE tasks SET status='completed' WHERE id=?", (r1.task["id"],))
    conn.commit()
    conn.close()

    r2 = rd.create_or_refresh_suggestion(**_signal(
        title="Send latest Power Up OKRs and progress data to Saurabh Pant",
        source_id="chat::saurabh.pant@microsoft.com::m365 conference status",
        key_people='[{"name":"Saurabh Pant","email":"saurabh.pant@microsoft.com"}]',
    ))
    assert r2.outcome == "created"
    assert r2.task["id"] != r1.task["id"]


def test_title_dedup_prefers_unresolved_over_dismissed(tmp_db):
    """When an unresolved task and a dismissed task both match the same new
    signal by title, the unresolved one should win (so we augment the live
    task rather than skip-as-dismissed)."""
    # Create an old dismissed task
    r_dismissed = rd.create_or_refresh_suggestion(**_signal(
        title="Send Power Up OKRs and numbers to Saurabh",
        source_id="chat::saurabh.pant@microsoft.com::power up old",
        key_people='[{"name":"Saurabh Pant","email":"saurabh.pant@microsoft.com"}]',
    ))
    conn = db_module.get_connection()
    conn.execute("UPDATE tasks SET status='dismissed' WHERE id=?", (r_dismissed.task["id"],))
    conn.commit()
    conn.close()

    # Create a live (suggested) task that also matches
    r_live = rd.create_or_refresh_suggestion(**_signal(
        title="Send Power Up OKRs to Saurabh Pant",
        source_id="chat::saurabh.pant@microsoft.com::power up live",
        key_people='[{"name":"Saurabh Pant","email":"saurabh.pant@microsoft.com"}]',
    ))
    # Either created (if no Pass 3 hit) or skipped_dismissed.
    # The point of the next step is that after this live task exists,
    # a third paraphrased signal should augment the LIVE one, not skip.
    if r_live.outcome == "skipped_dismissed":
        # Pass 3 caught it via the dismissed match — manually create a live task
        # to simulate the case where a live task already exists.
        conn = db_module.get_connection()
        conn.execute(
            """INSERT INTO tasks (title, status, parse_status, priority, source_type,
               source_id, key_people, created_at, updated_at)
               VALUES (?, 'suggested', 'parsed', 3, 'chat',
                       'chat::saurabh.pant@microsoft.com::power up live',
                       ?, '2026-04-29T00:00:00Z', '2026-04-29T00:00:00Z')""",
            ("Send Power Up OKRs to Saurabh Pant",
             '[{"name":"Saurabh Pant","email":"saurabh.pant@microsoft.com"}]'),
        )
        conn.commit()
        live_id = conn.execute(
            "SELECT id FROM tasks WHERE source_id='chat::saurabh.pant@microsoft.com::power up live'"
        ).fetchone()[0]
        conn.close()
    else:
        live_id = r_live.task["id"]

    # New signal that paraphrase-matches both — should augment the live one
    r3 = rd.create_or_refresh_suggestion(**_signal(
        title="Send latest Power Up OKRs and progress data to Saurabh Pant",
        source_id="chat::saurabh.pant@microsoft.com::another keyword tail",
        key_people='[{"name":"Saurabh Pant","email":"saurabh.pant@microsoft.com"}]',
    ))
    assert r3.outcome == "augmented"
    assert r3.matched_task_id == live_id


# ── Fix B: synonym expansion ─────────────────────────────────────────────

def test_synonym_a365_matches_agent_365(tmp_db):
    """A365 ↔ Agent 365 should be recognized as the same topic.

    This validates the synonym/multi-word collapse mechanism. The clean
    pair shares enough tokens that the synonym is the only thing keeping
    Jaccard above threshold."""
    r1 = rd.create_or_refresh_suggestion(**_signal(
        title="Provide feedback on Agent 365 training approach",
        source_id="chat::bill.spencer@microsoft.com::agent 365 training",
        key_people='[{"name":"Bill Spencer","email":"bill.spencer@microsoft.com"}]',
    ))
    r2 = rd.create_or_refresh_suggestion(**_signal(
        title="Reply on A365 training approach",
        source_id="chat::bill.spencer@microsoft.com::a365 training reply",
        key_people='[{"name":"Bill Spencer","email":"bill.spencer@microsoft.com"}]',
    ))
    # Without the synonym (a365 vs agent 365) these wouldn't share enough
    # tokens to clear threshold. With it, they collapse to the same topic.
    assert r2.outcome == "augmented"
    assert r2.matched_task_id == r1.task["id"]


def test_synonym_attendance_matches_participation(tmp_db):
    """attendance / participation / rejoin should collapse to the same token."""
    r1 = rd.create_or_refresh_suggestion(**_signal(
        title="Follow up with Vasavi on webinar meeting participation",
        source_id="chat::vasavi.bhaviri@microsoft.com::webinar participation",
        key_people='[{"name":"Vasavi","email":"vasavi.bhaviri@microsoft.com"}]',
    ))
    r2 = rd.create_or_refresh_suggestion(**_signal(
        title="Follow up with Vasavi on webinar meeting attendance",
        source_id="chat::vasavi.bhaviri@microsoft.com::webinar attendance",
        key_people='[{"name":"Vasavi","email":"vasavi.bhaviri@microsoft.com"}]',
    ))
    assert r2.outcome == "augmented"
    assert r2.matched_task_id == r1.task["id"]


# ── URL-timestamp reconciliation (earlier fix in this session) ───────────

def test_teams_url_timestamp_overrides_drifted_source_date(tmp_db):
    """When WorkIQ reports a recent date but the Teams URL embeds an older
    timestamp, the URL date wins (drift > 14 days)."""
    # URL has ms timestamp encoding 2026-02-03 16:12 UTC
    url = ("https://teams.microsoft.com/l/message/19:test@thread/1770135153375"
           "?context=%7B%22contextType%22%3A%22chat%22%7D")
    r = rd.create_or_refresh_suggestion(**_signal(
        source_date="2026-05-04",  # WorkIQ's drifted (wrong) claim
        source_url=url,
    ))
    assert r.outcome == "created"
    # Persisted source_date should be the URL date, not WorkIQ's drift
    assert r.task["source_date"] == "2026-02-03"


def test_teams_url_within_threshold_keeps_workiq_date(tmp_db):
    """Drift below 14 days — trust WorkIQ. URL doesn't override."""
    # URL ms timestamp 2026-04-25 — drift = 3 days from "2026-04-28" reported
    import time
    from datetime import datetime, timezone
    ts = int(datetime(2026, 4, 25, 10, 0, tzinfo=timezone.utc).timestamp() * 1000)
    url = f"https://teams.microsoft.com/l/message/19:test@thread/{ts}?x=1"
    r = rd.create_or_refresh_suggestion(**_signal(
        source_date="2026-04-28",
        source_url=url,
    ))
    assert r.outcome == "created"
    assert r.task["source_date"] == "2026-04-28"  # WorkIQ's claim kept


def test_teams_url_no_timestamp_keeps_workiq_date(tmp_db):
    """URL without parseable timestamp — fall back to WorkIQ's claim."""
    r = rd.create_or_refresh_suggestion(**_signal(
        source_date="2026-04-28",
        source_url="https://teams.microsoft.com/l/channel/19:abc/General",
    ))
    assert r.outcome == "created"
    assert r.task["source_date"] == "2026-04-28"


def test_non_teams_url_keeps_workiq_date(tmp_db):
    """Reconciliation only fires for teams.microsoft.com URLs."""
    r = rd.create_or_refresh_suggestion(**_signal(
        source_date="2026-05-04",
        source_url="https://outlook.office.com/mail/inbox/id/AAAA1770135153375",
    ))
    assert r.outcome == "created"
    assert r.task["source_date"] == "2026-05-04"  # not parsed
