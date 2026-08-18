import json
import sqlite3
from pathlib import Path


FIXTURE = Path(__file__).parent / "fixtures" / "refresh_regression_cases.json"

AUDITED_FAMILIES = {
    "dup-skill-share",
    "dup-account-deck-link",
    "dup-dashboard-prompt",
    "dup-data-source-email",
    "dup-agent-admin-sync",
    "dup-customer-transition",
    "dup-telemetry-review",
    "dup-design-support",
    "tombstone-lynx-analysis",
    "tombstone-lighthouse-validation",
    "completed-connector-metrics",
    "completed-hosting-validation",
    "near-deck-slide-ranges",
    "near-paychex-deliverables",
    "near-skill-vs-slide-fix",
    "near-lynx-vs-consumption",
    "near-outreach-vs-attendance",
    "near-loop-distinct-actions",
    "near-demo-vs-meeting-prep",
    "near-similar-title-different-people",
    "near-exact-url-different-deliverables",
    "appropriate-one-to-one",
    "appropriate-direct-mention",
    "appropriate-reply",
    "appropriate-commitment",
    "appropriate-channel",
    "appropriate-generic-assignment",
    "appropriate-meeting-owner",
    "appropriate-email-recipient",
    "appropriate-coassignment",
    "appropriate-flagged-email",
}
AUDIT_REFS = {
    "dup-skill-share": [2295, 2299],
    "dup-account-deck-link": [2326, 2330],
    "dup-dashboard-prompt": [2365, 2375],
    "dup-data-source-email": [2385, 2391],
    "dup-agent-admin-sync": [2396, 2395],
    "dup-customer-transition": [2360, 2359],
    "dup-telemetry-review": [2346, 2357],
    "dup-design-support": [2352, 2350],
    "tombstone-lynx-analysis": [2312, 2304],
    "tombstone-lighthouse-validation": [2381, 2337],
    "completed-connector-metrics": [2353, 2317],
    "completed-hosting-validation": [2301, 2294],
    "near-deck-slide-ranges": [2292, 2293],
    "near-paychex-deliverables": [2333, 2334, 2335],
    "near-skill-vs-slide-fix": [2295, 2299, 2340],
    "near-lynx-vs-consumption": [2312, 2338],
    "near-outreach-vs-attendance": [2343, 2309],
    "near-loop-distinct-actions": [2371, 2367, 2368, 2369, 2370],
    "near-demo-vs-meeting-prep": [2344, 2387],
    "near-similar-title-different-people": [],
    "near-exact-url-different-deliverables": [],
}
AUDIT_REFS.update({
    family_id: []
    for family_id in AUDITED_FAMILIES
    if family_id.startswith("appropriate-")
})

SOURCE_FIELDS = {
    "source_type",
    "item_id",
    "canonical_url_key",
    "date",
    "title",
    "people",
    "deliverable_tokens",
    "target_tokens",
    "existing_status",
}
SIGNAL_FIELDS = {
    "audience_scope",
    "request_kind",
    "ownership",
    "recipient_scope",
    "flagged",
    "freshness",
}
EXPECTED_FIELDS = {
    "dedup_decision",
    "appropriateness_decision",
    "priority_cap",
    "canonical_ref",
    "reason_codes",
}

DEDUP_DECISIONS = {
    "merge_live",
    "skip_dismissed",
    "skip_completed",
    "keep_separate",
    "review_recurrence",
    "not_applicable",
}
APPROPRIATENESS_DECISIONS = {
    "direct_high",
    "direct_shared",
    "review_ambiguous",
    "suppress_broad",
    "not_applicable",
}
SOURCE_TYPES = {"chat", "email", "meeting", "manual"}
EXISTING_STATUSES = {
    None,
    "suggested",
    "active",
    "in_progress",
    "waiting",
    "snoozed",
    "completed",
    "dismissed",
}
AUDIENCE_SCOPES = {"one_to_one", "small_group", "channel", "email", "meeting"}
REQUEST_KINDS = {
    "concrete_ask",
    "assignment",
    "commitment",
    "fyi",
    "announcement",
    "acknowledgment",
    "generic_ask",
    "none",
}
OWNERSHIP_VALUES = {"phil", "shared_with_phil", "unassigned", "other", "unknown"}
RECIPIENT_SCOPES = {
    "sole_to",
    "cc_only",
    "direct_mention",
    "reply_to_phil",
    "attendee",
    "none",
}
FRESHNESS_VALUES = {"newer", "same_or_older", "unknown"}
REASON_CODES = {
    "exact_item",
    "same_deliverable",
    "different_deliverable",
    "shared_url_insufficient",
    "teams_message_plus_deliverable",
    "person_alias",
    "teams_conversation_recall",
    "person_topic_deliverable",
    "conversation_only",
    "dismissed_tombstone",
    "new_outcome",
    "completed_old_evidence",
    "newer_evidence",
    "different_target",
    "attendance_only",
    "different_person",
    "title_only_insufficient",
    "one_to_one_ask",
    "concrete_action",
    "fyi_only",
    "no_concrete_action",
    "named_assignment",
    "direct_mention",
    "mention_only",
    "reply_to_phil",
    "acknowledgment_only",
    "phil_commitment",
    "broadcast_announcement",
    "generic_group_ask",
    "owner_unknown",
    "meeting_named_owner",
    "meeting_owner_unknown",
    "sole_to",
    "cc_only",
    "assigned_other",
    "named_coassignment",
    "group_owner_unknown",
    "flagged_user_intent",
    "automated_noise",
}


def load_corpus():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_corpus_load_is_file_only(monkeypatch):
    def reject_database(*_args, **_kwargs):
        raise AssertionError("Phase 0 corpus tests must not open SQLite")

    monkeypatch.setattr(sqlite3, "connect", reject_database)
    corpus = load_corpus()
    assert corpus["schema_version"] == 1


def test_frozen_family_set_is_exact():
    corpus = load_corpus()
    families = corpus["families"]
    assert {family["family_id"] for family in families} == AUDITED_FAMILIES
    assert len(families) == len(AUDITED_FAMILIES)
    assert {family["category"] for family in families} == {
        "duplicate",
        "near_match",
        "appropriateness",
    }


def test_every_family_has_positive_and_negative_cases():
    cases = load_corpus()["cases"]
    for family_id in AUDITED_FAMILIES:
        polarities = {
            case["polarity"]
            for case in cases
            if case["family_id"] == family_id
        }
        assert polarities == {"positive", "negative"}, family_id


def test_case_schema_and_enums_are_frozen():
    corpus = load_corpus()
    case_ids = set()
    for case in corpus["cases"]:
        assert set(case) == {
            "case_id",
            "family_id",
            "kind",
            "polarity",
            "source",
            "signals",
            "evidence_excerpt",
            "expected",
        }
        assert case["case_id"] not in case_ids
        case_ids.add(case["case_id"])
        assert case["family_id"] in AUDITED_FAMILIES
        assert case["kind"] in {"dedup", "appropriateness"}
        assert case["polarity"] in {"positive", "negative"}
        assert set(case["source"]) == SOURCE_FIELDS
        assert set(case["signals"]) == SIGNAL_FIELDS
        assert set(case["expected"]) == EXPECTED_FIELDS
        assert case["expected"]["dedup_decision"] in DEDUP_DECISIONS
        assert (
            case["expected"]["appropriateness_decision"]
            in APPROPRIATENESS_DECISIONS
        )
        assert set(case["expected"]["reason_codes"]) <= REASON_CODES
        assert case["expected"]["priority_cap"] in {None, 1, 2, 3, 4, 5}
        assert case["source"]["source_type"] in SOURCE_TYPES
        assert case["source"]["existing_status"] in EXISTING_STATUSES
        assert case["signals"]["audience_scope"] in AUDIENCE_SCOPES
        assert case["signals"]["request_kind"] in REQUEST_KINDS
        assert case["signals"]["ownership"] in OWNERSHIP_VALUES
        assert case["signals"]["recipient_scope"] in RECIPIENT_SCOPES
        assert isinstance(case["signals"]["flagged"], bool)
        assert case["signals"]["freshness"] in FRESHNESS_VALUES
        assert len(case["source"]["deliverable_tokens"]) >= 2
        assert len(case["source"]["target_tokens"]) >= 2
        assert all(case["source"]["people"])


def test_dedup_and_appropriateness_expectations_do_not_overlap():
    for case in load_corpus()["cases"]:
        expected = case["expected"]
        if case["kind"] == "dedup":
            assert expected["appropriateness_decision"] == "not_applicable"
            assert expected["dedup_decision"] != "not_applicable"
        else:
            assert expected["dedup_decision"] == "not_applicable"
            assert expected["appropriateness_decision"] != "not_applicable"


def test_fixture_contains_no_raw_or_sensitive_content():
    forbidden_fields = {
        "body",
        "raw_body",
        "html",
        "attachments",
        "quoted_chain",
        "message_body",
    }
    forbidden_markers = {
        "<html",
        "<div",
        "<p>",
        "-----original message-----",
        "\nfrom:",
        "\nsubject:",
        "@microsoft.com",
        "tenantid=",
    }
    for case in load_corpus()["cases"]:
        assert not (set(case["source"]) & forbidden_fields)
        assert not (set(case["signals"]) & forbidden_fields)
        excerpt = case["evidence_excerpt"]
        assert 1 <= len(excerpt) <= 160
        assert "\n" not in excerpt
        assert "<" not in excerpt and ">" not in excerpt
        lowered = excerpt.lower()
        assert not any(marker in lowered for marker in forbidden_markers)


def test_audit_references_are_ids_only():
    for family in load_corpus()["families"]:
        assert set(family) == {"family_id", "category", "audit_refs"}
        assert family["audit_refs"] == AUDIT_REFS[family["family_id"]]
        assert all(isinstance(value, int) and value > 0 for value in family["audit_refs"])
