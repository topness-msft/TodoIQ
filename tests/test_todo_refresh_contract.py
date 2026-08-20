from pathlib import Path


COMMAND = (
    Path(__file__).resolve().parents[1]
    / ".claude"
    / "commands"
    / "todo-refresh.md"
).read_text(encoding="utf-8")
NORMALIZED = " ".join(COMMAND.split())


def test_refresh_preserves_user_selected_people_on_existing_tasks():
    assert "selected `key_people` is authoritative" in NORMALIZED
    assert "Never overwrite `key_people`" in NORMALIZED
    assert "A person absent from `key_people` may have been explicitly removed" in NORMALIZED


def test_refresh_does_not_reparse_existing_structured_tasks():
    assert "Do not parse or reconstruct already-parsed tasks" in NORMALIZED
    assert "never re-run Teams membership resolution" in NORMALIZED
