"""Synthetic contracts, not historical or recorded M365 parity goldens."""

import importlib
import json
from pathlib import Path

import pytest


def candidate(**changes):
    return {
        "source_kind": "chat", "timestamp": "2026-09-19T12:00:00Z",
        "source_ref": None, "title": "Review release checklist",
        "description": "Taylor requests review of the release checklist.",
        "root_topic": "Release checklist", "evidence_snippet": "Review the checklist by Friday.",
        "people_hints": [{"name": "Taylor Example", "email": "taylor@example.test",
                          "upn": None, "aad_object_id": None}],
        "primary_person_hint_index": 0, "relevance_tier": "Direct",
        "base_priority": 3, "action_type": "review-document", **changes,
    }


def envelope(batch="direct", candidates=None):
    values = [] if candidates is None else candidates
    return {"version": 1, "batch": batch, "outcome": "candidates" if values else "none",
            "num_results": len(values), "candidates": values}


@pytest.fixture
def refresh():
    return importlib.import_module("src.services.refresh")


@pytest.mark.parametrize("batch", ["direct", "awaiting", "flagged"])
@pytest.mark.parametrize("fault", ["missing", "fenced", "extra", "count", "empty_candidates"])
def test_closed_batch_rejects_invalid_results(refresh, batch, fault):
    value = envelope(batch)
    if fault == "extra":
        value["conversation_id"] = "foreign"
    elif fault == "count":
        value["num_results"] = False
    elif fault == "empty_candidates":
        value["outcome"] = "candidates"
    text = json.dumps(value)
    if fault == "missing":
        text = "{}"
    elif fault == "fenced":
        text = "```json\n" + text + "\n```"
    with pytest.raises(ValueError):
        refresh.validate_batch(text, batch)


@pytest.mark.parametrize("batch", ["direct", "awaiting", "flagged"])
def test_explicit_empty_and_source_policy(refresh, batch):
    assert refresh.validate_batch(json.dumps(envelope(batch)), batch) == ()
    prompt = refresh.scan_prompt(batch, 4)
    assert "untrusted" in prompt.lower() and "JSON" in prompt
    if batch == "direct":
        assert "4 days" in prompt and "5 or fewer" in prompt and "@mention" in prompt
        assert "explicitly assigned" in prompt and "FYI" in prompt
    elif batch == "awaiting":
        assert "4 days" in prompt and "SENT" in prompt and "recipient" in prompt
        assert "informational" in prompt
    else:
        assert "Inbox" in prompt and "Archive" in prompt and "Deleted Items" in prompt
        assert "Sent Items" in prompt and "4 days" not in prompt and "unflagged" in prompt


@pytest.mark.parametrize("field,value", [
    ("base_priority", True), ("base_priority", 5), ("relevance_tier", "guess"),
    ("primary_person_hint_index", 1), ("primary_person_hint_index", False),
    ("people_hints", []), ("action_type", "send-now"), ("source_kind", "manual"),
    ("timestamp", "not-a-date"), ("source_ref", "https://outlook.evil.test/?ItemID=x"),
    ("source_ref", "javascript:alert(1)"), ("evidence_snippet", "x" * 2001),
    ("source_ref", "https://teams.microsoft.com/l/message/19:synthetic%0A@thread.v2/1"),
    ("root_topic", "Re: Fwd: "),
    ("conversation_id", "model-owned"), ("title", "x" * 301),
])
def test_candidate_is_closed_bounded_and_source_specific(refresh, field, value):
    with pytest.raises(ValueError):
        refresh.validate_batch(json.dumps(envelope(candidates=[candidate(**{field: value})])), "direct")


@pytest.mark.parametrize("tier,base,expected", [
    ("Direct", 1, 1), ("Direct", 4, 4), ("Group", 1, 2),
    ("Group", 4, 5), ("Tangential", 1, 5),
])
def test_priority_matrix(refresh, tier, base, expected):
    value = candidate(relevance_tier=tier, base_priority=base)
    assert refresh.priority(value) == expected


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "foreign", "inner", "extra"])
def test_coaching_exact_ordinal_whole_batch_gate(refresh, mutation):
    result = {"version": 1, "mode": "coaching_only", "coaching_text": "Ask Taylor to review.", "answers": []}
    value = {"version": 1, "results": [{"ordinal": i, "result": dict(result)} for i in [0, 1]]}
    if mutation == "missing":
        value["results"].pop()
    elif mutation == "duplicate":
        value["results"][1]["ordinal"] = 0
    elif mutation == "foreign":
        value["results"][1]["ordinal"] = 2
    elif mutation == "inner":
        value["results"][1]["result"]["answers"] = [{"question_id": 0, "answer": "Unrequested"}]
    else:
        value["extra"] = True
    with pytest.raises(ValueError):
        refresh.validate_coaching_batch(json.dumps(value), {0, 1})


@pytest.mark.parametrize("decisions", [
    [{"ordinal": 0, "task_id": 999}],
    [{"ordinal": 0, "task_id": 3}, {"ordinal": 0, "task_id": 3}],
    [{"ordinal": 1, "task_id": 3}],
    [], [{"ordinal": 0, "task_id": "3"}],
])
def test_semantic_foreign_partial_or_ambiguous_answer_is_no_match(refresh, decisions):
    assert refresh.semantic_matches(json.dumps({"version": 1, "results": decisions}), {0: {3}}) == {}


def test_synthetic_fixture_contract_has_no_external_dependencies(monkeypatch):
    from src import db
    from src.services import workiq_runtime, claude_runner
    def forbidden(*args, **kwargs):
        raise AssertionError("Fixture contract must be file-only")
    monkeypatch.setattr(db, "get_connection", forbidden)
    monkeypatch.setattr(workiq_runtime, "get_runtime", forbidden)
    monkeypatch.setattr(claude_runner, "run_copilot", forbidden)
    values = json.loads((Path(__file__).parent / "fixtures" / "refresh_cutover_v1.json").read_text())
    assert len({v["case_id"] for v in values}) == len(values)
    for value in values:
        assert set(value) == {"case_id", "requirement_id", "purpose", "input", "expected"}
        assert value["case_id"].startswith("REQ-RF-")
        assert "Synthetic" in value["purpose"]
