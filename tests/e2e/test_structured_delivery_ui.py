import json
import os

from playwright.sync_api import Page, expect


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
TEMP_DIR = os.path.join(PROJECT_ROOT, "temp")
os.makedirs(TEMP_DIR, exist_ok=True)


def _seed(page: Page, base_url: str, title: str) -> int:
    response = page.request.post(base_url + "/api/tasks", data={"title": title})
    assert response.ok, response.text()
    return response.json()["task"]["id"]


def test_structured_calendar_selector_is_explicit_and_not_cowork(
    page: Page, base_url: str
):
    page.set_viewport_size({"width": 1280, "height": 900})
    task_id = _seed(page, base_url, "Schedule a 25-minute review")
    page.goto(base_url + "/")
    page.wait_for_function(
        f"typeof tasks !== 'undefined' && tasks.some(t => t.id === {task_id})"
    )
    page.evaluate(
        """taskId => {
            const task = tasks.find(t => t.id === taskId);
            task.parse_status = 'parsed';
            task.action_type = 'schedule-meeting';
            task.key_people = JSON.stringify([{
                name: 'Rima Reyes',
                email: 'rima@microsoft.com'
            }]);
            selectedTaskId = taskId;
            _cwActions[taskId] = {
                id: 401,
                task_id: taskId,
                action_type: 'schedule-meeting',
                state: 'previewing',
                delivery_channel: 'calendar',
                structured_payload: '{"schema_version":1,"channel":"calendar"}',
                destination_ref: 'rima@microsoft.com',
                destination_display: 'Rima Reyes',
                waiting_on_user: true,
                interaction_request: {
                    invocation_id: 'structured-calendar-401',
                    questions: [{
                        id: '0',
                        header: 'Select & create meeting',
                        question: 'Choose one verified time, then press Select & '
                            + 'create meeting. There is no second confirmation.',
                        multi_select: false,
                        options: [{
                            value: '0',
                            label: 'Monday, August 21 at 9:05 AM',
                            description: 'All confirmed attendees are available.'
                        }]
                    }],
                    schedule_evidence: {
                        valid: true,
                        source: 'FindMeetingTimes+interaction',
                        query_backed: true,
                        attendees: ['rima@microsoft.com'],
                        duration_minutes: 25,
                        start_offset_minutes: 5,
                        slots: [{
                            value: '0',
                            start: '2028-08-21T09:05:00-07:00',
                            end: '2028-08-21T09:30:00-07:00',
                            timezone: 'America/Los_Angeles',
                            availability: {'rima@microsoft.com': 'free'}
                        }]
                    }
                },
                blocked_question: '{"invocation_id":"structured-calendar-401"}'
            };
            renderDetailPane(task);
        }""",
        task_id,
    )

    card = page.locator(".cw-card")
    expect(card).to_contain_text("WorkIQ")
    expect(card).to_contain_text("Riveter is waiting for your selection")
    # The card must not claim the click alone books it; the button does.
    expect(card).not_to_contain_text("immediately")
    expect(card).to_contain_text("press Select & create meeting")
    expect(card).to_contain_text("no second confirmation")
    expect(page.get_by_test_id("cw-answer-submit")).to_have_text(
        "Select & create meeting"
    )
    # A parked structured run still needs a way out; Stop does not apply.
    expect(page.get_by_test_id("cw-restart")).to_be_visible()
    expect(page.get_by_test_id("cw-stop")).to_have_count(0)
    expect(page.get_by_test_id("cw-open-cowork")).to_have_count(0)
    box = card.bounding_box()
    assert box and box["width"] >= 450 and box["height"] >= 250
    page.screenshot(
        path=os.path.join(TEMP_DIR, "structured-calendar-selector.png"),
        full_page=True,
    )


def test_structured_delivery_evidence_is_attributed_to_workiq(
    page: Page, base_url: str
):
    task_id = _seed(page, base_url, "Reply to Sarah")
    page.goto(base_url + "/")
    page.wait_for_function(
        f"typeof tasks !== 'undefined' && tasks.some(t => t.id === {task_id})"
    )
    page.evaluate(
        """taskId => {
            const task = tasks.find(t => t.id === taskId);
            task.parse_status = 'parsed';
            task.action_type = 'respond-email';
            selectedTaskId = taskId;
            _cwActions[taskId] = {
                id: 402,
                task_id: taskId,
                action_type: 'respond-email',
                state: 'executed',
                delivery_channel: 'email',
                structured_payload: '{"schema_version":1,"channel":"email"}',
                destination_ref: 'sarah@microsoft.com',
                destination_display: 'Sarah Goodwin',
                draft: 'Subject: Project update\\n\\nApproved body',
                delivery_confirmed_at: '2028-08-20T12:00:00Z',
                workiq_delivery_ref: 'email-reply:message-1'
            };
            renderDetailPane(task);
        }""",
        task_id,
    )

    card = page.locator(".cw-card")
    expect(card).to_contain_text("Reply · WorkIQ")
    expect(card).to_contain_text("WorkIQ returned positive delivery evidence")
    expect(card).not_to_contain_text("Open in Cowork")
    page.screenshot(
        path=os.path.join(TEMP_DIR, "structured-email-delivered.png"),
        full_page=True,
    )


def _render_unconfirmed(page: Page, base_url: str, title: str, channel: str,
                        action_type: str) -> int:
    task_id = _seed(page, base_url, title)
    page.goto(base_url + "/")
    page.wait_for_function(
        f"typeof tasks !== 'undefined' && tasks.some(t => t.id === {task_id})"
    )
    page.evaluate(
        """({taskId, channel, actionType}) => {
            const task = tasks.find(t => t.id === taskId);
            task.parse_status = 'parsed';
            task.action_type = actionType;
            selectedTaskId = taskId;
            _cwActions[taskId] = {
                id: 501,
                task_id: taskId,
                action_type: actionType,
                state: 'execute_unconfirmed',
                delivery_channel: channel,
                structured_payload: '{"schema_version":1,"channel":"'
                    + channel + '"}',
                destination_display: 'Rima Reyes',
                draft: 'Approved content',
                error: 'Structured worker produced no readable output'
            };
            renderDetailPane(task);
        }""",
        {"taskId": task_id, "channel": channel, "actionType": action_type},
    )
    return task_id


def test_unconfirmed_calendar_offers_a_safe_retry(page: Page, base_url: str):
    page.set_viewport_size({"width": 1280, "height": 900})
    _render_unconfirmed(
        page, base_url, "Schedule a 25-minute review", "calendar",
        "schedule-meeting",
    )

    card = page.locator(".cw-card")
    expect(card).to_contain_text("Delivery could not be confirmed")
    expect(page.get_by_test_id("cw-retry")).to_have_text("Retry safely")
    expect(card).to_contain_text("cannot be sent twice")
    page.screenshot(
        path=os.path.join(TEMP_DIR, "structured-calendar-retry.png"),
        full_page=True,
    )


def test_unconfirmed_teams_withholds_retry(page: Page, base_url: str):
    """A stale Teams post cannot be checked, so no retry is offered."""
    task_id = _seed(page, base_url, "Ping the project chat")
    page.goto(base_url + "/")
    page.wait_for_function(
        f"typeof tasks !== 'undefined' && tasks.some(t => t.id === {task_id})"
    )
    page.evaluate(
        """taskId => {
            const task = tasks.find(t => t.id === taskId);
            task.parse_status = 'parsed';
            task.action_type = 'follow-up';
            selectedTaskId = taskId;
            const old = new Date(Date.now() - 300 * 60000).toISOString()
                .replace(/\\.\\d+Z$/, 'Z');
            _cwActions[taskId] = {
                id: 502, task_id: taskId, action_type: 'follow-up',
                state: 'execute_unconfirmed', delivery_channel: 'teams',
                structured_payload: '{"schema_version":1,"channel":"teams"}',
                destination_display: 'Project chat', draft: 'Approved content',
                updated_at: old,
                error: 'Structured worker produced no readable output'
            };
            renderDetailPane(task);
        }""",
        task_id,
    )

    card = page.locator(".cw-card")
    expect(card).to_contain_text("Delivery could not be confirmed")
    expect(page.get_by_test_id("cw-retry")).to_have_count(0)
    expect(card).to_contain_text("Check the destination before retrying")


def test_recent_unconfirmed_teams_offers_a_checked_retry(page: Page, base_url: str):
    """A recent Teams post can be looked up, but the promise is weaker."""
    page.set_viewport_size({"width": 1280, "height": 900})
    task_id = _seed(page, base_url, "Ping the project chat")
    page.goto(base_url + "/")
    page.wait_for_function(
        f"typeof tasks !== 'undefined' && tasks.some(t => t.id === {task_id})"
    )
    page.evaluate(
        """taskId => {
            const task = tasks.find(t => t.id === taskId);
            task.parse_status = 'parsed';
            task.action_type = 'follow-up';
            selectedTaskId = taskId;
            const recent = new Date(Date.now() - 4 * 60000).toISOString()
                .replace(/\\.\\d+Z$/, 'Z');
            _cwActions[taskId] = {
                id: 503, task_id: taskId, action_type: 'follow-up',
                state: 'execute_unconfirmed', delivery_channel: 'teams',
                structured_payload: '{"schema_version":1,"channel":"teams"}',
                destination_display: 'Project chat', draft: 'Approved content',
                updated_at: recent,
                error: 'Structured worker produced no readable output'
            };
            renderDetailPane(task);
        }""",
        task_id,
    )

    card = page.locator(".cw-card")
    expect(page.get_by_test_id("cw-retry")).to_have_text("Check and retry")
    # It must not borrow calendar's stronger guarantee.
    expect(card).to_contain_text("check the chat first")
    expect(card).not_to_contain_text("cannot be sent twice")
    page.screenshot(
        path=os.path.join(TEMP_DIR, "structured-teams-retry.png"),
        full_page=True,
    )
