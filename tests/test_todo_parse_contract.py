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
