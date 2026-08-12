import json
import os

from playwright.sync_api import Page, expect


SCREENSHOTS_DIR = os.path.join("temp", "cowork-handoff")


def test_running_handoff_badge_refreshes_until_completion(page: Page, base_url):
    created = page.request.post(
        f"{base_url}/api/tasks",
        data={"title": "Refresh completed Cowork handoff"},
    )
    task_id = created.json()["task"]["id"]
    calls = {"count": 0}

    def cowork_route(route):
        calls["count"] += 1
        handoff_state = "running" if calls["count"] < 6 else "completed"
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "action": {
                    "id": 1,
                    "task_id": task_id,
                    "state": "ready",
                    "progress": [],
                    "finding": "Research complete.",
                    "draft": "Draft complete.",
                    "intent": "research and draft",
                    "channel": "teams",
                    "conversation_id": "t:u:handoff",
                    "handoff": {
                        "state": handoff_state,
                        "waiting_on_user": False,
                        "last_activity": 1786542600000,
                    },
                }
            }),
        )

    page.route(f"**/api/tasks/{task_id}/cowork*", cowork_route)
    try:
        page.goto(base_url + "/")
        page.evaluate(
            """() => {
                CW_HANDOFF_POLL_MS = 750;
            }"""
        )
        page.wait_for_function(
            f"typeof tasks !== 'undefined' && tasks.some(t => t.id === {task_id})"
        )
        page.evaluate(f"selectTask({task_id})")

        expect(page.locator(".cw-handoff-running")).to_be_visible()
        os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
        page.evaluate(
            "document.querySelector('.detail-actions-bar').style.display = 'none'"
        )
        page.screenshot(
            path=os.path.join(SCREENSHOTS_DIR, "handoff-running.png"),
            full_page=True,
        )
        expect(page.locator(".cw-handoff-done")).to_be_visible(timeout=7000)
        expect(page.locator(".cw-handoff-running")).to_have_count(0)
        page.screenshot(
            path=os.path.join(SCREENSHOTS_DIR, "handoff-completed.png"),
            full_page=True,
        )
        assert calls["count"] >= 6
    finally:
        page.request.delete(f"{base_url}/api/tasks/{task_id}")


def test_todo_handoff_badge_refreshes_until_completion(page: Page, base_url):
    created = page.request.post(
        f"{base_url}/api/tasks",
        data={"title": "Refresh Todo-view Cowork handoff"},
    )
    task_id = created.json()["task"]["id"]
    calls = {"count": 0}

    def cowork_route(route):
        calls["count"] += 1
        handoff_state = "running" if calls["count"] == 1 else "completed"
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "action": {
                    "id": 2,
                    "task_id": task_id,
                    "state": "ready",
                    "progress": [],
                    "finding": "Research complete.",
                    "draft": "Draft complete.",
                    "intent": "research and draft",
                    "channel": "teams",
                    "conversation_id": "t:u:todo-handoff",
                    "handoff": {
                        "state": handoff_state,
                        "waiting_on_user": False,
                        "last_activity": 1786542600000,
                    },
                }
            }),
        )

    page.route(f"**/api/tasks/{task_id}/cowork*", cowork_route)
    try:
        page.goto(base_url + "/todo")
        page.evaluate(
            """() => {
                CW_HANDOFF_POLL_MS = 50;
            }"""
        )
        page.wait_for_function(
            f"typeof tasks !== 'undefined' && tasks.some(t => t.id === {task_id})"
        )
        page.evaluate(f"selectTask({task_id})")

        expect(page.locator(".cw-handoff-running")).to_be_visible()
        expect(page.locator(".cw-handoff-done")).to_be_visible(timeout=3000)
        expect(page.locator(".cw-handoff-running")).to_have_count(0)
        assert calls["count"] >= 2
    finally:
        page.request.delete(f"{base_url}/api/tasks/{task_id}")
