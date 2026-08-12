"""E2E gates for Cowork list status and detail-card ordering."""

import json
import os

from playwright.sync_api import Page, expect


SCREENSHOTS_DIR = os.path.join("temp", "cowork-indicators")

def _seed_task(page: Page, base_url: str) -> int:
    response = page.request.post(
        f"{base_url}/api/tasks",
        data={
            "title": "Cowork indicator gate",
            "description": "Indicator test task",
            "action_type": "follow-up",
            "skill_output": "Existing enrichment",
        },
    )
    assert response.ok
    return response.json()["task"]["id"]


def _delete_task(page: Page, base_url: str, task_id: int) -> None:
    page.request.delete(f"{base_url}/api/tasks/{task_id}")


class TestCoworkIndicators:
    def test_dashboard_uses_bare_cowork_icon_for_completed_enrichment(
        self, page: Page, base_url
    ):
        task_id = _seed_task(page, base_url)
        os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
        try:
            page.goto(base_url + "/")
            page.wait_for_function(
                f"Boolean(tasks.find(task => task.id === {task_id}))"
            )
            page.evaluate(
                f"""
                const task = tasks.find(item => item.id === {task_id});
                task.skill_output = 'Existing enrichment';
                // A parsed task no longer renders a parse icon (the green tick
                // was on the majority of rows and carried no information), and
                // this test aligns the Cowork indicator against that icon. Use
                // a state that still shows one, so the alignment assertion
                // keeps testing alignment rather than the removal.
                task.parse_status = 'unparsed';
                task.cw_state = 'previewing';
                task.cw_seen_at = null;
                renderTaskList();
                """
            )
            row = page.locator(f'.task-row[data-id="{task_id}"]')
            expect(row.locator(".cw-status-running")).to_be_visible()
            expect(
                row.locator(
                    '.cw-status-running img[src="/static/img/coworker.svg"]'
                )
            ).to_have_count(1)
            assert row.locator(".cw-status-running").evaluate(
                "node => getComputedStyle(node).animationName"
            ) != "none"
            expect(row.locator(".enriched-icon")).to_have_count(0)

            page.evaluate(
                f"""
                const task = tasks.find(item => item.id === {task_id});
                task.cw_state = 'ready';
                task.cw_seen_at = null;
                renderTaskList();
                """
            )
            expect(row.locator(".cw-status-unread")).to_be_visible()
            expect(
                row.locator(
                    '.cw-status-unread img[src="/static/img/coworker.svg"]'
                )
            ).to_have_count(1)
            expect(row.locator(".enriched-icon")).to_have_count(0)
            assert row.locator(".cw-status-unread").evaluate(
                "node => getComputedStyle(node).boxShadow"
            ) == "none"
            box = row.locator(".cw-status-unread").bounding_box()
            assert box and box["width"] > 0 and box["height"] > 0
            parsed_box = row.locator(".parse-icon").bounding_box()
            assert parsed_box
            assert abs(
                (box["y"] + box["height"] / 2)
                - (parsed_box["y"] + parsed_box["height"] / 2)
            ) <= 1
            page.screenshot(
                path=os.path.join(SCREENSHOTS_DIR, "dashboard-unread-light.png"),
                full_page=True,
            )
            page.evaluate(
                "document.documentElement.setAttribute('data-theme', 'dark')"
            )
            page.screenshot(
                path=os.path.join(SCREENSHOTS_DIR, "dashboard-unread-dark.png"),
                full_page=True,
            )

            page.evaluate(
                f"""
                const task = tasks.find(item => item.id === {task_id});
                task.cw_seen_at = '2026-07-31T19:00:00Z';
                renderTaskList();
                """
            )
            expect(row.locator(".cw-status-unread")).to_have_count(0)
            expect(row.locator(".enriched-icon")).to_have_count(0)
            expect(row.locator(".cw-status-complete")).to_be_visible()
            expect(
                row.locator(
                    '.cw-status-complete img[src="/static/img/coworker.svg"]'
                )
            ).to_have_count(1)
        finally:
            _delete_task(page, base_url, task_id)

    def test_todoiq_running_and_unread_indicators(self, page: Page, base_url):
        task_id = _seed_task(page, base_url)
        try:
            page.goto(base_url + "/todo")
            page.wait_for_function(
                f"Boolean(tasks.find(task => task.id === {task_id}))"
            )
            page.evaluate(
                f"""
                const task = tasks.find(item => item.id === {task_id});
                task.cw_state = 'previewing';
                task.cw_seen_at = null;
                renderTasks();
                """
            )
            row = page.locator(f'.task-row[title="Task #{task_id}"]')
            expect(row.locator(".cw-status-running")).to_be_visible()
            expect(
                row.locator(
                    '.cw-status-running img[src="/static/img/coworker.svg"]'
                )
            ).to_have_count(1)
            assert row.locator(".cw-status-running").evaluate(
                "node => getComputedStyle(node).animationName"
            ) != "none"

            page.evaluate(
                f"""
                const task = tasks.find(item => item.id === {task_id});
                task.cw_state = 'ready';
                renderTasks();
                """
            )
            expect(row.locator(".cw-status-unread")).to_be_visible()
            expect(
                row.locator(
                    '.cw-status-unread img[src="/static/img/coworker.svg"]'
                )
            ).to_have_count(1)
            box = row.locator(".cw-status-unread").bounding_box()
            assert box and box["width"] > 0 and box["height"] > 0
            page.screenshot(
                path=os.path.join(SCREENSHOTS_DIR, "todo-unread-light.png"),
                full_page=True,
            )
            page.evaluate("document.body.classList.add('dark')")
            page.screenshot(
                path=os.path.join(SCREENSHOTS_DIR, "todo-unread-dark.png"),
                full_page=True,
            )
        finally:
            _delete_task(page, base_url, task_id)

    def test_dashboard_open_marks_ready_action_seen(self, page: Page, base_url):
        task_id = _seed_task(page, base_url)
        action = {
            "id": 123,
            "task_id": task_id,
            "state": "ready",
            "finding": "Done",
            "draft": "Draft",
            "seen_at": "2026-07-31T19:00:00Z",
            "destination_kind": "none",
        }
        requested = []
        page.route(
            f"**/api/tasks/{task_id}/cowork?mark_seen=1",
            lambda route: (
                requested.append(route.request.url),
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"action": action}),
                ),
            )[-1],
        )
        try:
            page.goto(base_url + "/")
            page.wait_for_function(
                f"Boolean(tasks.find(task => task.id === {task_id}))"
            )
            page.evaluate(
                f"""
                const task = tasks.find(item => item.id === {task_id});
                task.cw_state = 'ready';
                task.cw_seen_at = null;
                renderTaskList();
                selectTask({task_id});
                """
            )
            page.wait_for_function(
                f"(tasks.find(task => task.id === {task_id}) || {{}}).cw_seen_at"
            )
            assert requested
            row = page.locator(f'.task-row[data-id="{task_id}"]')
            expect(row.locator(".cw-status-unread")).to_have_count(0)
        finally:
            _delete_task(page, base_url, task_id)

    def test_dashboard_cowork_card_is_beside_evidence(self, page: Page, base_url):
        task_id = _seed_task(page, base_url)
        try:
            page.goto(base_url + "/")
            page.wait_for_function(
                f"Boolean(tasks.find(task => task.id === {task_id}))"
            )
            page.evaluate(
                f"""
                _cwActions[{task_id}] = {{
                    id: {task_id}, task_id: {task_id}, state: 'ready',
                    finding: 'Current state', draft: 'Draft', destination_kind: 'none'
                }};
                selectTask({task_id});
                """
            )
            expect(page.locator(".detail-workspace .cw-card")).to_be_visible()
            cowork_x = page.locator(".cw-card").bounding_box()["x"]
            notes_x = page.get_by_text("Notes", exact=True).bounding_box()["x"]
            assert cowork_x > notes_x
        finally:
            _delete_task(page, base_url, task_id)
