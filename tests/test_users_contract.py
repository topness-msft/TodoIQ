from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMMAND = (ROOT / ".claude" / "commands" / "users.md")


def test_users_command_is_the_only_workiq_identity_boundary():
    text = COMMAND.read_text(encoding="utf-8")
    assert "/users/{exact_id_or_email}" in text
    assert "displayName eq" in text
    assert "Name candidates are never persisted" in text
    assert "person_backfill" in text
    assert "seed_audited_aliases(conn, commit=True)" in text
    assert "INSERT INTO person" not in text
    assert "UPDATE person" not in text


def test_startup_and_tornado_do_not_run_identity_backfill():
    app = (ROOT / "src" / "app.py").read_text(encoding="utf-8")
    handlers = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "src" / "handlers").glob("*.py")
    )
    assert "person_backfill" not in app
    assert "person_backfill" not in handlers
    backfill = (
        ROOT / "src" / "services" / "person_backfill.py"
    ).read_text(encoding="utf-8").lower()
    assert "import workiq" not in backfill
    assert "ask_work_iq" not in backfill
