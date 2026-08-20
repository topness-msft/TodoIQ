from pathlib import Path


COMMAND = (
    Path(__file__).resolve().parents[1]
    / ".claude"
    / "commands"
    / "todo-parse.md"
).read_text(encoding="utf-8")


def test_teams_links_use_exact_chat_membership_and_profiles():
    assert "/chats/{encoded_conversation_id}/members" in COMMAND
    assert "/users/{member_object_id}" in COMMAND
    assert "exclude the signed-in user" in COMMAND


def test_teams_resolution_never_falls_back_to_recent_contacts():
    assert "Never substitute a recent chat or recent contact" in COMMAND


def test_resolved_teams_members_are_persisted_before_scheduling():
    assert "key_people" in COMMAND
    assert "before any Cowork scheduling preview" in COMMAND


def test_exact_teams_membership_profiles_are_confirmed_automatically():
    assert "Exact membership profiles are confirmed identities" in COMMAND
    assert "do not add `unresolved`" in COMMAND


def test_unique_exact_internal_name_can_be_confirmed_but_ambiguity_cannot():
    assert "exactly one internal tenant profile" in COMMAND
    assert "Multiple plausible matches" in COMMAND


def test_coaching_refresh_preserves_confirmed_selected_people():
    assert "Never replace confirmed `key_people`" in COMMAND
    assert "never re-add a person the user removed" in COMMAND
    assert "Do not scan task prose for new people during coaching-only refresh" in COMMAND
    assert "names in the current `title`, `description` and `user_notes`" not in COMMAND


def test_exact_group_membership_defaults_to_confirmed_attendance():
    assert "exact internal membership is the intended attendee set by default" in COMMAND
    assert "without `attendance_uncertain`" in COMMAND


def test_attendance_uncertainty_requires_an_explicit_ambiguity_signal():
    assert "explicitly signals attendee ambiguity" in COMMAND
    assert "attendee subset is still" in COMMAND
    assert "undecided" in COMMAND


def test_exact_guest_members_still_require_confirmation():
    assert "Guest or external membership profiles" in COMMAND
