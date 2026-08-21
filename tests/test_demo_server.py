import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from unittest import mock

import pytest

from src.services import claude_runner, cowork_runner, runtime_mode
from src.services.cowork_runner import parse_source_url
from scripts import demo_server


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "demo_server.py"


def _run(tmp_path, *args):
    env = {
        **os.environ,
        "RIVETER_DEMO_DIR": str(tmp_path / "demo"),
        "RIVETER_DEMO_PORT": "18776",
    }
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _digest(path):
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT title,status,priority,action_type,key_people,waiting_activity,"
            "description,source_date,source_snippet,source_url,created_at,updated_at "
            "FROM tasks ORDER BY id"
        ).fetchall()
        actions = conn.execute(
            "SELECT state,action_type,finding,draft,destination_kind,"
            "destination_ref,destination_display,delivery_channel,"
            "destination_source,tool_trace,blocked_question,answered_interaction,"
            "had_interaction,created_at,updated_at "
            "FROM task_actions ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    return hashlib.sha256(repr((rows, actions)).encode()).hexdigest()


def test_demo_reset_is_isolated_and_deterministic(tmp_path):
    production_shape = tmp_path / "production.db"
    production_shape.write_bytes(b"production sentinel")

    first = _run(tmp_path, "reset")
    assert first.returncode == 0, first.stderr
    demo_db = tmp_path / "demo" / "riveter-demo.db"
    assert demo_db.exists()
    settings = json.loads(
        (tmp_path / "demo" / "settings.json").read_text(encoding="utf-8")
    )
    assert settings["demo_mode"] is True
    assert settings["cowork_api_transport"] is True
    first_digest = _digest(demo_db)

    second = _run(tmp_path, "reset")
    assert second.returncode == 0, second.stderr
    assert _digest(demo_db) == first_digest
    assert production_shape.read_bytes() == b"production sentinel"

    conn = sqlite3.connect(demo_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 17
        task_statuses = dict(
            conn.execute(
                "SELECT status,COUNT(*) FROM tasks GROUP BY status"
            ).fetchall()
        )
        source_types = {
            row[0] for row in conn.execute("SELECT DISTINCT source_type FROM tasks")
        }
        source_type_counts = dict(
            conn.execute(
                "SELECT source_type,COUNT(*) FROM tasks GROUP BY source_type"
            ).fetchall()
        )
        action_types = {
            row[0] for row in conn.execute("SELECT DISTINCT action_type FROM tasks")
        }
        action_states = {
            row[0]
            for row in conn.execute("SELECT DISTINCT state FROM task_actions")
        }
        action_count = conn.execute("SELECT COUNT(*) FROM task_actions").fetchone()[0]
        titles = {
            row[0] for row in conn.execute("SELECT title FROM tasks")
        }
        suggestion_activity = [
            json.loads(row[0])
            for row in conn.execute(
                "SELECT waiting_activity FROM tasks "
                "WHERE status = 'suggested' AND waiting_activity IS NOT NULL"
            )
        ]
        suggestion_sources = [
            row[0]
            for row in conn.execute(
                "SELECT source_snippet FROM tasks WHERE source_snippet IS NOT NULL "
                "ORDER BY id"
            )
        ]
        people_payloads = [
            json.loads(row[0])
            for row in conn.execute(
                "SELECT key_people FROM tasks WHERE key_people IS NOT NULL"
            )
        ]
        timestamps = conn.execute(
            "SELECT DISTINCT created_at,updated_at FROM tasks"
        ).fetchall()
        source_rows = conn.execute(
            "SELECT source_id,source_type,source_url,source_date,source_snippet,"
            "description,key_people FROM tasks"
        ).fetchall()
    finally:
        conn.close()
    assert task_statuses == {
        "suggested": 7,
        "active": 4,
        "in_progress": 1,
        "waiting": 1,
        "completed": 1,
        "dismissed": 2,
        "snoozed": 1,
    }
    assert source_types == {"chat", "email", "meeting", "manual"}
    assert all(source_type_counts[source] >= 3 for source in ("chat", "meeting", "email"))
    assert {
        "follow-up",
        "respond-email",
        "schedule-meeting",
        "prepare",
        "awaiting-response",
        "review-document",
    }.issubset(action_types)
    assert action_states == {"ready"}
    assert action_count == 3
    assert titles == {
        "Find the current tester for the new Cowork API with Rima",
        "Follow up with Luis on the generated customer presentations",
        "Confirm Manuela's PPCC distribution list preference",
        "Clarify the AIA engagement model with Bobby",
        "Coordinate the five-customer dashboard examples with Adrian",
        "Prepare the account-team briefing with Aamer",
        "Schedule the Lighthouse workshop mapping session with Steve",
        "Schedule the Friday demo review with Bobby Chang",
        "Reply to Adrian with the FY27 program direction",
        "Send Luis the customer-assignment update in Teams",
        "Prepare the Lighthouse customer-list rationale with Rima",
        "Review the dashboard customer examples with Aamer",
        "Wait for Steve's Lighthouse workshop invitation",
        "Document Manuela's customer-search requirements",
        "Review the Power Up asset transition with Luis",
        "Dismiss the superseded AMR kickoff follow-up with Bobby",
        "Dismiss the outdated FY27 guidance request to Adrian",
    }
    assert [item["status"] for item in suggestion_activity].count(
        "likely_resolved"
    ) == 2
    assert len(suggestion_activity) == 4
    assert {item["status"] for item in suggestion_activity} == {
        "likely_resolved", "activity_detected", "may_be_resolved"
    }
    assert all(item.get("summary") and item.get("checked_at") for item in suggestion_activity)
    assert all(item["checked_at"] == "2026-08-20T18:00:00Z" for item in suggestion_activity)
    people = [person for payload in people_payloads for person in payload]
    assert {person["name"] for person in people} == {
        "Rima Reyes",
        "Bobby Chang",
        "Luis Camino",
        "Steve Jeffery",
        "Manuela Pichler",
        "Adrian Maclean",
        "Aamer Kaleem",
    }
    assert all(
        person.get("email", "").endswith("@example.invalid")
        and person.get("aad_object_id")
        and isinstance(person.get("alternatives"), list)
        for person in people
    )
    assert any(person["alternatives"] for person in people)
    assert len(suggestion_sources) == 16
    assert all(summary.startswith("On ") for summary in suggestion_sources)
    assert timestamps == [("2026-08-20T18:00:00Z", "2026-08-20T18:00:00Z")]
    approved_oids = {
        "00000000-0000-4000-8000-000000000000",
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
        "33333333-3333-4333-8333-333333333333",
        "55555555-5555-4555-8555-555555555555",
    }
    chat_urls = []
    for (
        source_id, source_type, source_url, source_date, source_snippet,
        description, key_people,
    ) in source_rows:
        assert source_id.startswith("demo::")
        people_for_task = json.loads(key_people)
        names = {person["name"] for person in people_for_task}
        assert len(description) >= 200
        assert any(name in description for name in names)
        assert re.search(r"August \d{1,2}, 2026", description)
        assert "Current state:" in description
        assert "Next step:" in description
        if source_type == "chat":
            assert source_url is not None
            assert source_url.startswith("https://teams.microsoft.com/l/message/")
            parsed = parse_source_url(source_url)
            assert parsed["kind"] == "one_to_one"
            assert parsed["is_broadcast"] is False
            assert parsed["conversation_id"]
            chat_urls.append(source_url)
            url_oids = set(re.findall(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
                r"[89ab][0-9a-f]{3}-[0-9a-f]{12}",
                source_url,
                flags=re.IGNORECASE,
            ))
            assert url_oids and url_oids <= approved_oids
        else:
            assert source_url is None
        if source_snippet is not None:
            assert source_date
            assert 80 <= len(source_snippet) <= 220
            assert source_snippet.startswith("On ")
            assert any(name in source_snippet for name in names)
            assert not (
                source_snippet.startswith(('"', "“"))
                and source_snippet.endswith(('"', "”"))
            )
    assert len(chat_urls) == 6
    assert len(set(chat_urls)) == 6


def test_demo_prebuilt_cowork_actions_are_reviewable_and_safe(tmp_path):
    result = _run(tmp_path, "reset")
    assert result.returncode == 0, result.stderr
    conn = sqlite3.connect(tmp_path / "demo" / "riveter-demo.db")
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT t.source_id,a.* FROM task_actions a "
            "JOIN tasks t ON t.id=a.task_id ORDER BY t.source_id"
        ).fetchall()
    finally:
        conn.close()

    assert {row["source_id"] for row in rows} == {
        "demo::luis::customer-assignment",
        "demo::adrian::fy27-direction",
        "demo::steve::workshop-mapping",
    }
    expected = {
        "demo::luis::customer-assignment": ("follow-up", "teams", "Luis Camino"),
        "demo::adrian::fy27-direction": ("respond-email", "email", "Adrian Maclean"),
        "demo::steve::workshop-mapping": (
            "schedule-meeting", None, "Steve Jeffery, Rima Reyes, and Adrian Maclean"
        ),
    }
    write_tools = ("send", "postmessage", "createevent")
    for row in rows:
        action_type, channel, destination = expected[row["source_id"]]
        assert row["state"] == "ready"
        assert row["action_type"] == action_type
        assert row["delivery_channel"] == channel
        assert row["destination_display"] == destination
        assert row["finding"] and row["draft"]
        assert row["destination_ref"]
        assert row["destination_source"] == "auto_key_people"
        assert all(
            row[field] is None
            for field in (
                "conversation_id",
                "destination_confirmed_at",
                "parent_action_id",
                "execution_requested_at",
                "delivery_confirmed_at",
                "completed_at",
                "terminal_status",
            )
        )
        trace = json.loads(row["tool_trace"])
        assert trace
        assert not any(
            item.get("ok") is True
            and any(token in item.get("tool_name", "").lower().replace("_", "")
                    for token in write_tools)
            for item in trace
        )


def test_demo_meeting_action_has_certified_availability_matrix(tmp_path):
    result = _run(tmp_path, "reset")
    assert result.returncode == 0, result.stderr
    conn = sqlite3.connect(tmp_path / "demo" / "riveter-demo.db")
    conn.row_factory = sqlite3.Row
    try:
        task = conn.execute(
            "SELECT * FROM tasks WHERE source_id='demo::steve::workshop-mapping'"
        ).fetchone()
        action = conn.execute(
            "SELECT * FROM task_actions WHERE task_id=?", (task["id"],)
        ).fetchone()
    finally:
        conn.close()

    people = json.loads(task["key_people"])
    attendees = cowork_runner.schedule_attendees(dict(task))
    assert len(people) == len(attendees) == 3
    assert all(person["aad_object_id"] and person["email"] for person in people)
    assert action["had_interaction"] == 1
    interaction = json.loads(action["blocked_question"])
    assert cowork_runner.schedule_interaction_is_certified(
        interaction, attendees, duration_minutes=25
    )
    options = interaction["questions"][0]["options"]
    evidence = interaction["schedule_evidence"]
    assert len(options) == len(evidence["slots"]) == 3
    attendee_emails = {person["email"] for person in attendees}
    assert set(evidence["attendees"]) == attendee_emails
    for slot in evidence["slots"]:
        start = datetime.fromisoformat(slot["start"])
        end = datetime.fromisoformat(slot["end"])
        assert start.minute in {5, 35}
        assert (end - start).total_seconds() == 25 * 60
        assert set(slot["availability"]) == attendee_emails
        assert set(slot["availability"].values()) <= {"free", "tentative"}
    answer = json.loads(action["answered_interaction"])
    assert answer["kind"] == "interaction_answer"
    assert answer["interaction"] == interaction
    assert answer["answers"] == {"0": options[0]["value"]}
    trace = json.loads(action["tool_trace"])
    event = next(
        item["input"] for item in trace
        if item["tool_name"].endswith("CreateEvent")
    )
    assert set(event["attendees"]) == attendee_emails
    assert event["start"] == evidence["slots"][0]["start"]
    assert event["end"] == evidence["slots"][0]["end"]
    assert event["is_online_meeting"] is True


def test_demo_stop_refuses_unrelated_reused_pid(tmp_path):
    demo_dir = tmp_path / "demo"
    demo_dir.mkdir()
    (demo_dir / "riveter-demo.pid").write_text(json.dumps({
        "pid": os.getpid(),
        "created": 0,
        "executable": "not-the-current-process.exe",
    }))

    result = _run(tmp_path, "stop")

    assert result.returncode != 0
    assert "refusing" in (result.stdout + result.stderr).lower()


def test_demo_process_identity_requires_matching_creation_token(monkeypatch):
    record = {"pid": 42, "created": 100, "executable": "python.exe"}
    monkeypatch.setattr(demo_server, "_pid_record", lambda: record)
    monkeypatch.setattr(
        demo_server,
        "_process_identity",
        lambda _pid: dict(record),
    )
    assert demo_server._demo_process(42)
    monkeypatch.setattr(
        demo_server,
        "_process_identity",
        lambda _pid: {**record, "created": 101},
    )
    assert not demo_server._demo_process(42)


def test_external_runners_fail_closed_in_demo_mode(monkeypatch):
    monkeypatch.setenv("RIVETER_DEMO_MODE", "1")
    with mock.patch("subprocess.Popen") as popen:
        result = claude_runner.run_copilot("/todo-refresh", label="sync")
    assert result["ok"] is False
    assert "demo" in result["message"].lower()
    popen.assert_not_called()

    with pytest.raises(RuntimeError, match="demo"):
        cowork_runner.start_preview(1, "Do not run")
    with pytest.raises(RuntimeError, match="demo"):
        cowork_runner.continue_preview(1, "demo:user:preview", "Continue")
    with pytest.raises(RuntimeError, match="demo"):
        cowork_runner.start_execution(1, "Execute", "demo:user:preview")
    with pytest.raises(RuntimeError, match="demo"):
        cowork_runner.answer_interaction(
            "demo:user:preview", "invocation", {"0": "answer"}
        )
    cancel = mock.Mock()
    assert cowork_runner.cancel_run("demo:user:preview", _post=cancel) is False
    cancel.assert_not_called()


def test_live_demo_capabilities_are_independently_allowlisted(monkeypatch):
    monkeypatch.setenv("RIVETER_DEMO_MODE", "1")
    assert not runtime_mode.todo_parse_enabled()
    assert not runtime_mode.cowork_session_enabled()
    assert not runtime_mode.cowork_execute_enabled()
    assert not runtime_mode.copilot_command_enabled("/todo-refresh", "sync")

    monkeypatch.setenv("RIVETER_DEMO_ALLOW_TODO_PARSE", "1")
    monkeypatch.setenv("RIVETER_DEMO_ALLOW_COWORK_SESSION", "1")
    monkeypatch.setenv("RIVETER_DEMO_ALLOW_COWORK_EXECUTE", "1")
    assert runtime_mode.todo_parse_enabled()
    assert runtime_mode.cowork_session_enabled()
    assert runtime_mode.cowork_execute_enabled()
    assert runtime_mode.copilot_command_enabled("/todo-parse", "parse")
    assert not runtime_mode.copilot_command_enabled("/todo-refresh", "parse")
    assert not runtime_mode.copilot_command_enabled("/todo-parse", "sync")


def test_todo_parse_command_uses_configured_database():
    command = (ROOT / ".claude" / "commands" / "todo-parse.md").read_text(
        encoding="utf-8"
    )
    assert "TODONESS_DB_PATH" in command
    assert "sqlite3.connect('$PROJECT_ROOT/data/claudetodo.db')" not in command


def test_live_demo_allows_cowork_entrypoints_with_fake_transports(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("RIVETER_DEMO_MODE", "1")
    monkeypatch.setenv("RIVETER_DEMO_ALLOW_COWORK_SESSION", "1")
    monkeypatch.setenv("RIVETER_DEMO_ALLOW_COWORK_EXECUTE", "1")

    class FakeThread:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def start(self):
            return None

    with mock.patch.object(
        cowork_runner, "api_transport_enabled", return_value=True
    ), mock.patch.object(
        cowork_runner, "build_callback_config", return_value=tmp_path / "callback.json"
    ), mock.patch.object(
        cowork_runner, "write_prompt_file", return_value=tmp_path / "prompt.txt"
    ), mock.patch.object(
        cowork_runner,
        "tenant_barrier_precheck",
        return_value={"status": "ok", "reason": "test"},
    ), mock.patch.object(cowork_runner.threading, "Thread", FakeThread):
        preview = cowork_runner.start_preview(
            901, "Preview", conversation_id="demo:user:preview"
        )
        follow_up = cowork_runner.continue_preview(
            902, "demo:user:follow-up", "Refine"
        )
        execution = cowork_runner.start_execution(
            903, "Execute", "demo:user:execute"
        )
    assert preview == cowork_runner.preview_label(901)
    assert follow_up == cowork_runner.preview_label(902)
    assert execution == cowork_runner.execution_label(903)
    with cowork_runner._runs_lock:
        cowork_runner._runs.pop(preview, None)
        cowork_runner._runs.pop(follow_up, None)
        cowork_runner._runs.pop(execution, None)

    class Response:
        status_code = 202

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, *_args, **_kwargs):
            return Response()

    with mock.patch.object(
        cowork_runner,
        "_api_auth_fn",
        return_value=("token", "https://api", "tenant", "user"),
    ), mock.patch.object(cowork_runner, "_api_http_client_fn", return_value=Client()):
        assert cowork_runner.answer_interaction(
            "demo:user:answer", "invocation", {"0": "answer"}
        ) is True

def test_demo_server_routes_are_live_and_sync_skills_stay_forbidden(tmp_path):
    reset = _run(tmp_path, "reset")
    assert reset.returncode == 0, reset.stderr
    started = _run(tmp_path, "start")
    assert started.returncode == 0, started.stderr
    base = "http://127.0.0.1:18776"
    try:
        with urllib.request.urlopen(base + "/api/stats", timeout=5) as response:
            assert response.status == 200
            assert b'"total"' in response.read()
        with urllib.request.urlopen(base + "/", timeout=5) as response:
            assert response.status == 200
            assert b'data-demo-mode="true"' in response.read()
        with urllib.request.urlopen(
            base + "/api/tasks?source_type=chat", timeout=5
        ) as response:
            payload = json.loads(response.read())
            chat_tasks = [
                task for task in payload["tasks"] if task["source_type"] == "chat"
            ]
            assert len(chat_tasks) == 6
            assert all(
                task["source_url"].startswith(
                    "https://teams.microsoft.com/l/message/"
                )
                for task in chat_tasks
            )
        for path, body in (
            ("/api/sync-status", b"{}"),
            ("/api/tasks/1/skill", b'{"skill":"follow-up"}'),
        ):
            request = urllib.request.Request(
                base + path,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with pytest.raises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request, timeout=5)
            assert error.value.code == 403
            assert b"demo mode" in error.value.read().lower()
    finally:
        stopped = _run(tmp_path, "stop")
        assert stopped.returncode == 0, stopped.stderr
