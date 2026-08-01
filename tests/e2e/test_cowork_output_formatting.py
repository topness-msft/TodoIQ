"""Visual and behavioral gates for formatted Cowork output."""

import json
import os

from playwright.sync_api import Page, expect


SCREENSHOTS_DIR = os.path.join("temp", "cowork-output")
LONG_FINDING = """I checked [Alice](context:person?name=Alice&email=alice%40example.com).

## Current state

- The first dependency is complete.
- The second dependency still needs review.
- The third dependency has a decision pending.

For details, see [the roadmap](https://example.com/roadmap)."""
DRAFT = """**Subject:** Project update

Hi Alice,

- First item
- Second item"""
CONVERSATION_ID = "tenant:user:cw-diagnostic-123"


def _seed_task(page: Page, base_url: str) -> int:
    response = page.request.post(
        f"{base_url}/api/tasks",
        data={
            "title": "Formatted Cowork output",
            "description": "Output formatting visual gate",
            "action_type": "follow-up",
        },
    )
    assert response.ok
    return response.json()["task"]["id"]


def _delete_task(page: Page, base_url: str, task_id: int) -> None:
    page.request.delete(f"{base_url}/api/tasks/{task_id}")


def _assert_formatted_card(page: Page, task_id: int) -> None:
    finding = page.locator(f"#cw-finding-{task_id}")
    expect(finding).to_be_visible()
    expect(finding.locator("h2")).to_have_text("Current state")
    expect(finding.locator("li")).to_have_count(3)
    assert "context:person" not in finding.inner_text()

    draft = page.locator(".cw-draft.cw-markdown")
    expect(draft.locator("strong")).to_have_text("Subject:")
    expect(draft.locator("li")).to_have_count(2)

    toggle = page.locator(f"#cw-finding-toggle-{task_id}")
    expect(toggle).to_be_visible()
    expect(toggle).to_have_text("Show more")
    clamped = finding.evaluate(
        "el => ({client: el.clientHeight, scroll: el.scrollHeight, clamp: getComputedStyle(el).webkitLineClamp})"
    )
    assert clamped["clamp"] == "4"
    assert clamped["scroll"] > clamped["client"]

    expect(page.get_by_text("No delivery destination selected")).to_be_visible()
    debug = page.locator(".cw-debug-id")
    expect(debug).to_have_attribute("title", CONVERSATION_ID)
    assert CONVERSATION_ID not in page.locator(".cw-dest").inner_text()


def _assert_expands(page: Page, task_id: int) -> None:
    toggle = page.locator(f"#cw-finding-toggle-{task_id}")
    finding = page.locator(f"#cw-finding-{task_id}")
    toggle.click()
    expect(toggle).to_have_text("Show less")
    assert finding.evaluate("el => el.scrollHeight === el.clientHeight")


class TestOutputFormatting:
    def test_dashboard_formats_and_clamps_cowork_output(self, page: Page, base_url):
        task_id = _seed_task(page, base_url)
        os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
        try:
            page.goto(base_url + "/")
            page.wait_for_function(
                f"Boolean(tasks.find(task => task.id === {task_id}))"
            )
            page.evaluate(
                f"""
                _cwActions[{task_id}] = {json.dumps({
                    "id": task_id,
                    "task_id": task_id,
                    "state": "ready",
                    "finding": LONG_FINDING,
                    "draft": DRAFT,
                    "destination_kind": "none",
                    "conversation_id": CONVERSATION_ID,
                })};
                selectTask({task_id});
                """
            )
            _assert_formatted_card(page, task_id)
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
            _assert_expands(page, task_id)
        finally:
            _delete_task(page, base_url, task_id)

    def test_todoiq_formats_and_clamps_cowork_output(self, page: Page, base_url):
        task_id = _seed_task(page, base_url)
        try:
            page.goto(base_url + "/todo")
            page.wait_for_function(
                f"Boolean(tasks.find(task => task.id === {task_id}))"
            )
            page.evaluate(
                f"""
                const task = tasks.find(item => item.id === {task_id});
                Object.assign(task, {json.dumps({
                    "cw_loaded": True,
                    "cw_state": "ready",
                    "cw_finding": LONG_FINDING,
                    "cw_draft": DRAFT,
                    "cw_dest_kind": "none",
                    "cw_conversation_id": CONVERSATION_ID,
                })});
                selectTask({task_id});
                """
            )
            _assert_formatted_card(page, task_id)
            page.screenshot(
                path=os.path.join(SCREENSHOTS_DIR, "todo-light.png"),
                full_page=True,
            )
            page.evaluate("document.body.classList.add('dark')")
            page.screenshot(
                path=os.path.join(SCREENSHOTS_DIR, "todo-dark.png"),
                full_page=True,
            )
            _assert_expands(page, task_id)
        finally:
            _delete_task(page, base_url, task_id)
