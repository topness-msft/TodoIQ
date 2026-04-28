"""Unit tests for shadow_dedup: Stage A recall + Stage C validation/apply.

The LLM call itself (Stage B) is not tested here — see temp/dedup_test.py for
end-to-end validation against real copilot.
"""
import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from src import db as db_module
from src.services import shadow_dedup as sd


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


def _insert(conn, **kw):
    kw.setdefault("status", "suggested")
    kw.setdefault("parse_status", "parsed")
    fields = ",".join(kw.keys())
    placeholders = ",".join("?" * len(kw))
    cur = conn.execute(
        f"INSERT INTO tasks ({fields}) VALUES ({placeholders})",
        tuple(kw.values()),
    )
    conn.commit()
    return cur.lastrowid


# ── Stage A recall ────────────────────────────────────────────────────────

def test_fetch_recent_items_skips_checked(tmp_db):
    a = _insert(tmp_db, title="New A")
    b = _insert(tmp_db, title="New B")
    tmp_db.execute(
        "UPDATE tasks SET shadow_checked_at = ? WHERE id = ?",
        ("2026-04-24T00:00:00Z", a),
    )
    tmp_db.commit()
    items = sd.fetch_recent_items(tmp_db, since_minutes=1440 * 30)
    ids = {i["id"] for i in items}
    assert b in ids
    assert a not in ids


def test_fetch_candidates_matches_by_key_people_alias(tmp_db):
    """PNC scenario: new item with full email, candidate with truncated email —
    recall bridges via shared last-name token 'morenov'."""
    cand = _insert(
        tmp_db,
        title="Old Federico task",
        source_id="chat::federico.morenov@microsoft.com::something",
        key_people='[{"name":"Federico Moreno Vasquez","email":"federico.morenov@microsoft.com"}]',
        created_at="2026-04-01T00:00:00Z",
    )
    new_item = {
        "id": 999,
        "title": "New Federico task",
        "source_id": "chat::federico.morenovasquez@microsoft.com::other",
        "key_people": '[{"name":"Federico Moreno Vasquez","email":"federico.morenovasquez@microsoft.com"}]',
        "source_snippet": "",
    }
    cands = sd.fetch_candidates(tmp_db, [new_item], lookback_days=365)
    assert cand in {c["id"] for c in cands}


def test_fetch_candidates_bridges_orthogonal_aliases_via_name(tmp_db):
    """With name-token extraction, recall bridges even fully orthogonal
    email aliases (e.g. 'fem' vs 'federico.morenovasquez') via the shared
    person name in key_people ('Federico Moreno Vasquez')."""
    cand = _insert(
        tmp_db,
        title="Old Federico task",
        source_id="chat::fem@microsoft.com::something",
        key_people='[{"name":"Federico Moreno Vasquez","email":"fem@microsoft.com"}]',
    )
    new_item = {
        "id": 999,
        "title": "New Federico task",
        "source_id": "chat::federico.morenovasquez@microsoft.com::other",
        "key_people": '[{"name":"Federico Moreno Vasquez","email":"federico.morenovasquez@microsoft.com"}]',
        "source_snippet": "",
    }
    cands = sd.fetch_candidates(tmp_db, [new_item], lookback_days=365)
    assert cand in {c["id"] for c in cands}


def test_fetch_candidates_excludes_new_item_ids(tmp_db):
    a = _insert(
        tmp_db,
        title="A",
        source_id="chat::foo@microsoft.com::x",
        key_people='[{"name":"Foo","email":"foo@microsoft.com"}]',
    )
    new_items = [{
        "id": a,
        "title": "A",
        "source_id": "chat::foo@microsoft.com::x",
        "key_people": '[{"name":"Foo","email":"foo@microsoft.com"}]',
        "source_snippet": "",
    }]
    cands = sd.fetch_candidates(tmp_db, new_items, lookback_days=365)
    assert a not in {c["id"] for c in cands}


# ── JSON parsing ──────────────────────────────────────────────────────────

def test_parse_decisions_extracts_from_prose():
    raw = 'Sure, here is the result:\n{"decisions": [{"id": 1, "match_id": null, "reason": "ok"}]}\nDone!'
    parsed = sd.parse_decisions(raw)
    assert parsed == {"decisions": [{"id": 1, "match_id": None, "reason": "ok"}]}


def test_parse_decisions_returns_none_on_garbage():
    assert sd.parse_decisions("no json here") is None
    assert sd.parse_decisions("") is None
    assert sd.parse_decisions("{not valid json}") is None


# ── Stage C validation ────────────────────────────────────────────────────

def _items(ids):
    return [{"id": i, "title": f"t{i}"} for i in ids]


def test_validate_drops_unknown_new_id():
    decisions = [{"id": 999, "match_id": None, "reason": "x"}]
    out = sd.validate_decisions(decisions, _items([1, 2]), [])
    assert out == []


def test_validate_drops_invalid_match_id():
    decisions = [{"id": 2, "match_id": 9999, "reason": "x"}]
    out = sd.validate_decisions(decisions, _items([1, 2]), [{"id": 10}])
    assert out == []


def test_validate_accepts_candidate_match():
    decisions = [{"id": 2, "match_id": 10, "reason": "x"}]
    out = sd.validate_decisions(decisions, _items([1, 2]), [{"id": 10}])
    assert len(out) == 1


def test_validate_accepts_earlier_new_item_match():
    decisions = [{"id": 2, "match_id": 1, "reason": "x"}]
    out = sd.validate_decisions(decisions, _items([1, 2]), [])
    assert len(out) == 1


def test_validate_rejects_later_new_item_match():
    """Decision pointing to a LATER new item (index > own index) is invalid."""
    decisions = [{"id": 1, "match_id": 2, "reason": "x"}]
    out = sd.validate_decisions(decisions, _items([1, 2]), [])
    assert out == []


def test_validate_rejects_self_match():
    decisions = [{"id": 1, "match_id": 1, "reason": "x"}]
    out = sd.validate_decisions(decisions, _items([1, 2]), [])
    assert out == []


# ── Stage C apply ────────────────────────────────────────────────────────

def test_apply_writes_flags(tmp_db):
    a = _insert(tmp_db, title="A")
    b = _insert(tmp_db, title="B")
    decisions = [
        {"id": b, "match_id": a, "reason": "same thing"},
    ]
    flagged = sd.apply_shadow_flags(tmp_db, decisions)
    assert flagged == 1
    row = tmp_db.execute(
        "SELECT shadow_dup_of, shadow_dup_reason, shadow_checked_at FROM tasks WHERE id = ?",
        (b,),
    ).fetchone()
    assert row["shadow_dup_of"] == a
    assert row["shadow_dup_reason"] == "same thing"
    assert row["shadow_checked_at"] is not None


def test_apply_stamps_checked_at_on_non_dupes(tmp_db):
    a = _insert(tmp_db, title="A")
    decisions = [{"id": a, "match_id": None, "reason": "unique"}]
    flagged = sd.apply_shadow_flags(tmp_db, decisions)
    assert flagged == 0
    row = tmp_db.execute(
        "SELECT shadow_dup_of, shadow_checked_at FROM tasks WHERE id = ?",
        (a,),
    ).fetchone()
    assert row["shadow_dup_of"] is None
    assert row["shadow_checked_at"] is not None


def test_apply_truncates_long_reason(tmp_db):
    a = _insert(tmp_db, title="A")
    b = _insert(tmp_db, title="B")
    decisions = [{"id": b, "match_id": a, "reason": "x" * 1000}]
    sd.apply_shadow_flags(tmp_db, decisions)
    row = tmp_db.execute(
        "SELECT shadow_dup_reason FROM tasks WHERE id = ?", (b,),
    ).fetchone()
    assert len(row["shadow_dup_reason"]) == 500


# ── End-to-end with mocked LLM (three known fixtures) ─────────────────────

def _load_fixture_items(tmp_db, fixture_rows):
    """Insert a fixture set, return ordered new-item list (as recent items)."""
    inserted = []
    for r in fixture_rows:
        i = _insert(tmp_db, **r)
        inserted.append(i)
    return inserted


def test_end_to_end_pnc_like(tmp_db):
    """Simulate: 4 new PNC items created, mock LLM clusters them all → 3 flagged."""
    rows = [
        dict(title="Arrange PNC call", source_id="chat::fem@microsoft.com::pnc",
             source_type="chat",
             key_people='[{"name":"Federico Moreno Vasquez","email":"fem@microsoft.com"}]'),
        dict(title="Connect PNC Bank with Power CAT", source_id="chat::fem@microsoft.com::pnc2",
             source_type="chat",
             key_people='[{"name":"Federico Moreno Vasquez","email":"fem@microsoft.com"}]'),
        dict(title="Help line up PNC call", source_id="chat::federico.morenov@microsoft.com::pnc3",
             source_type="chat",
             key_people='[{"name":"Federico Moreno Vasquez","email":"federico.morenov@microsoft.com"}]'),
        dict(title="Program overview for PNC",
             source_id="chat::federico.morenovasquez@microsoft.com::pnc4",
             source_type="chat",
             key_people='[{"name":"Federico Moreno Vasquez","email":"federico.morenovasquez@microsoft.com"}]'),
    ]
    ids = _load_fixture_items(tmp_db, rows)

    fake_llm_output = json.dumps({
        "decisions": [
            {"id": ids[0], "match_id": None, "reason": "first PNC task"},
            {"id": ids[1], "match_id": ids[0], "reason": "same PNC thread"},
            {"id": ids[2], "match_id": ids[0], "reason": "same PNC thread"},
            {"id": ids[3], "match_id": ids[0], "reason": "same PNC thread"},
        ]
    })

    with patch.object(sd, "run_ratification", return_value=json.loads(fake_llm_output)):
        result = sd.run_shadow_dedup(since_minutes=1440 * 30)

    assert result["ok"] is True
    assert result["new_items"] == 4
    assert result["flagged"] == 3
    # All four should have shadow_checked_at set
    for i in ids:
        row = tmp_db.execute(
            "SELECT shadow_checked_at, shadow_dup_of FROM tasks WHERE id = ?", (i,),
        ).fetchone()
        assert row["shadow_checked_at"] is not None
    # Items 2-4 should point to item 1
    for i in ids[1:]:
        row = tmp_db.execute(
            "SELECT shadow_dup_of FROM tasks WHERE id = ?", (i,),
        ).fetchone()
        assert row["shadow_dup_of"] == ids[0]


def test_end_to_end_handles_no_new_items(tmp_db):
    result = sd.run_shadow_dedup(since_minutes=1)
    assert result["ok"] is True
    assert result["new_items"] == 0


def test_end_to_end_handles_llm_failure(tmp_db):
    _insert(tmp_db, title="solo")
    with patch.object(sd, "run_ratification", return_value=None):
        result = sd.run_shadow_dedup(since_minutes=1440 * 30)
    assert result["ok"] is False


# -- Chain-collapse + backfill (Change C) ---------------------------------

def test_resolve_shadow_root_no_pointer(tmp_db):
    a = _insert(tmp_db, title="A")
    assert sd.resolve_shadow_root(tmp_db, a) == a


def test_resolve_shadow_root_walks_chain(tmp_db):
    a = _insert(tmp_db, title="A")
    b = _insert(tmp_db, title="B")
    c = _insert(tmp_db, title="C")
    tmp_db.execute("UPDATE tasks SET shadow_dup_of=? WHERE id=?", (a, b))
    tmp_db.execute("UPDATE tasks SET shadow_dup_of=? WHERE id=?", (b, c))
    tmp_db.commit()
    assert sd.resolve_shadow_root(tmp_db, c) == a
    assert sd.resolve_shadow_root(tmp_db, b) == a


def test_resolve_shadow_root_handles_cycle(tmp_db):
    a = _insert(tmp_db, title="A")
    b = _insert(tmp_db, title="B")
    tmp_db.execute("UPDATE tasks SET shadow_dup_of=? WHERE id=?", (b, a))
    tmp_db.execute("UPDATE tasks SET shadow_dup_of=? WHERE id=?", (a, b))
    tmp_db.commit()
    # Should return without raising; result is one of {a, b}
    root = sd.resolve_shadow_root(tmp_db, a)
    assert root in (a, b)


def test_apply_collapses_chain_at_write(tmp_db):
    """Item C dups B which already dups A -> writing C should produce C->A."""
    a = _insert(tmp_db, title="A")
    b = _insert(tmp_db, title="B")
    c = _insert(tmp_db, title="C")
    tmp_db.execute("UPDATE tasks SET shadow_dup_of=? WHERE id=?", (a, b))
    tmp_db.commit()
    decisions = [{"id": c, "match_id": b, "reason": "same"}]
    sd.apply_shadow_flags(tmp_db, decisions)
    row = tmp_db.execute(
        "SELECT shadow_dup_of FROM tasks WHERE id=?", (c,)
    ).fetchone()
    assert row["shadow_dup_of"] == a


def test_backfill_rewrites_existing_chains(tmp_db):
    """Pre-existing A<-B chain, then later B<-C; backfill should produce A<-C."""
    a = _insert(tmp_db, title="A")
    b = _insert(tmp_db, title="B")
    c = _insert(tmp_db, title="C")
    tmp_db.execute("UPDATE tasks SET shadow_dup_of=? WHERE id=?", (a, c))  # C->A
    tmp_db.execute("UPDATE tasks SET shadow_dup_of=? WHERE id=?", (a, b))  # B->A
    tmp_db.commit()
    # Now imagine a later refresh that finds B is a dup of an even older A2.
    a2 = _insert(tmp_db, title="A2")
    tmp_db.execute("UPDATE tasks SET shadow_dup_of=? WHERE id=?", (a2, a))  # A->A2
    tmp_db.commit()
    updated = sd.backfill_shadow_roots(tmp_db)
    assert updated >= 2
    # Both B and C should now point at A2 (the new root)
    for tid in (b, c, a):
        row = tmp_db.execute(
            "SELECT shadow_dup_of FROM tasks WHERE id=?", (tid,)
        ).fetchone()
        assert row["shadow_dup_of"] == a2


def test_backfill_idempotent(tmp_db):
    a = _insert(tmp_db, title="A")
    b = _insert(tmp_db, title="B")
    c = _insert(tmp_db, title="C")
    tmp_db.execute("UPDATE tasks SET shadow_dup_of=? WHERE id=?", (a, b))
    tmp_db.execute("UPDATE tasks SET shadow_dup_of=? WHERE id=?", (b, c))
    tmp_db.commit()
    sd.backfill_shadow_roots(tmp_db)
    second = sd.backfill_shadow_roots(tmp_db)
    assert second == 0


def test_backfill_breaks_self_loop(tmp_db):
    a = _insert(tmp_db, title="A")
    tmp_db.execute("UPDATE tasks SET shadow_dup_of=? WHERE id=?", (a, a))
    tmp_db.commit()
    sd.backfill_shadow_roots(tmp_db)
    row = tmp_db.execute("SELECT shadow_dup_of FROM tasks WHERE id=?", (a,)).fetchone()
    assert row["shadow_dup_of"] is None


def test_fetch_candidates_excludes_already_dup(tmp_db):
    """A candidate already flagged as a dup should NOT be offered as a candidate."""
    root = _insert(
        tmp_db, title="Root",
        source_id="chat::foo@microsoft.com::r",
        key_people='[{"name":"Foo","email":"foo@microsoft.com"}]',
    )
    dup = _insert(
        tmp_db, title="Dup",
        source_id="chat::foo@microsoft.com::d",
        key_people='[{"name":"Foo","email":"foo@microsoft.com"}]',
    )
    tmp_db.execute("UPDATE tasks SET shadow_dup_of=? WHERE id=?", (root, dup))
    tmp_db.commit()
    new_item = {
        "id": 9999,
        "title": "New Foo",
        "source_id": "chat::foo@microsoft.com::n",
        "key_people": '[{"name":"Foo","email":"foo@microsoft.com"}]',
        "source_snippet": "",
    }
    cands = sd.fetch_candidates(tmp_db, [new_item], lookback_days=365)
    ids = {c["id"] for c in cands}
    assert root in ids
    assert dup not in ids
