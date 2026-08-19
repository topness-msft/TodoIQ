import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import pytest

from src.services import claude_runner, cowork_runner, runtime_mode
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
            "SELECT title,status,priority,action_type,key_people "
            "FROM tasks ORDER BY id"
        ).fetchall()
        actions = conn.execute(
            "SELECT state,action_type,destination_display,draft "
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
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 8
        task_statuses = {
            row[0]
            for row in conn.execute("SELECT DISTINCT status FROM tasks")
        }
        action_states = {
            row[0]
            for row in conn.execute("SELECT DISTINCT state FROM task_actions")
        }
        titles = {
            row[0] for row in conn.execute("SELECT title FROM tasks")
        }
    finally:
        conn.close()
    assert task_statuses == {
        "suggested", "active", "waiting", "completed", "dismissed", "snoozed"
    }
    assert action_states == set()
    assert titles == {
        "Review the demo run-of-show",
        "Schedule the Friday demo review with Bobby Chang and Em D'Arcy",
        "Send Raj the complete Kickstarter adoption materials in Teams",
        "Reply to Adrian about Srini's executive pane",
        "Waiting on the final demo video assets",
        "Finish the isolated demo environment",
        "Send the outdated demo script",
        "Check pilot adoption metrics",
    }


def test_demo_stop_refuses_unrelated_reused_pid(tmp_path):
    demo_dir = tmp_path / "demo"
    demo_dir.mkdir()
    (demo_dir / "riveter-demo.pid").write_text(str(os.getpid()))

    result = _run(tmp_path, "stop")

    assert result.returncode != 0
    assert "refusing" in (result.stdout + result.stderr).lower()


def test_demo_process_identity_requires_serve_argument():
    command = f'python.exe "{SCRIPT}"'
    assert demo_server._command_is_demo_serve(command + " serve")
    assert not demo_server._command_is_demo_serve(command + " reset")
    assert not demo_server._command_is_demo_serve(command + " status")


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
    assert not runtime_mode.demo_schedule_choices_enabled()
    assert not runtime_mode.copilot_command_enabled("/todo-refresh", "sync")

    monkeypatch.setenv("RIVETER_DEMO_ALLOW_TODO_PARSE", "1")
    monkeypatch.setenv("RIVETER_DEMO_ALLOW_COWORK_SESSION", "1")
    monkeypatch.setenv("RIVETER_DEMO_ALLOW_COWORK_EXECUTE", "1")
    monkeypatch.setenv("RIVETER_DEMO_TRUST_SCHEDULE_CHOICES", "1")
    assert runtime_mode.todo_parse_enabled()
    assert runtime_mode.cowork_session_enabled()
    assert runtime_mode.cowork_execute_enabled()
    assert runtime_mode.demo_schedule_choices_enabled()
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


def test_schedule_choice_trust_is_demo_only(monkeypatch):
    attendees = [
        {"name": "Bobby Chang", "email": "bobby.chang@microsoft.com"},
        {"name": "Em D'Arcy", "email": "emdarcy@microsoft.com"},
    ]
    marker = (
        '[avail:{"bobby.chang@microsoft.com":"free",'
        '"emdarcy@microsoft.com":"free"}]'
    )
    interaction = {
        "invocation_id": "demo-schedule",
        "questions": [{
            "id": "0",
            "header": "Pick a time",
            "question": "Which verified time works?",
            "multi_select": False,
            "options": [
                {
                    "value": value,
                    "label": value,
                    "description": marker,
                    "image_url": "",
                }
                for value in ("Wed 1 PM ET", "Thu 10 AM ET", "Fri 2 PM ET")
            ],
        }],
    }
    events = [
        ("ts", {
            "tid": "tool-1",
            "tn": "mcp__outlook_calendar__FindMeetingTimes",
            "inp": json.dumps({
                "attendees": [
                    "bobby.chang@microsoft.com",
                    "emdarcy@microsoft.com",
                ]
            }),
        }),
        ("tx", {
            "tid": "tool-1",
            "tn": "mcp__outlook_calendar__FindMeetingTimes",
            "ok": True,
        }),
    ]
    assert cowork_runner._demo_schedule_result(interaction, attendees) is None

    monkeypatch.setenv("RIVETER_DEMO_MODE", "1")
    monkeypatch.setenv("RIVETER_DEMO_TRUST_SCHEDULE_CHOICES", "1")
    trusted = cowork_runner._demo_schedule_result(interaction, attendees)
    certified = cowork_runner.certify_schedule_interaction(
        interaction, events, attendees, trusted
    )
    assert certified["schedule_evidence"]["valid"] is True
    assert certified["schedule_evidence"]["attendees"] == [
        "bobby.chang@microsoft.com",
        "emdarcy@microsoft.com",
    ]


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
