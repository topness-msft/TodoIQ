import json
import os

from playwright.sync_api import Page, expect


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
TEMP_DIR = os.path.join(PROJECT_ROOT, "temp")
os.makedirs(TEMP_DIR, exist_ok=True)


def _render(page: Page, base_url, action):
    created = page.request.post(
        base_url + "/api/tasks",
        data={"title": "Prepare the FinOps follow-up"},
    )
    task_id = created.json()["task"]["id"]
    if action is not None:
        action["task_id"] = task_id
    page.goto(base_url + "/")
    page.wait_for_function(
        f"typeof tasks !== 'undefined' && tasks.some(t => t.id === {task_id})"
    )
    page.evaluate(
        """({taskId, action}) => {
            const task = tasks.find(t => t.id === taskId);
            task.action_type = 'follow-up';
            task.coaching_text = 'Check the current position and draft the follow-up.';
            selectedTaskId = taskId;
            _cwActions[taskId] = action;
            renderDetailPane(task);
        }""",
        {"taskId": task_id, "action": action},
    )
    return task_id


def test_mode_selector_drives_start_request(page: Page, base_url):
    task_id = _render(page, base_url, None)
    posted = {}

    def start_route(route):
        posted.update(json.loads(route.request.post_data))
        route.fulfill(
            status=202,
            content_type="application/json",
            body=json.dumps(
                {
                    "action": {
                        "task_id": task_id,
                        "state": "previewing",
                        "interaction_mode": posted["interaction_mode"],
                    }
                }
            ),
        )

    page.route(f"**/api/tasks/{task_id}/cowork", start_route)
    no_interaction = page.get_by_test_id("session-mode-no-interaction")
    expect(no_interaction).to_be_visible()
    no_interaction.click()
    expect(no_interaction).to_have_attribute("aria-pressed", "true")
    page.get_by_role("button", name="Preview with Cowork").click()
    page.wait_for_function("() => window.__unused !== true")
    assert posted["interaction_mode"] == "no_interaction"


def test_running_card_uses_real_trace_and_locks_mode(page: Page, base_url):
    _render(
        page,
        base_url,
        {
            "state": "previewing",
            "interaction_mode": "no_interaction",
            "created_at": "2026-08-10T12:00:00Z",
            "progress": ["Drafting the response"],
            "tool_trace": json.dumps(
                [
                    {
                        "name": "m365_teams-GetMessages",
                        "ok": True,
                        "duration_seconds": 3,
                    },
                    {
                        "name": "outlook-GetCalendarView",
                        "ok": True,
                        "duration_seconds": 8,
                    },
                ]
            ),
        },
    )

    expect(page.get_by_test_id("session-mode-no-interaction")).to_be_disabled()
    expect(page.get_by_test_id("session-timeline")).to_be_visible()
    expect(page.get_by_test_id("session-timeline")).to_contain_text("Get Messages")
    expect(page.get_by_test_id("session-timeline")).to_contain_text(
        "Drafting the response"
    )


def test_ready_no_interaction_matches_completion_state(page: Page, base_url):
    _render(
        page,
        base_url,
        {
            "state": "ready",
            "interaction_mode": "no_interaction",
            "finding": "The current FinOps position is confirmed.",
            "draft": "Hi Mehdi, here is the current position.",
            "cost_credits": 30.2,
            "updated_at": "2026-08-10T12:00:00Z",
            "completed_at": "2026-08-10T12:00:00Z",
            "conversation_id": "t:u:cw-parity",
            "tool_trace": json.dumps(
                [{"name": "m365_teams-GetMessages", "ok": True}]
            ),
        },
    )

    expect(page.get_by_test_id("session-complete")).to_contain_text(
        "Draft completed without interruption"
    )
    expect(page.get_by_test_id("credit-meter")).to_have_text("30.2 credits")
    expect(page.get_by_test_id("cowork-finished")).to_contain_text("Cowork finished")
    expect(page.get_by_test_id("open-in-cowork-link")).to_be_visible()
    expect(page.get_by_role("button", name="Open draft in Outlook")).to_have_count(0)
    page.evaluate("document.documentElement.setAttribute('data-theme', 'light')")
    page.screenshot(
        path=os.path.join(TEMP_DIR, "cowork-mock-b-parity-light.png"),
        full_page=True,
    )
    page.evaluate("document.documentElement.setAttribute('data-theme', 'dark')")
    page.screenshot(
        path=os.path.join(TEMP_DIR, "cowork-mock-b-parity-dark.png"),
        full_page=True,
    )


def test_answered_no_interaction_run_does_not_claim_no_interruption(
    page: Page, base_url
):
    _render(
        page,
        base_url,
        {
            "state": "ready",
            "interaction_mode": "no_interaction",
            "had_interaction": 1,
            "finding": "The tenant was selected after asking.",
            "draft": "Draft text.",
        },
    )
    expect(page.get_by_test_id("session-complete")).to_have_count(0)
