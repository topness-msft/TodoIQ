import hashlib
import os
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import pytest

from src.services import claude_runner, cowork_runner
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
    first_digest = _digest(demo_db)

    second = _run(tmp_path, "reset")
    assert second.returncode == 0, second.stderr
    assert _digest(demo_db) == first_digest
    assert production_shape.read_bytes() == b"production sentinel"

    conn = sqlite3.connect(demo_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] >= 8
        task_statuses = {
            row[0]
            for row in conn.execute("SELECT DISTINCT status FROM tasks")
        }
        action_states = {
            row[0]
            for row in conn.execute("SELECT DISTINCT state FROM task_actions")
        }
    finally:
        conn.close()
    assert {"suggested", "active", "waiting", "completed"} <= task_statuses
    assert {"ready", "executed", "execute_unconfirmed", "previewing"} <= action_states


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


def test_demo_server_routes_are_live_but_external_actions_are_forbidden(tmp_path):
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
        with urllib.request.urlopen(base + "/api/tasks/2/cowork", timeout=5) as response:
            assert response.status == 200
            assert b'"state": "ready"' in response.read()
        for path, body in (
            ("/api/sync-status", b"{}"),
            ("/api/tasks/1/refresh", b"{}"),
            ("/api/tasks/1/skill", b'{"skill":"follow-up"}'),
            ("/api/tasks/1/cowork", b'{"interaction_mode":"interaction"}'),
            ("/api/tasks/2/cowork/refine", b'{"instruction":"Improve it"}'),
            (
                "/api/tasks/3/cowork/answer",
                b'{"invocation_id":"demo","answers":{"0":"answer"}}',
            ),
            ("/api/tasks/2/cowork/execute", b"{}"),
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
        delete = urllib.request.Request(
            base + "/api/tasks/3/cowork",
            method="DELETE",
        )
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(delete, timeout=5)
        assert error.value.code == 403
    finally:
        stopped = _run(tmp_path, "stop")
        assert stopped.returncode == 0, stopped.stderr
