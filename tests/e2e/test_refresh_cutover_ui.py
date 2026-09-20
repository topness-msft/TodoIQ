"""Actual routes/HTTP/direct cores; synthetic provider answers, no M365 access."""

import asyncio
import json
from pathlib import Path
import re
import threading
from unittest.mock import Mock

import pytest
from PIL import Image, ImageChops
from playwright.sync_api import expect
import tornado.httpserver
import tornado.ioloop
import tornado.netutil

from src import app, db, models
from src.handlers import workiq_api
from src.services import claude_runner, parsing, refresh, workiq_runtime
from tests.test_refresh_workflow import Runtime


@pytest.fixture
def tornado_server(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "visual.db")
    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.delenv("RIVETER_DEMO_MODE", raising=False)
    conn = db.get_connection()
    db.init_db(conn)
    conn.close()
    forbidden = Mock(side_effect=AssertionError("Live provider or CLI forbidden"))
    monkeypatch.setattr(workiq_runtime, "get_runtime", forbidden)
    monkeypatch.setattr(claude_runner, "run_copilot", forbidden)
    fake_status = Mock()
    fake_status.snapshot.return_value = {
        "state": "ready", "authenticated": True, "error": None,
        "queue_depth": 0, "active_job_id": None,
    }
    fake_setup = Mock()
    fake_setup.inspect.return_value = {"state": "mcp_unavailable"}
    monkeypatch.setattr(workiq_api, "get_runtime", lambda: fake_status)
    monkeypatch.setattr(workiq_api, "get_setup", lambda: fake_setup)
    runtime = Runtime()
    parser = parsing.ParseService(runtime_provider=lambda: runtime)
    sync = refresh.RefreshService(runtime_provider=lambda: runtime)
    monkeypatch.setattr(parsing, "_service", parser)
    monkeypatch.setattr(refresh, "_service", sync)
    monkeypatch.setattr(parsing, "_completion", None)
    monkeypatch.setattr(refresh, "_completion", None)
    entered, release, ready = threading.Event(), threading.Event(), threading.Event()
    def pause(operation, payload):
        if operation == "ask" and ("mode" in payload or payload.get("batch") == "direct"):
            entered.set()
            assert release.wait(45), "Synthetic provider gate was not released"
            if payload.get("batch") == "direct" and state.get("refresh_error"):
                raise workiq_runtime.AuthRequiredError("PRIVATE provider context")
    runtime.before = pause
    state = {"runtime": runtime, "parser": parser, "sync": sync,
             "entered": entered, "release": release}
    def serve():
        asyncio.set_event_loop(asyncio.new_event_loop())
        loop = tornado.ioloop.IOLoop.current()
        server = tornado.httpserver.HTTPServer(app.make_app())
        sockets = tornado.netutil.bind_sockets(0, "127.0.0.1")
        server.add_sockets(sockets)
        state.update(loop=loop, server=server, url=f"http://127.0.0.1:{sockets[0].getsockname()[1]}")
        ready.set()
        loop.start()
        loop.close(all_fds=True)
    thread = threading.Thread(target=serve, name="stage3-visual-server", daemon=True)
    thread.start()
    assert ready.wait(10)
    try:
        yield state
    finally:
        release.set()
        parser.join(10)
        sync.join(10)
        async def stop():
            state["server"].stop()
            await state["server"].close_all_connections()
            state["loop"].stop()
        state["loop"].add_callback(stop)
        thread.join(10)
        assert not thread.is_alive()
        forbidden.assert_not_called()


@pytest.fixture(scope="session")
def base_url():
    # pytest-base-url requires a session-scoped value. Tests use the isolated
    # per-case server's absolute URL instead of the shared E2E server.
    return None


def overflow(page):
    return page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")


def capture(page, element, stem, baseline, route):
    expect(element).to_be_visible()
    # Playwright's stability wait never settles a rotating button. Native
    # scrolling must not turn a running capture into a later terminal capture.
    element.evaluate("el => el.scrollIntoView({block:'center', inline:'nearest', behavior:'instant'})")
    box = element.bounding_box()
    viewport = page.viewport_size
    assert box and box["width"] > 0 and box["height"] > 0
    assert box["x"] < viewport["width"] and box["x"] + box["width"] > 0
    assert box["y"] < viewport["height"] and box["y"] + box["height"] > 0
    current = overflow(page)
    assert current <= baseline
    if route == "/":
        assert current == 0
    directory = Path("temp") / "stage3-visual"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (stem + ".png")
    page.screenshot(path=str(path), full_page=True, animations="allow")
    assert path.stat().st_size > 1000
    (directory / (stem + ".json")).write_text(json.dumps({
        "synthetic": True, "viewport": viewport, "bounds": box,
        "baseline_overflow": baseline, "state_overflow": current,
    }), encoding="utf-8")
    return path


@pytest.mark.parametrize("route", ["/", "/todo"])
@pytest.mark.parametrize("viewport", [{"width": 1440, "height": 900}, {"width": 375, "height": 720}],
                         ids=["desktop", "mobile"])
@pytest.mark.parametrize("scenario", ["parse-progress", "parse-error", "refresh-success", "refresh-error"])
def test_direct_cutover_rendered_states(page, tornado_server, route, viewport, scenario):
    base_url = tornado_server["url"]
    page.set_viewport_size(viewport)
    assert page.request.get(base_url + "/api/stats").status == 200
    page.route("**/api/tasks/*/cowork**", lambda request: request.fulfill(
        status=200, content_type="application/json", body='{"action":null}'))
    control = page.request.post(base_url + "/api/tasks", data={
        "title": "Synthetic baseline control", "parse_status": "parsed", "key_people": "[]",
    }).json()["task"]
    page.goto(base_url + route)
    page.wait_for_function("tasks.some(t => t.id === " + str(control["id"]) + ")")
    page.evaluate(f"selectTask({control['id']})")
    baseline = overflow(page)
    stem = f"{'dashboard' if route == '/' else 'todo'}-{viewport['width']}-{scenario}"

    if scenario in {"refresh-success", "refresh-error"}:
        runtime = tornado_server["runtime"]
        pause = runtime.before
        runtime.before = lambda operation, payload: None
        try:
            previous = page.request.post(base_url + "/api/sync-status", data={}).json()
            assert previous["ok"]
            tornado_server["sync"].join(10)
            previous_completion = tornado_server["sync"].completion()
            assert previous_completion["state"] == "succeeded"
            assert previous_completion["run_id"] == previous["run_id"]
        finally:
            runtime.before = pause
        previous_marker = page.request.get(base_url + "/api/sync-status").json()["last_sync"]
        assert type(previous_marker["id"]) is int and previous_marker["id"] > 0
        assert json.loads(previous_marker["result_summary"])["run_id"] == previous["run_id"]
        fails = scenario == "refresh-error"
        tornado_server["refresh_error"] = fails
        page.reload()
        if route == "/":
            page.wait_for_function("(stamp) => lastSyncTime === stamp", arg=previous_marker["synced_at"])
            expect(page.locator("#sync-status-text")).to_be_visible()
            expect(page.locator("#sync-status-text")).not_to_be_empty()
        button = page.locator("#sync-btn")
        expect(button).to_be_visible()
        button.click()
        assert tornado_server["entered"].wait(5)
        run_id = tornado_server["sync"].status()["run_id"]
        assert run_id != previous["run_id"]
        spinner = page.locator("#sync-icon") if route == "/" else button
        def assert_running():
            assert not tornado_server["release"].is_set()
            assert tornado_server["sync"].status()["run_id"] == run_id
            assert page.request.get(base_url + "/api/sync-status").json()["last_sync"] == previous_marker
            runner = page.request.get(base_url + "/api/runner-status").json()
            assert runner["_runs"]["sync"]["run_id"] == run_id
            assert runner["_completed"]["sync"] == previous_completion
            if route == "/":
                assert page.evaluate("lastSyncTime") == previous_marker["synced_at"]
            expect(button).to_have_class(re.compile(r"\bsyncing\b"))
            expect(button).to_have_attribute("aria-busy", "true")
            expect(button).to_be_disabled()
            expect(spinner).to_be_visible()
            assert spinner.evaluate("el => getComputedStyle(el).animationName") != "none"
            expect(page.get_by_text("Sync complete", exact=True)).to_have_count(0)
        assert_running()
        page.wait_for_timeout(5500)
        assert_running()
        first_transform = spinner.evaluate("el => getComputedStyle(el).transform")
        page.wait_for_timeout(200)
        assert spinner.evaluate("el => getComputedStyle(el).transform") != first_transform
        running_image = capture(page, button, stem + "-running", baseline, route)
        assert_running()
        tornado_server["release"].set()
        tornado_server["sync"].join(10)
        completed = tornado_server["sync"].completion()
        assert completed["run_id"] == run_id
        assert completed["state"] == ("blocked" if fails else "succeeded")
        runner = page.request.get(base_url + "/api/runner-status").json()
        assert "sync" not in runner["_runs"]
        assert runner["_completed"]["sync"] == completed
        marker = models.get_last_sync("full_scan")
        if fails:
            assert marker == previous_marker
            feedback = page.locator("#sync-feedback") if route == "/" else page.locator("#toast-container .toast").last
            expect(feedback).to_contain_text("Refresh could not complete discovery.", timeout=15000)
            expect(feedback).to_contain_text("Retry the refresh.")
            expect(feedback).not_to_contain_text("PRIVATE")
            expect(page.get_by_text("Sync complete", exact=True)).to_have_count(0)
        else:
            assert type(marker["id"]) is int and marker["id"] > previous_marker["id"]
            assert json.loads(marker["result_summary"])["run_id"] == run_id
            feedback = page.locator("#sync-status-text") if route == "/" else page.get_by_text("Sync complete", exact=True)
            expect(feedback).to_be_visible(timeout=15000)
            expect(feedback).not_to_be_empty()
        expect(button).not_to_have_class(re.compile(r"\bsyncing\b"), timeout=15000)
        expect(button).to_have_attribute("aria-busy", "false")
        expect(button).to_be_enabled()
        assert page.request.get(base_url + "/api/sync-status").json()["last_sync"] == marker
        if route == "/":
            page.wait_for_function("(stamp) => lastSyncTime === stamp", arg=marker["synced_at"])
            expect(page.locator("#sync-status-text")).to_be_visible()
            expect(page.locator("#sync-status-text")).not_to_be_empty()
            if not fails:
                expect(page.locator("#sync-feedback")).to_be_hidden()
        terminal_image = capture(page, feedback, stem + ("-error" if fails else "-complete"), baseline, route)
        assert tornado_server["sync"].status() is None
        assert button.get_attribute("aria-busy") == "false"
        with Image.open(running_image) as running, Image.open(terminal_image) as terminal:
            assert running.size == terminal.size
            delta = ImageChops.difference(running.convert("RGB"), terminal.convert("RGB"))
            changed_pixels = sum(pixel != (0, 0, 0) for pixel in delta.getdata())
        assert changed_pixels > 100, "Running and terminal evidence must be visibly different"
        (terminal_image.parent / (stem + "-pixel-diff.json")).write_text(json.dumps({
            "run_id": run_id, "terminal_state": completed["state"], "changed_pixels": changed_pixels,
            "previous_run_id": previous["run_id"], "previous_marker_id": previous_marker["id"],
            "terminal_marker_id": marker["id"],
        }), encoding="utf-8")
        return

    if scenario == "parse-error":
        tornado_server["runtime"].parse_answer = "{}"
    created = page.request.post(base_url + "/api/tasks", data={"raw_input": "Review synthetic release checklist"})
    assert created.status == 201 and created.json()["task"]["parse_status"] == "queued"
    task_id = created.json()["task"]["id"]
    assert tornado_server["entered"].wait(5)
    assert models.get_task(task_id)["parse_status"] == "parsing"
    # The alternate UI has no parse polling; reload its real HTTP state rather
    # than manufacture DOM data or silently broaden this stage into a UI rewrite.
    page.reload()
    page.wait_for_function("tasks.some(t => t.id === " + str(task_id) + ")")
    page.evaluate(f"selectTask({task_id})")
    progress = page.locator(".cw-progress").first if route == "/" else page.locator(".parse-badge.parsing")
    capture(page, progress, stem + "-running", baseline, route)
    tornado_server["release"].set()
    tornado_server["parser"].join(10)
    assert models.get_task(task_id)["parse_status"] == ("error" if scenario == "parse-error" else "parsed")
    page.reload()
    page.wait_for_function("tasks.some(t => t.id === " + str(task_id) + ")")
    page.evaluate(f"selectTask({task_id})")
    if scenario == "parse-error":
        error = page.locator(".parse-error-box") if route == "/" else page.locator(".parse-badge.error")
        expect(error).to_contain_text("Retry")
        capture(page, error, stem + "-terminal", baseline, route)
    else:
        expect(page.locator(".cw-status-running" if route == "/" else ".parse-badge.parsing")).to_have_count(0)
        capture(page, page.locator(f"#title-display-{task_id}" if route == "/" else ".detail-title"),
                stem + "-terminal", baseline, route)
