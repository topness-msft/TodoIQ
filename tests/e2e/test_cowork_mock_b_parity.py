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
            task.action_type = (action && action.action_type) || 'follow-up';
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
    expect(no_interaction).to_have_text("Finish without questions")
    expect(no_interaction).to_have_attribute(
        "title", "Cowork makes reasonable decisions and does not pause to ask."
    )
    expect(page.get_by_test_id("session-mode-interaction")).to_have_text(
        "Can ask questions"
    )
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
                    {
                        "name": "Bash",
                        "ok": True,
                        "duration_seconds": 1,
                        "input": "{\"command\":\"python check_calendar_dates.py\"}",
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
    expect(page.get_by_test_id("session-timeline")).to_contain_text(
        "Checking date and time details"
    )
    expect(page.get_by_test_id("session-timeline")).not_to_contain_text("Bash")
    expect(page.get_by_test_id("tool-icon")).to_have_count(3)
    expect(page.locator('[data-testid="tool-icon"] svg')).to_have_count(3)
    expect(page.locator('[data-tool-icon="teams"]')).to_have_count(1)
    expect(page.locator('[data-tool-icon="calendar"]')).to_have_count(1)
    live = page.locator(".cw-timeline-event.is-active > div > span")
    box = live.evaluate(
        "(el) => ({clientHeight: el.clientHeight, scrollHeight: el.scrollHeight})"
    )
    assert box["scrollHeight"] <= box["clientHeight"] + 2


def test_ready_no_interaction_matches_completion_state(page: Page, base_url):
    task_id = _render(
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
    expect(page.get_by_role("button", name="Edit")).to_have_count(0)
    expect(page.get_by_role("link", name="Finish in Cowork")).to_have_count(0)
    expect(page.get_by_role("button", name="Hide")).to_have_count(0)
    expect(page.get_by_role("button", name="Refine")).to_be_visible()
    draft = page.get_by_test_id("cowork-draft-click-edit")
    expect(draft).to_have_attribute("title", "Click to edit draft")
    draft.focus()
    draft.press("Enter")
    editor = page.locator(f"#cw-draft-{task_id}")
    expect(editor).to_be_visible()
    expect(editor).to_be_focused()
    page.get_by_role("button", name="Cancel").click()
    draft = page.get_by_test_id("cowork-draft-click-edit")
    draft.click()
    expect(page.locator(f"#cw-draft-{task_id}")).to_be_visible()
    expect(page.locator('link[href*="style.css?v="]')).to_have_count(1)
    expect(page.locator('script[src*="dashboard.js?v="]')).to_have_count(1)
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


def test_ready_action_opens_exact_confirmation_and_submits_once(
    page: Page, base_url
):
    task_id = _render(
        page,
        base_url,
        {
            "state": "ready",
            "action_type": "follow-up",
            "draft": "Hi Mehdi, the review is complete.",
            "conversation_id": "t:u:cw-send",
            "delivery_channel": "teams",
            "destination_ref": "mehdi@microsoft.com",
            "destination_display": "Mehdi Slaoui Andaloussi",
            "destination_confirmed_at": "2026-08-11T12:00:00Z",
        },
    )
    sent = []
    sent_headers = []

    def execute_route(route):
        sent.append(route.request.post_data)
        sent_headers.append(route.request.headers)
        route.fulfill(
            status=202,
            content_type="application/json",
            body=json.dumps(
                {
                    "action": {
                        "task_id": task_id,
                        "state": "executing",
                        "destination_display": "Mehdi Slaoui Andaloussi",
                    }
                }
            ),
        )

    page.route(f"**/api/tasks/{task_id}/cowork/execute", execute_route)
    page.get_by_role("button", name="Send Teams message").click()
    modal = page.get_by_test_id("execute-confirmation")
    expect(modal).to_be_visible()
    expect(modal).to_contain_text("Mehdi Slaoui Andaloussi")
    expect(modal).to_contain_text("Hi Mehdi, the review is complete.")
    page.screenshot(
        path=os.path.join(TEMP_DIR, "cowork-execute-confirmation-light.png"),
        full_page=True,
    )
    page.evaluate("document.documentElement.setAttribute('data-theme', 'dark')")
    page.screenshot(
        path=os.path.join(TEMP_DIR, "cowork-execute-confirmation-dark.png"),
        full_page=True,
    )
    confirm = page.get_by_test_id("execute-confirm-btn")
    confirm.dblclick()
    expect(page.get_by_test_id("execute-confirmation")).to_have_count(0)
    assert len(sent) == 1
    assert sent_headers[0]["x-riveter-action"] == "confirm"
    snapshot = json.loads(sent[0])["approved_snapshot"]
    assert snapshot["draft"] == "Hi Mehdi, the review is complete."
    assert snapshot["destination_ref"] == "mehdi@microsoft.com"


def test_action_labels_and_terminal_states(page: Page, base_url):
    _render(
        page,
        base_url,
        {
            "state": "ready",
            "action_type": "schedule-meeting",
            "draft": "Create a 30-minute review next week.",
            "conversation_id": "t:u:cw-meeting",
            "destination_ref": "mehdi@microsoft.com",
            "destination_display": "Mehdi Slaoui Andaloussi",
            "destination_confirmed_at": "2026-08-11T12:00:00Z",
        },
    )
    expect(page.get_by_role("button", name="Create meeting")).to_be_visible()

    unconfirmed_task_id = _render(
        page,
        base_url,
        {
            "state": "execute_unconfirmed",
            "draft": "Final approved text.",
            "destination_display": "Mehdi Slaoui Andaloussi",
            "error": "Delivery could not be confirmed.",
        },
    )
    expect(page.get_by_test_id("delivery-unconfirmed")).to_contain_text(
        "Check the destination before retrying"
    )
    assert page.evaluate(
        f"Boolean(_cwPollers[{unconfirmed_task_id}])"
    ) is False
    page.screenshot(
        path=os.path.join(TEMP_DIR, "cowork-delivery-unconfirmed.png"),
        full_page=True,
    )

    executed_task_id = _render(
        page,
        base_url,
        {
            "state": "executed",
            "draft": "Final approved text.",
            "destination_display": "Mehdi Slaoui Andaloussi",
            "delivery_confirmed_at": "2026-08-11T12:00:00Z",
        },
    )
    expect(page.get_by_test_id("delivery-confirmed")).to_contain_text(
        "Delivered to Mehdi Slaoui Andaloussi"
    )
    assert page.evaluate(f"Boolean(_cwPollers[{executed_task_id}])") is False
    page.screenshot(
        path=os.path.join(TEMP_DIR, "cowork-delivery-confirmed.png"),
        full_page=True,
    )


def test_execution_progress_and_approval_states(page: Page, base_url):
    _render(
        page,
        base_url,
        {
            "state": "executing",
            "draft": "Final approved text.",
            "destination_display": "Mehdi Slaoui Andaloussi",
            "progress": ["Sending the approved Teams message"],
            "started_at": "2026-08-11T12:00:00Z",
        },
    )
    expect(page.get_by_text("approved action in progress")).to_be_visible()
    page.screenshot(
        path=os.path.join(TEMP_DIR, "cowork-executing.png"),
        full_page=True,
    )

    _render(
        page,
        base_url,
        {
            "state": "executing",
            "draft": "Final approved text.",
            "destination_display": "Mehdi Slaoui Andaloussi",
            "waiting_on_user": True,
            "interaction_request": {
                "invocation_id": "approval-1",
                "questions": [
                    {
                        "id": "confirm",
                        "header": "Confirm recipient",
                        "question": "Send this message to Mehdi?",
                        "options": [
                            {"label": "Send", "value": "send"},
                            {"label": "Cancel", "value": "cancel"},
                        ],
                    }
                ],
            },
        },
    )
    expect(page.get_by_text("Cowork needs your approval to finish this action.")).to_be_visible()
    expect(page.get_by_role("button", name="Answer and continue")).to_be_visible()
    page.screenshot(
        path=os.path.join(TEMP_DIR, "cowork-execution-approval.png"),
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
