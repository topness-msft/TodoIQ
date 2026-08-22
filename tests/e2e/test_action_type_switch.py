import json
import os

from playwright.sync_api import Page, expect


SCREENSHOTS_DIR = os.path.join("temp", "action-type-switch")


def test_action_type_switch_hides_stale_preview_without_starting_cowork(
    page: Page, base_url
):
    created = page.request.post(
        base_url + "/api/tasks",
        data={
            "title": "Freada 1:1",
            "action_type": "prepare",
            "parse_status": "parsed",
            "key_people": json.dumps([{
                "name": "Freada Sylvester",
                "email": "freadas@microsoft.com",
            }]),
        },
    )
    seeded_task = created.json()["task"]
    task_id = seeded_task["id"]
    writes = []

    def record_write(route):
        writes.append((route.request.method, route.request.url))
        route.continue_()

    page.route(f"**/api/tasks/{task_id}/cowork", record_write)
    page.route(
        f"**/api/tasks/{task_id}/refresh",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"task": {
                **seeded_task,
                "action_type": "schedule-meeting",
                "cowork_revision": 1,
                "parse_status": "parsed",
            }}),
        ),
    )
    try:
        page.goto(base_url + "/")
        page.wait_for_function(
            f"typeof tasks !== 'undefined' && tasks.some(t => t.id === {task_id})"
        )
        page.evaluate(
            """taskId => {
                selectedTaskId = taskId;
                _cwActions[taskId] = {
                    id: 154,
                    task_id: taskId,
                    action_type: 'prepare',
                    cowork_revision: 0,
                    state: 'ready',
                    finding: 'Old preparation',
                    draft: 'Old preparation draft'
                };
                renderDetailPane(tasks.find(t => t.id === taskId));
            }""",
            task_id,
        )
        expect(page.get_by_text("Old preparation draft")).to_be_visible()

        page.locator(".action-type-select").select_option("schedule-meeting")

        expect(page.get_by_role("button", name="Preview with WorkIQ")).to_be_visible()
        expect(page.get_by_text("Old preparation draft")).to_have_count(0)
        os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
        page.screenshot(
            path=os.path.join(SCREENSHOTS_DIR, "fresh-schedule-preview-light.png"),
            full_page=True,
        )
        page.evaluate(
            "document.documentElement.setAttribute('data-theme', 'dark')"
        )
        page.screenshot(
            path=os.path.join(SCREENSHOTS_DIR, "fresh-schedule-preview-dark.png"),
            full_page=True,
        )
        page.wait_for_timeout(300)
        assert not any(
            method == "POST" and url.endswith(f"/api/tasks/{task_id}/cowork")
            for method, url in writes
        )
    finally:
        page.request.delete(f"{base_url}/api/tasks/{task_id}")
