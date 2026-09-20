import json
import threading
from unittest.mock import Mock, patch

import pytest
import tornado.testing
import tornado.web

from src.handlers.task_actions import TaskSkillHandler
from src.handlers.sync_api import RunnerStatusHandler
from src.services import skills
from tests.test_skill_workflow import store, runtime, finish
from tests.test_parse_workflow import raw


class TestSkillAPI(tornado.testing.AsyncHTTPTestCase):
    @pytest.fixture(autouse=True)
    def storage(self, store, monkeypatch):
        self.task = store()
        self.monkeypatch = monkeypatch
        self.provider = runtime()
        self.worker = skills.SkillService(runtime_provider=lambda: self.provider)
        monkeypatch.setattr(skills, "_service", self.worker)

    def get_app(self):
        return tornado.web.Application([
            (r"/api/tasks/(\d+)/skill", TaskSkillHandler),
            (r"/api/runner-status", RunnerStatusHandler),
        ])

    def post(self, skill, task_id=None):
        return self.fetch(f'/api/tasks/{task_id or self.task["id"]}/skill', method="POST",
                          body=json.dumps({"skill": skill}))

    def test_all_six_direct_routes_no_subprocess_and_status_privacy(self):
        with patch("subprocess.Popen", side_effect=AssertionError("No process")), \
             patch("src.services.claude_runner.run_copilot", side_effect=AssertionError("No CLI")), \
             patch("src.handlers.task_actions.broadcast") as broadcast:
            for skill in sorted(skills.VALID_SKILLS):
                with self.subTest(skill=skill):
                    response = self.post(skill)
                    assert response.code == 200
                    admitted = json.loads(response.body)
                    assert admitted["ok"] and admitted["started_at"] and admitted["run_id"]
                    assert finish(self.worker, admitted)["state"] == "succeeded"
                    status = json.loads(self.fetch("/api/runner-status").body)
                    label = f'skill:{skill}:{self.task["id"]}'
                    assert status["_completed"][label]["persisted"] is True
                    assert status["_completed"][label]["run_id"] == admitted["run_id"]
                    assert label not in status
                    assert "PRIVATE" not in json.dumps(status)
            assert broadcast.call_count == 6

    def test_busy_invalid_missing_and_demo_never_broadcast(self):
        entered, release = threading.Event(), threading.Event()
        original = self.provider.execute_ask.side_effect
        def ask(prompt, **kwargs):
            entered.set()
            assert release.wait(5)
            return original(prompt, **kwargs)
        self.provider.execute_ask.side_effect = ask
        with patch("src.handlers.task_actions.broadcast") as broadcast:
            admitted = json.loads(self.post("prepare").body)
            assert entered.wait(5)
            try:
                status = json.loads(self.fetch("/api/runner-status").body)
                label = f'skill:prepare:{self.task["id"]}'
                assert status[label] and status["_runs"][label]["run_id"] == admitted["run_id"]
                assert json.loads(self.post("prepare").body)["ok"] is False
                assert self.post("unknown").code == 400
                assert self.post("prepare", 9999).code == 404
                with self.monkeypatch.context() as environment:
                    environment.setenv("RIVETER_DEMO_MODE", "1")
                    environment.setenv("RIVETER_DEMO_ALLOW_TODO_PARSE", "1")
                    for skill in skills.VALID_SKILLS:
                        assert self.post(skill).code == 403
                assert broadcast.call_count == 1
            finally:
                release.set()
            assert finish(self.worker, admitted)["state"] == "succeeded"
