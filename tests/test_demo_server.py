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
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 12
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
        suggestion_activity = [
            json.loads(row[0])
            for row in conn.execute(
                "SELECT waiting_activity FROM tasks "
                "WHERE status = 'suggested' AND waiting_activity IS NOT NULL"
            )
        ]
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
        "Follow up on the Kickstarter pilot feedback from Raj",
        "Check whether Adrian replied about the executive pane layout",
        "Confirm the demo video assets are ready to use",
        "Review the run-of-show timing with Em D'Arcy",
    }
    assert [item["status"] for item in suggestion_activity].count(
        "likely_resolved"
    ) == 2
    assert all(
        item.get("summary") and item.get("checked_at")
        for item in suggestion_activity
    )


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
