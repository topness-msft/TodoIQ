"""Atomic HTTP/application/tray cutover using real direct cores and fake Work IQ."""

import ast
import importlib
import json
from pathlib import Path
import threading
from unittest.mock import Mock, patch

import pytest
from tornado.testing import AsyncHTTPTestCase

from src import app, models
from src.services import parsing
from tests.test_refresh_workflow import store, worker, Runtime
from tests.test_parse_workflow import raw


def tray_sync():
    # Execute the actual callback without tray module's logging/GUI side effects.
    path = Path(__file__).parents[1] / "scripts" / "todoness_tray.pyw"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    callback = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "on_sync_now")
    namespace = {"_ioloop": Mock(add_callback=lambda fn: fn()), "logger": Mock()}
    exec(compile(ast.Module(body=[callback], type_ignores=[]), str(path), "exec"), namespace)
    namespace["on_sync_now"](None, None)


class TestAtomicEntryPoints(AsyncHTTPTestCase):
    @pytest.fixture(autouse=True)
    def isolate(self, store, monkeypatch):
        self.seed = store
        self.refresh, self.parser, self.runtime = worker(monkeypatch)
        forbidden = Mock(side_effect=AssertionError("Legacy command forbidden"))
        for module_name in ("src.app", "src.handlers.task_api", "src.handlers.task_actions", "src.handlers.sync_api"):
            module = importlib.import_module(module_name)
            if hasattr(module, "run_copilot"):
                monkeypatch.setattr(module, "run_copilot", forbidden)
        yield
        self.parser.join(5)
        self.refresh.join(5)

    def get_app(self):
        return app.make_app()

    def post(self, url, body):
        return self.fetch(url, method="POST", body=json.dumps(body),
                          headers={"Content-Type": "application/json"})

    def test_all_eight_callers_use_shared_direct_owners(self):
        assert parsing.get_parse_service() is self.parser
        response = self.post("/api/tasks", {"raw_input": "Review the synthetic release checklist"})
        assert response.code == 201
        task_id = json.loads(response.body)["task"]["id"]
        self.parser.join(5)
        assert raw(task_id)["parse_intent"] == "full"
        assert raw(task_id)["parse_status"] == "parsed"

        task = self.seed(status="suggested", title="Existing suggestion", key_people="[]")
        response = self.post(f"/api/tasks/{task['id']}/action", {"action": "promote"})
        assert response.code == 200
        self.parser.join(5)
        assert raw(task["id"])["parse_intent"] == "coaching_only"
        assert raw(task["id"])["key_people"] == "[]"

        response = self.post(f"/api/tasks/{task['id']}/refresh", {})
        assert response.code == 200
        self.parser.join(5)
        assert raw(task["id"])["parse_status"] == "parsed"

        watched = self.seed(parse_status="queued", parse_intent="coaching_only")
        app._check_unparsed()
        self.parser.join(5)
        assert raw(watched["id"])["parse_status"] == "parsed"

        inline = self.seed(parse_status="queued", parse_intent="coaching_only")
        response = self.post("/api/sync-status", {})
        assert response.code == 200 and json.loads(response.body)["ok"]
        self.refresh.join(5)
        assert raw(inline["id"])["parse_status"] == "parsed"
        assert self.refresh.completion()["state"] == "succeeded"
        app._periodic_sync()
        self.refresh.join(5)
        tray_sync()
        self.refresh.join(5)
        assert app.SYNC_INTERVAL_MS == 30 * 60 * 1000
        assert app.PARSE_CHECK_INTERVAL_MS == 30 * 1000
        assert self.refresh.completion()["state"] == "succeeded"
        assert len([c for c in self.runtime.calls if c[0] == "ask" and "batch" in c[1]]) == 9

    def test_existing_refresh_preserves_selected_empty_and_pending_full(self):
        task = self.seed(raw_input="Original raw", parse_status="queued", parse_intent="full",
                         key_people="[]")
        response = self.post(f"/api/tasks/{task['id']}/refresh", {})
        assert response.code == 200
        self.parser.join(5)
        assert raw(task["id"])["parse_intent"] == "full"
        assert raw(task["id"])["key_people"] == "[]"

    def test_parse_failure_is_honest_task_state_and_public_error(self):
        self.runtime.parse_answer = '{"version":1}'
        response = self.post("/api/tasks", {"raw_input": "Review checklist"})
        assert response.code == 201
        task_id = json.loads(response.body)["task"]["id"]
        self.parser.join(5)
        response = self.fetch(f"/api/tasks/{task_id}")
        task = json.loads(response.body)["task"]
        assert task["parse_status"] == "error"
        assert task["error_message"] == models.PARSE_FAILURE_MESSAGE
        assert "SECRET-CONVERSATION" not in response.body.decode()

    def test_sync_launch_failure_retains_http_500_and_safe_completion(self):
        original = threading.Thread.start
        def fail_sync(thread):
            if thread.name == "refresh-workflow":
                raise RuntimeError("PRIVATE")
            return original(thread)
        with patch("src.services.refresh.threading.Thread.start", new=fail_sync):
            response = self.post("/api/sync-status", {})
        assert response.code == 500 and json.loads(response.body)["ok"] is False
        assert self.refresh.completion()["state"] == "failed"
        assert models.get_last_sync("full_scan") is None
        assert "PRIVATE" not in response.body.decode()
        self.refresh.join(0)


def test_no_application_parse_or_refresh_command_left():
    root = Path(__file__).parents[1]
    files = [root / "src" / "app.py", root / "scripts" / "todoness_tray.pyw"]
    files += list((root / "src" / "handlers").glob("*.py"))
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        strings = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        assert not any(value.strip().startswith(("/todo-parse", "/todo-refresh")) for value in strings), path
