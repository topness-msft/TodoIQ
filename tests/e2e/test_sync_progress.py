"""Sync completion must follow one exact backend run, not a UI timer."""

import json
import re

import pytest
from playwright.sync_api import expect


RUN = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
OLD = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"


def routes(page):
    state = {"phase": "idle", "posts": 0}

    def sync(route):
        if route.request.method == "POST":
            state["posts"] += 1
            state["phase"] = state.get("post_phase", "running")
            payload = {"ok": True, "run_id": RUN, "started_at": "2030-01-01T12:00:00Z"}
        else:
            success = state["phase"] == "succeeded" and state.get("marker_ready", True)
            payload = {
                "sync_running": state["phase"] == "running",
                "last_sync": {
                    "id": 2 if success else 1, "sync_type": "full_scan",
                    "synced_at": "2030-01-01T12:01:00Z" if success else "2020-01-01T12:00:00Z",
                    "result_summary": json.dumps({"run_id": RUN if success else OLD}),
                },
                "suggestion_check_running": False, "auto_sync_enabled": True,
            }
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    def runner(route):
        running = state["phase"] == "running"
        terminal = state["phase"] in {"failed", "succeeded"}
        payload = {
            "_runs": {"sync": {"run_id": RUN}} if running else {},
            "_completed": {"sync": {
                "run_id": RUN if terminal else OLD,
                "state": state["phase"] if terminal else "succeeded",
                "exit_code": 1 if state["phase"] == "failed" else 0,
                "error": "Synthetic backend failure" if state["phase"] == "failed" else None,
            }},
        }
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    page.route("**/api/sync-status", sync)
    page.route("**/api/runner-status", runner)
    return state


def monitor_page(page, base_url):
    page.route("**/sync-monitor-test", lambda route: route.fulfill(
        content_type="text/html", body="<html><body>Sync monitor test</body></html>"
    ))
    page.goto(base_url + "/sync-monitor-test")
    page.add_script_tag(url=base_url + "/static/js/sync-status.js")
    page.evaluate("""() => {
        window.syncEvents = [];
        window.testMonitor = createRiveterSyncMonitor(event => syncEvents.push(event));
    }""")


@pytest.mark.parametrize("path", ["/", "/todo"])
def test_sync_remains_running_past_old_timer_and_shows_real_failure(page, base_url, path):
    task = page.request.post(base_url + "/api/tasks", data={
        "title": "Synthetic sync check", "status": "suggested", "parse_status": "parsed",
    }).json()["task"]["id"]
    state = routes(page)
    try:
        page.goto(base_url + path)
        page.wait_for_function("typeof tasks !== 'undefined' && tasks.length > 0")
        page.locator("#sync-btn").click()
        expect(page.locator("#sync-btn")).to_have_class(re.compile(r"\bsyncing\b"))
        page.wait_for_timeout(5500)
        expect(page.locator("#sync-btn")).to_have_class(re.compile(r"\bsyncing\b"))
        expect(page.locator("#sync-btn")).to_be_disabled()
        state["phase"] = "failed"
        feedback = page.locator("#sync-feedback") if path == "/" else page.locator("#toast-container .toast").last
        expect(feedback).to_contain_text("Synthetic backend failure", timeout=12000)
        expect(feedback).to_be_visible()
        expect(page.locator("#sync-btn")).not_to_have_class(re.compile(r"\bsyncing\b"))
        assert state["posts"] == 1
    finally:
        page.request.delete(base_url + f"/api/tasks/{task}")


def test_fast_completion_requires_matching_marker_and_exact_run(page, base_url):
    state = routes(page)
    state.update(post_phase="succeeded", marker_ready=False)
    monitor_page(page, base_url)
    page.evaluate("() => testMonitor.sync()")
    assert page.evaluate("syncEvents.at(-1).state") == "unconfirmed"
    assert not page.evaluate("syncEvents.some(event => event.state === 'succeeded')")
    state["marker_ready"] = True
    page.evaluate("() => testMonitor.poll()")
    assert page.evaluate("syncEvents.at(-1).state") == "succeeded"
    assert page.evaluate("syncEvents.at(-1).run_id") == RUN


def test_old_completion_never_completes_new_request(page, base_url):
    state = routes(page)
    state["post_phase"] = "missing"
    monitor_page(page, base_url)
    page.evaluate("() => testMonitor.sync()")
    assert page.evaluate("syncEvents.at(-1).state") == "unconfirmed"
    assert not page.evaluate("syncEvents.some(event => event.state === 'succeeded')")
    state["phase"] = "running"
    page.evaluate("() => testMonitor.poll()")
    assert page.evaluate("syncEvents.at(-1).state") == "running"


def test_reload_adopts_current_run_without_posting(page, base_url):
    state = routes(page)
    state["phase"] = "running"
    monitor_page(page, base_url)
    page.evaluate("() => testMonitor.start()")
    assert page.evaluate("syncEvents.at(-1).state") == "running"
    assert page.evaluate("syncEvents.at(-1).run_id") == RUN
    assert state["posts"] == 0
    page.evaluate("() => testMonitor.stop()")


def test_poll_is_single_flight_and_stale_generation_cannot_overwrite_post(page, base_url):
    state = routes(page)
    held = []
    hold = {"enabled": True}

    def intercept(route):
        if route.request.method == "GET" and hold["enabled"]:
            held.append(route)
        else:
            route.fallback()

    for endpoint in ("sync-status", "runner-status"):
        page.route(f"**/api/{endpoint}", intercept)
    monitor_page(page, base_url)
    assert page.evaluate("""() => {
        const first = testMonitor.poll();
        return first === testMonitor.poll();
    }""")
    page.wait_for_timeout(100)
    assert len(held) == 2
    hold["enabled"] = False
    page.evaluate("() => { testMonitor.sync(); }")
    page.wait_for_function("syncEvents.some(event => event.state === 'running')")
    for request in held:
        payload = (
            {"sync_running": False, "last_sync": None}
            if request.request.url.endswith("sync-status")
            else {"_runs": {}, "_completed": {"sync": {
                "run_id": OLD, "state": "failed", "exit_code": 1, "error": "Stale failure"
            }}}
        )
        request.fulfill(content_type="application/json", body=json.dumps(payload))
    page.wait_for_timeout(100)
    assert page.evaluate("syncEvents.at(-1).state") == "running"
    assert not page.evaluate("syncEvents.some(event => event.state === 'failed')")
    assert state["posts"] == 1


def test_network_loss_is_unconfirmed_and_expiry_does_not_cancel_server(page, base_url):
    state = routes(page)
    monitor_page(page, base_url)
    page.evaluate("() => { window.clockValue = Date.now(); Date.now = () => clockValue; }")
    page.evaluate("() => testMonitor.sync()")
    for endpoint in ("sync-status", "runner-status"):
        page.route(f"**/api/{endpoint}", lambda route: route.abort())
    page.evaluate("() => testMonitor.poll()")
    assert page.evaluate("syncEvents.at(-1).state") == "unconfirmed"
    assert "may still be running" in page.evaluate("syncEvents.at(-1).message")
    page.evaluate("clockValue += 600001")
    page.evaluate("() => testMonitor.poll()")
    assert "not been cancelled" in page.evaluate("syncEvents.at(-1).message")
    assert state["posts"] == 1
