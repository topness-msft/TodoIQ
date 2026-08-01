"""Open-in-Cowork first-party web deep-link gates."""

import os

from playwright.sync_api import Page, expect


CONV_ID = "tenant:user:cw-testid"
EXPECTED_URL = (
    "https://m365.cloud.microsoft/agents/cowork#/task/"
    "tenant%3Auser%3Acw-testid"
)
SCREENSHOTS_DIR = os.path.join("temp", "cowork-deeplink")


def _seed_task(page: Page, base_url: str) -> int:
    response = page.request.post(
        f"{base_url}/api/tasks",
        data={
            "title": "Open in Cowork gate",
            "description": "Deep-link visual test",
            "action_type": "follow-up",
        },
    )
    assert response.ok
    return response.json()["task"]["id"]


def _delete_task(page: Page, base_url: str, task_id: int) -> None:
    page.request.delete(f"{base_url}/api/tasks/{task_id}")


def _assert_link(page: Page) -> None:
    link = page.get_by_role("link", name="Open in Cowork")
    expect(link).to_be_visible()
    expect(link).to_have_attribute("href", EXPECTED_URL)
    expect(link).to_have_attribute("target", "_blank")
    expect(link).to_have_attribute("rel", "noopener noreferrer")
    box = link.bounding_box()
    assert box and box["width"] > 0 and box["height"] > 0


class TestCoworkDeepLink:
    def test_dashboard_link_visibility_encoding_and_themes(
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
                _cwActions[{task_id}] = {{
                    id: {task_id}, task_id: {task_id}, state: 'ready',
                    finding: 'Found', draft: 'Draft',
                    destination_kind: 'none', conversation_id: '{CONV_ID}'
                }};
                selectedTaskId = {task_id};
                renderDetailPane(tasks.find(task => task.id === {task_id}));
                """
            )
            _assert_link(page)
            page.screenshot(
                path=os.path.join(SCREENSHOTS_DIR, "dashboard-light.png"),
                full_page=True,
            )
            page.evaluate(
                "document.documentElement.setAttribute('data-theme', 'dark')"
            )
            page.screenshot(
                path=os.path.join(SCREENSHOTS_DIR, "dashboard-dark.png"),
                full_page=True,
            )

            page.evaluate(
                f"_cwActions[{task_id}].conversation_id=''; "
                f"renderDetailPane(tasks.find(task => task.id === {task_id}));"
            )
            expect(page.get_by_role("link", name="Open in Cowork")).to_have_count(0)
        finally:
            _delete_task(page, base_url, task_id)

    def test_todoiq_link_visibility_encoding_and_themes(self, page: Page, base_url):
        task_id = _seed_task(page, base_url)
        try:
            page.goto(base_url + "/todo")
            page.wait_for_function(
                f"Boolean(tasks.find(task => task.id === {task_id}))"
            )
            page.evaluate(
                f"""
                const task = tasks.find(item => item.id === {task_id});
                Object.assign(task, {{
                    cw_loaded: true, cw_state: 'ready', cw_seen_at: 'seen',
                    cw_finding: 'Found', cw_draft: 'Draft',
                    cw_dest_kind: 'none', cw_conversation_id: '{CONV_ID}'
                }});
                selectTask({task_id});
                """
            )
            _assert_link(page)
            page.screenshot(
                path=os.path.join(SCREENSHOTS_DIR, "todo-light.png"),
                full_page=True,
            )
            page.evaluate("document.body.classList.add('dark')")
            page.screenshot(
                path=os.path.join(SCREENSHOTS_DIR, "todo-dark.png"),
                full_page=True,
            )

            page.evaluate(
                f"tasks.find(item => item.id === {task_id}).cw_conversation_id=''; "
                f"selectTask({task_id});"
            )
            expect(page.get_by_role("link", name="Open in Cowork")).to_have_count(0)
        finally:
            _delete_task(page, base_url, task_id)
