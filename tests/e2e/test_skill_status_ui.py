"""Real server/worker/status/SQLite integration; only the provider is synthetic."""

import asyncio
import json
from pathlib import Path
import threading
import urllib.request
from unittest.mock import patch

import pytest
from playwright.sync_api import expect
import tornado.httpserver
import tornado.ioloop
import tornado.netutil

from src import db
from src.app import make_app
from src.services import skills, workiq_runtime, workspace_settings


class SyntheticProvider:
    def __init__(self):
        self.release = threading.Event()
        self.entered = threading.Event()
        self.fail = False

    def execute_ask(self, question, *, timeout):
        self.entered.set()
        if not self.release.wait(30):
            raise RuntimeError("Synthetic gate expired")
        if self.fail:
            raise RuntimeError("Synthetic provider failure")
        return {"answer": json.dumps({
            "event": "Synthetic release review", "date": None, "checklist": ["Read the release notes"],
            "talking_points": ["Confirm readiness"], "materials": ["Release notes"],
            "questions": ["Are we ready?"], "estimate_minutes": 25,
        })}


@pytest.fixture(scope="module")
def skill_server(tmp_path_factory):
    folder = tmp_path_factory.mktemp("skill-visual")
    ready = threading.Event()
    state = {"provider": SyntheticProvider()}
    worker = skills.SkillService(runtime_provider=lambda: state["provider"])
    with patch.object(db, "DB_PATH", folder / "skills.db"), \
         patch.object(db, "DB_DIR", folder), \
         patch.object(skills, "_service", worker), \
         patch.object(workspace_settings, "SETTINGS_PATH", folder / "absent-settings.json"), \
         patch.object(workiq_runtime, "get_runtime", side_effect=AssertionError("No live M365")), \
         patch("src.services.claude_runner.run_copilot", side_effect=AssertionError("No CLI")):
        conn = db.get_connection()
        db.init_db(conn)
        conn.close()

        def serve():
            asyncio.set_event_loop(asyncio.new_event_loop())
            loop = tornado.ioloop.IOLoop.current()
            server = tornado.httpserver.HTTPServer(make_app())
            sockets = tornado.netutil.bind_sockets(0, "127.0.0.1")
            server.add_sockets(sockets)
            state.update(loop=loop, server=server, url=f"http://127.0.0.1:{sockets[0].getsockname()[1]}")
            ready.set()
            loop.start()
            server.stop()
            loop.close(all_fds=True)
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        assert ready.wait(10)
        assert urllib.request.urlopen(state["url"] + "/api/stats", timeout=5).status == 200
        yield state
        state["provider"].release.set()
        state["loop"].add_callback(state["loop"].stop)
        thread.join(5)
        assert not thread.is_alive()


# Avoid the repository's unrelated session server and external-temp database.
@pytest.fixture(scope="session")
def tornado_server():
    yield None


@pytest.mark.parametrize("route", ["/", "/todo"])
@pytest.mark.parametrize("width,height", [(1440, 900), (375, 720)])
def test_api_launched_skill_polls_and_refetches_real_persisted_output(page, skill_server, route, width, height):
    state = skill_server
    state["provider"] = SyntheticProvider()
    provider = state["provider"]
    url = state["url"]
    task = page.request.post(url + "/api/tasks", data={
        "title": "Synthetic release review", "source_type": "manual", "status": "active",
        "parse_status": "parsed", "action_type": "prepare", "skill_output": "Historical preparation",
        "key_people": json.dumps([{"name": "Taylor Example", "email": "taylor@example.test"}]),
    }).json()["task"]["id"]
    assert page.request.put(url + f"/api/tasks/{task}", data={"skill_output": "Historical preparation"}).ok
    requests = []
    page.on("request", lambda request: requests.append(request.url))
    page.set_viewport_size({"width": width, "height": height})
    try:
        page.goto(url + route)
        page.wait_for_function("(id) => typeof tasks !== 'undefined' && tasks.some(t => t.id === id)", arg=task)
        page.evaluate("(id) => selectTask(id)", task)
        page.wait_for_timeout(300)
        admitted = page.evaluate("""async id => (await fetch('/api/tasks/' + id + '/skill', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({skill: 'prepare'})
        })).json()""", task)
        assert admitted["ok"] and provider.entered.wait(5)
        if route == "/todo":
            # Modern cards need not display the legacy output; their existing
            # launch toast and background refetch still have to work.
            indicator = page.locator("#toast-container .toast").last
            expect(indicator).to_contain_text("Running prepare", timeout=7000)
        else:
            page.wait_for_function("Object.keys(_runningSkills).length === 1")
            indicator = page.locator("#detail-pane")
        box = indicator.bounding_box()
        assert box and box["width"] > 0 and box["height"] > 0
        folder = Path("temp") / "skill-migration"
        folder.mkdir(parents=True, exist_ok=True)
        stem = f'{"root" if route == "/" else "todo"}-{width}'
        page.screenshot(path=str(folder / f"{stem}-running.png"), full_page=True)
        provider.release.set()
        page.wait_for_function("""id => {
            const task = tasks.find(t => t.id === id);
            return task && task.skill_output && task.skill_output.includes('Read the release notes');
        }""", arg=task, timeout=15000)
        status = page.request.get(url + "/api/runner-status").json()
        assert status["_completed"][f"skill:prepare:{task}"]["run_id"] == admitted["run_id"]
        assert status["_completed"][f"skill:prepare:{task}"]["persisted"] is True
        assert any("/api/runner-status" in item for item in requests)
        assert any(f"/api/tasks/{task}" in item for item in requests)
        page.screenshot(path=str(folder / f"{stem}-terminal.png"), full_page=True)
        assert page.locator("body").bounding_box()["width"] > 0
    finally:
        provider.release.set()
        page.request.delete(url + f"/api/tasks/{task}")


def test_todo_retained_output_is_not_a_new_success(page, skill_server):
    state = skill_server
    state["provider"] = SyntheticProvider()
    provider = state["provider"]
    provider.fail = True
    url = state["url"]
    task = page.request.post(url + "/api/tasks", data={
        "title": "Synthetic failed rerun", "source_type": "manual", "status": "active",
        "parse_status": "parsed", "action_type": "prepare", "skill_output": "Historical preparation",
    }).json()["task"]["id"]
    assert page.request.put(url + f"/api/tasks/{task}", data={"skill_output": "Historical preparation"}).ok
    try:
        page.goto(url + "/todo")
        page.wait_for_function("typeof redoSkill === 'function'")
        page.evaluate("(id) => redoSkill(id, 'prepare')", task)
        assert provider.entered.wait(5)
        page.wait_for_timeout(2400)
        assert "AI draft generated" not in page.locator("#toast-container").inner_text()
        assert page.request.get(url + f"/api/tasks/{task}").json()["task"]["skill_output"] == "Historical preparation"
        provider.release.set()
        expect(page.locator("#toast-container")).to_contain_text("Draft unchanged", timeout=8000)
        assert "AI draft generated" not in page.locator("#toast-container").inner_text()
        assert page.request.get(url + f"/api/tasks/{task}").json()["task"]["skill_output"] == "Historical preparation"
        folder = Path("temp") / "skill-migration"
        folder.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(folder / "todo-failed-rerun.png"), full_page=True)
    finally:
        provider.release.set()
        page.request.delete(url + f"/api/tasks/{task}")
