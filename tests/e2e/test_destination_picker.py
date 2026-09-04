"""E2E gates for destination binding, direct actions, and the picker."""

import json
import os
import re

import pytest
from playwright.sync_api import Page, expect


SCREENSHOTS_DIR = os.path.join("temp", "cowork-destination")
CONV_ID = "tenant:user:cw-desttest"
THREAD = "19:aaaa_bbbb@unq.gbl.spaces"


def _seed_task(page: Page, base_url: str) -> int:
    response = page.request.post(
        f"{base_url}/api/tasks",
        data={
            "title": "Destination picker gate",
            "description": "Destination binding test",
            "action_type": "follow-up",
        },
    )
    assert response.ok
    return response.json()["task"]["id"]


def _delete_task(page: Page, base_url: str, task_id: int) -> None:
    page.request.delete(f"{base_url}/api/tasks/{task_id}")


def _action(task_id: int, **overrides) -> dict:
    action = {
        "id": 4242,
        "task_id": task_id,
        "state": "ready",
        "finding": "Found",
        "draft": "Draft",
        "destination_kind": "one_to_one",
        "destination_ref": THREAD,
        "destination_display": "Sarah Goodwin (direct message)",
        "destination_confirmed_at": "2026-08-01T12:00:00Z",
        "destination_source": "auto_source_url",
        "delivery_channel": "teams",
        "conversation_id": CONV_ID,
        "is_broadcast": False,
        # A real ready row is already marked seen by the time the card is open;
        # without it the /todo adapter re-fetches and discards the fixture.
        "seen_at": "2026-08-01T12:00:00Z",
    }
    action.update(overrides)
    return action


def _load_dashboard(page: Page, base_url: str, task_id: int, action: dict) -> None:
    page.goto(base_url + "/")
    page.wait_for_function(f"Boolean(tasks.find(task => task.id === {task_id}))")
    page.evaluate(
        f"""
        _cwActions[{task_id}] = {json.dumps(action)};
        selectedTaskId = {task_id};
        const task = tasks.find(task => task.id === {task_id});
        task.parse_status = 'parsed';
        clearInterval(parsePollerInterval);
        parsePollerInterval = null;
        renderDetailPane(task);
        """
    )


def _load_todo(page: Page, base_url: str, task_id: int, action: dict) -> None:
    page.goto(base_url + "/todo")
    page.wait_for_function(f"Boolean(tasks.find(task => task.id === {task_id}))")
    page.evaluate(
        f"""
        const action = {json.dumps(action)};
        const task = tasks.find(item => item.id === {task_id});
        Object.assign(task, {{
            cw_loaded: true, cw_state: 'ready', cw_seen_at: 'seen',
            parse_status: 'parsed',
            cw_finding: action.finding, cw_draft: action.draft,
            cw_dest_kind: action.destination_kind,
            cw_dest_ref: action.destination_ref,
            cw_dest_display: action.destination_display,
            cw_dest_confirmed_at: action.destination_confirmed_at,
            cw_delivery_channel: action.delivery_channel,
            cw_is_broadcast: action.is_broadcast,
            cw_conversation_id: action.conversation_id
        }});
        selectTask({task_id});
        """
    )


class TestDestinationBinding:
    @pytest.mark.parametrize(
        "mismatch_error",
        [
            "Calendar attendees changed before scheduler query",
            (
                "Could not complete WorkIQ preview: "
                "Calendar attendees changed during resolution"
            ),
        ],
    )
    def test_historical_attendee_mismatch_explains_key_people_and_retries(
        self, page: Page, base_url, mismatch_error
    ):
        page.set_viewport_size({"width": 1280, "height": 900})
        task_id = _seed_task(page, base_url)
        action = _action(
            task_id,
            state="failed",
            delivery_channel="calendar",
            structured_payload=json.dumps({
                "schema_version": 1,
                "channel": "calendar",
            }),
            error=mismatch_error,
        )
        posted = {}

        def retry_route(route):
            posted.update(route.request.post_data_json)
            route.fulfill(
                status=202,
                content_type="application/json",
                body=json.dumps({
                    "action": {
                        **action,
                        "state": "previewing",
                        "error": None,
                    },
                }),
            )

        try:
            _load_dashboard(page, base_url, task_id, action)
            page.evaluate(
                """taskId => {
                    const task = tasks.find(item => item.id === taskId);
                    task.action_type = 'schedule-meeting';
                    task.description =
                        'Meet with Rima, Henry, Christopher, and Michael.';
                    task.key_people = JSON.stringify([
                        {name: 'Rima Reyes', email: 'rima@microsoft.com'},
                        {name: 'Henry James', email: 'henry@microsoft.com'}
                    ]);
                    renderDetailPane(task);
                }""",
                task_id,
            )
            page.route(f"**/api/tasks/{task_id}/cowork", retry_route)

            notice = page.get_by_test_id("cw-attendee-authority-error")
            expect(notice).to_be_visible()
            expect(notice).to_contain_text("Key People controls who is invited")
            expect(notice).to_contain_text(
                "Names mentioned in the task description are not added"
            )
            expect(
                page.get_by_role("button", name="Retry with Key People")
            ).to_be_visible()
            expect(page.get_by_role("button", name="Redo")).to_have_count(0)
            bounds = notice.bounding_box()
            assert bounds and bounds["width"] >= 300 and bounds["height"] < 220

            os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
            page.screenshot(
                path=os.path.join(
                    SCREENSHOTS_DIR,
                    "attendee-authority-failure-"
                    + (
                        "before-scheduler"
                        if "before scheduler" in mismatch_error
                        else "during-resolution"
                    )
                    + "-light.png",
                ),
                full_page=True,
            )

            page.get_by_role("button", name="Retry with Key People").click()
            expect(
                page.get_by_test_id("cw-attendee-authority-error")
            ).to_have_count(0)
            assert posted == {"interaction_mode": "interaction"}
        finally:
            _delete_task(page, base_url, task_id)

    def test_dashboard_confirmed_one_to_one_and_open_link(self, page: Page, base_url):
        task_id = _seed_task(page, base_url)
        os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
        try:
            _load_dashboard(page, base_url, task_id, _action(task_id))
            status = page.get_by_test_id("dest-status")
            expect(status).to_be_visible()
            expect(status).to_contain_text("Sarah Goodwin")
            expect(page.get_by_test_id("dest-confirmed")).to_be_visible()
            expect(page.get_by_test_id("dest-change-btn")).to_be_visible()
            expect(page.get_by_test_id("cw-open-cowork")).to_be_visible()
            page.screenshot(
                path=os.path.join(SCREENSHOTS_DIR, "dashboard-confirmed-light.png"),
                full_page=True,
            )
            page.evaluate(
                "document.documentElement.setAttribute('data-theme','dark')"
            )
            page.screenshot(
                path=os.path.join(SCREENSHOTS_DIR, "dashboard-confirmed-dark.png"),
                full_page=True,
            )
        finally:
            _delete_task(page, base_url, task_id)

    def test_unconfirmed_email_skips_redundant_picker_and_shows_address(
        self, page: Page, base_url
    ):
        task_id = _seed_task(page, base_url)
        posted = {}
        action = _action(
            task_id,
            draft="Subject: Workshop follow-up\n\nHi Phil,\n\nFollowing up.\n\nPhil",
            destination_ref="phil@topness.com",
            destination_display="Phil Topness",
            destination_confirmed_at=None,
            destination_source="user_picker",
            delivery_channel="email",
        )

        def destination_route(route):
            posted.update(json.loads(route.request.post_data))
            confirmed = {
                **action,
                "destination_confirmed_at": "2026-08-16T12:00:00Z",
            }
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"action": confirmed}),
            )

        try:
            _load_dashboard(page, base_url, task_id, action)
            page.evaluate(
                """taskId => {
                    const task = tasks.find(item => item.id === taskId);
                    task.action_type = 'respond-email';
                    renderDetailPane(task);
                }""",
                task_id,
            )
            page.route(
                f"**/api/tasks/{task_id}/cowork/destination",
                destination_route,
            )

            page.evaluate("taskId => cwOpenExecuteConfirm(taskId)", task_id)

            confirmation = page.get_by_test_id("execute-confirmation")
            expect(confirmation).to_be_visible()
            expect(page.get_by_test_id("dest-picker")).to_have_count(0)
            expect(confirmation).to_contain_text("Phil Topness")
            expect(confirmation).to_contain_text("phil@topness.com")
            expect(confirmation).to_contain_text("Workshop follow-up")
            assert posted == {
                "destination_ref": "phil@topness.com",
                "destination_display": "Phil Topness",
                "delivery_channel": "email",
            }
            os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
            page.evaluate(
                "document.documentElement.setAttribute('data-theme','dark')"
            )
            page.screenshot(
                path=os.path.join(
                    SCREENSHOTS_DIR, "email-direct-confirmation-dark.png"
                ),
                full_page=True,
            )
        finally:
            _delete_task(page, base_url, task_id)

    def test_delivery_channel_email_skips_redundant_picker(
        self, page: Page, base_url
    ):
        task_id = _seed_task(page, base_url)
        posted = {}
        action = _action(
            task_id,
            draft="Subject: Workshop follow-up\n\nFollowing up.",
            destination_ref="phil@topness.com",
            destination_display="Phil Topness",
            destination_confirmed_at=None,
            delivery_channel="email",
        )

        def destination_route(route):
            posted.update(json.loads(route.request.post_data))
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    "action": {
                        **action,
                        "destination_confirmed_at": "2026-08-16T12:00:00Z",
                    }
                }),
            )

        try:
            _load_dashboard(page, base_url, task_id, action)
            page.evaluate(
                """taskId => {
                    const task = tasks.find(item => item.id === taskId);
                    task.action_type = 'follow-up';
                    renderDetailPane(task);
                }""",
                task_id,
            )
            page.route(
                f"**/api/tasks/{task_id}/cowork/destination",
                destination_route,
            )

            page.evaluate("taskId => cwOpenExecuteConfirm(taskId)", task_id)

            expect(page.get_by_test_id("execute-confirmation")).to_be_visible()
            expect(page.get_by_test_id("dest-picker")).to_have_count(0)
            assert posted["delivery_channel"] == "email"
        finally:
            _delete_task(page, base_url, task_id)

    def test_subjectless_email_is_repaired_before_send_confirmation(
        self, page: Page, base_url
    ):
        task_id = _seed_task(page, base_url)
        dialogs = []
        saved = {}
        action = _action(
            task_id,
            finding="Cowork found the original thread.",
            draft="Hi Phil,\n\nThanks for the update.",
            destination_ref="phil@topness.com",
            destination_display="Phil Topness",
            delivery_channel="email",
        )
        page.route(
            f"**/api/tasks/{task_id}/cowork",
            lambda route: (
                saved.update(route.request.post_data_json),
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({
                        "action": {
                            **action,
                            "draft_edited": route.request.post_data_json[
                                "draft_edited"
                            ],
                        }
                    }),
                ),
            ),
        )
        page.on(
            "dialog",
            lambda dialog: (dialogs.append(dialog.message), dialog.accept()),
        )
        try:
            _load_dashboard(page, base_url, task_id, action)
            page.evaluate(
                """taskId => {
                    const task = tasks.find(item => item.id === taskId);
                    task.action_type = 'respond-email';
                    renderDetailPane(task);
                    cwOpenExecuteConfirm(taskId);
                }""",
                task_id,
            )

            confirmation = page.get_by_test_id("execute-confirmation")
            expect(confirmation).to_be_visible()
            expect(confirmation).to_contain_text("Destination picker gate")
            expect(confirmation).to_contain_text("Thanks for the update.")
            assert saved["draft_edited"].startswith(
                "Subject: Destination picker gate\n\n"
            )
            assert page.evaluate(
                "taskId => _cwExecuteApprovals[taskId].draft",
                task_id,
            ) == saved["draft_edited"]
            assert page.evaluate(
                """() => cwDeriveEmailSubject(
                    {title: 'Reply to Phil about the Ascentium contract'},
                    {finding: ''}
                )"""
            ) == "Re: the Ascentium contract"
            assert page.evaluate(
                """() => cwDeriveEmailSubject(
                    {title: 'Follow up'},
                    {finding: '**Subject:** Ascentium proposal - next steps'}
                )"""
            ) == "Ascentium proposal - next steps"
            assert page.evaluate(
                """() => cwDeriveEmailSubject(
                    {
                        title: 'Reply to Sarah about the proposal',
                        action_type: 'respond-email',
                        source_type: 'email',
                        source_id: 'email::sarah@example.com::FY27 proposal'
                    },
                    {finding: ''}
                )"""
            ) == "Re: FY27 proposal"
            assert dialogs == []
            page.screenshot(
                path=os.path.join(
                    SCREENSHOTS_DIR, "email-subject-repaired-light.png"
                ),
                full_page=True,
            )
            page.evaluate(
                "document.documentElement.setAttribute('data-theme', 'dark')"
            )
            page.screenshot(
                path=os.path.join(
                    SCREENSHOTS_DIR, "email-subject-repaired-dark.png"
                ),
                full_page=True,
            )
        finally:
            _delete_task(page, base_url, task_id)

    def test_subject_only_email_never_opens_send_confirmation(
        self, page: Page, base_url
    ):
        task_id = _seed_task(page, base_url)
        dialogs = []
        action = _action(
            task_id,
            draft="Subject: Workshop follow-up",
            destination_ref="phil@topness.com",
            destination_display="Phil Topness",
            delivery_channel="email",
        )
        page.on(
            "dialog",
            lambda dialog: (dialogs.append(dialog.message), dialog.accept()),
        )
        try:
            _load_dashboard(page, base_url, task_id, action)
            page.evaluate(
                """taskId => {
                    const task = tasks.find(item => item.id === taskId);
                    task.action_type = 'respond-email';
                    cwOpenExecuteConfirm(taskId);
                }""",
                task_id,
            )

            expect(page.get_by_test_id("execute-confirmation")).to_have_count(0)
            assert dialogs == ["The final email draft must include a message body."]
        finally:
            _delete_task(page, base_url, task_id)

    def test_dashboard_broadcast_warns_but_never_gates_open_link(
        self, page: Page, base_url
    ):
        task_id = _seed_task(page, base_url)
        os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
        try:
            _load_dashboard(
                page,
                base_url,
                task_id,
                _action(
                    task_id,
                    destination_kind="channel",
                    destination_display="Copilot CAT channel",
                    destination_confirmed_at=None,
                    destination_source=None,
                    is_broadcast=True,
                ),
            )
            expect(page.get_by_test_id("dest-risky")).to_be_visible()
            expect(page.get_by_test_id("dest-confirmed")).to_have_count(0)
            expect(page.get_by_test_id("cw-open-cowork")).to_be_visible()
            page.screenshot(
                path=os.path.join(SCREENSHOTS_DIR, "dashboard-broadcast-light.png"),
                full_page=True,
            )
        finally:
            _delete_task(page, base_url, task_id)

    def test_dashboard_picker_posts_validated_bundle(self, page: Page, base_url):
        task_id = _seed_task(page, base_url)
        try:
            _load_dashboard(
                page,
                base_url,
                task_id,
                _action(
                    task_id,
                    destination_kind="none",
                    destination_ref="",
                    destination_display="",
                    destination_confirmed_at=None,
                    delivery_channel=None,
                ),
            )
            page.route(
                f"**/api/tasks/{task_id}/cowork/destination",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "action": _action(
                                task_id,
                                destination_kind="none",
                                destination_ref="sarah@microsoft.com",
                                destination_display="Sarah Goodwin",
                                delivery_channel="email",
                                destination_source="user_picker",
                            )
                        }
                    ),
                ),
            )
            page.get_by_test_id("dest-change-btn").click()
            expect(page.get_by_test_id("dest-picker")).to_be_visible()
            page.select_option('[data-testid="dest-channel"]', "email")
            page.fill('[data-testid="dest-ref"]', "sarah@microsoft.com")
            page.fill('[data-testid="dest-display"]', "Sarah Goodwin")

            with page.expect_request(
                lambda request: request.method == "POST"
                and f"/api/tasks/{task_id}/cowork/destination" in request.url
            ) as request_info:
                page.get_by_test_id("dest-confirm-btn").click()

            body = request_info.value.post_data_json
            assert body["delivery_channel"] == "email"
            assert body["destination_ref"] == "sarah@microsoft.com"
            assert body["destination_display"] == "Sarah Goodwin"
            expect(page.get_by_test_id("dest-picker")).to_have_count(0)
            expect(page.get_by_test_id("dest-status")).to_contain_text(
                "Sarah Goodwin"
            )
        finally:
            _delete_task(page, base_url, task_id)

    def test_create_meeting_opens_meeting_details_not_teams_picker(
        self, page: Page, base_url
    ):
        task_id = _seed_task(page, base_url)
        os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
        action = _action(
            task_id,
            draft=(
                "**Title:** 1:1 with Rima Reyes\n\n"
                "**When:** Monday, August 17 at 10:05 AM\n\n"
                "**Duration:** 30 minutes"
            ),
            destination_kind="none",
            destination_ref="rima.reyes@microsoft.com",
            destination_display="Rima Reyes",
            destination_confirmed_at=None,
            destination_source="auto_key_people",
            delivery_channel=None,
            calendar_preview={
                "attendees": ["rima.reyes@microsoft.com"],
                "date_time": (
                    "Monday, August 17, 2026 · 10:05 AM–10:35 AM · "
                    "America/New_York"
                ),
                "format": "Teams meeting",
                "subject": "1:1 with Rima Reyes",
                "body_html": (
                    "<p><strong>Agenda</strong></p><ul>"
                    "<li>Review current priorities</li></ul>"
                ),
            },
        )
        confirmed = {**action, "destination_confirmed_at": "2026-08-13T19:00:00Z"}
        posted = {}

        def destination_route(route):
            posted.update(route.request.post_data_json)
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"action": confirmed}),
            )

        try:
            _load_dashboard(page, base_url, task_id, action)
            page.evaluate(
                f"""
                const task = tasks.find(item => item.id === {task_id});
                task.parse_status = 'parsed';
                task.action_type = 'schedule-meeting';
                task.key_people = JSON.stringify([
                    {{name: 'Rima Reyes', email: 'rima.reyes@microsoft.com'}}
                ]);
                renderDetailPane(task);
                """
            )
            page.route(
                f"**/api/tasks/{task_id}/cowork/destination",
                destination_route,
            )

            page.get_by_role("button", name="Review meeting").click()

            expect(page.get_by_test_id("execute-confirmation")).to_be_visible()
            expect(page.get_by_test_id("dest-picker")).to_have_count(0)
            expect(page.get_by_test_id("dest-channel")).to_have_count(0)
            modal = page.get_by_test_id("execute-confirmation")
            expect(modal.get_by_test_id("meeting-confirm-attendees")).to_contain_text(
                "Rima Reyes"
            )
            expect(modal.get_by_test_id("meeting-confirm-date-time")).to_contain_text(
                "Date & timeMonday, August 17, 2026 · 10:05 AM–10:35 AM · "
                "America/New_York"
            )
            expect(modal.get_by_test_id("meeting-confirm-date-time")).to_contain_text(
                "Teams meeting"
            )
            expect(modal.get_by_test_id("meeting-confirm-body")).to_contain_text(
                "Review current priorities"
            )
            expect(modal.get_by_test_id("meeting-confirm-body")).to_contain_text(
                "Subject1:1 with Rima Reyes"
            )
            body_section = modal.get_by_test_id("meeting-confirm-body")
            body_box = body_section.bounding_box()
            subject_box = body_section.locator(
                ".cw-meeting-confirm-subject"
            ).bounding_box()
            assert body_box and body_box["width"] >= 480
            assert subject_box and subject_box["x"] >= body_box["x"] + 100
            expect(modal).not_to_contain_text("Meeting details")
            expect(modal).not_to_contain_text("Availability")
            assert posted == {
                "destination_ref": "rima.reyes@microsoft.com",
                "destination_display": "Rima Reyes",
            }
            page.screenshot(
                path=os.path.join(
                    SCREENSHOTS_DIR, "schedule-meeting-details-light.png"
                ),
                full_page=True,
            )
            page.evaluate(
                "document.documentElement.setAttribute('data-theme','dark')"
            )
            page.screenshot(
                path=os.path.join(
                    SCREENSHOTS_DIR, "schedule-meeting-details-dark.png"
                ),
                full_page=True,
            )
            page.set_viewport_size({"width": 375, "height": 812})
            expect(modal).to_be_visible()
            expect(modal.get_by_test_id("meeting-confirm-date-time")).to_be_visible()
            page.screenshot(
                path=os.path.join(
                    SCREENSHOTS_DIR, "schedule-meeting-details-mobile.png"
                ),
                full_page=True,
            )
        finally:
            _delete_task(page, base_url, task_id)

    def test_create_meeting_uses_finding_when_selected_slot_has_no_draft(
        self, page: Page, base_url
    ):
        task_id = _seed_task(page, base_url)
        meeting_details = (
            "**Phil / Rima 1:1**\n\n"
            "- **When:** Monday, August 17, 3:05–3:30 PM ET\n"
            "- **Where:** Teams meeting\n\n"
            "**Agenda**\n- Current priorities\n- Open questions"
        )
        action = _action(
            task_id,
            action_type="schedule-meeting",
            draft=None,
            finding=meeting_details,
            destination_kind="none",
            destination_ref="rima.reyes@microsoft.com",
            destination_display="Rima Reyes",
            destination_confirmed_at="2026-08-13T19:00:00Z",
            destination_source="auto_key_people",
            delivery_channel=None,
            calendar_preview={
                "attendees": ["rima.reyes@microsoft.com"],
                "date_time": (
                    "Monday, August 17, 2026 · 10:05 AM–10:35 AM · "
                    "America/New_York"
                ),
                "format": "Teams meeting",
                "subject": "1:1 with Rima Reyes",
                "body_html": (
                    "<p><strong>Agenda</strong></p><ul>"
                    "<li>Review current priorities</li></ul>"
                ),
            },
        )
        try:
            _load_dashboard(page, base_url, task_id, action)
            page.evaluate(
                f"""
                const task = tasks.find(item => item.id === {task_id});
                task.action_type = 'schedule-meeting';
                task.key_people = JSON.stringify([
                    {{name: 'Rima Reyes', email: 'rima.reyes@microsoft.com'}}
                ]);
                renderDetailPane(task);
                """
            )

            draft_area = page.get_by_test_id("cowork-draft-click-edit")
            expect(draft_area).to_contain_text(
                "Monday, August 17, 3:05–3:30 PM ET"
            )
            assert page.evaluate(
                f"cwCurrentDraft(_cwActions[{task_id}])"
            ) == meeting_details
            draft_area.click()
            editor = page.locator(f"#cw-draft-{task_id}")
            expect(editor).to_have_value(
                re.compile("Monday, August 17, 3:05–3:30 PM ET")
            )
            os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
            page.screenshot(
                path=os.path.join(
                    SCREENSHOTS_DIR, "schedule-meeting-edit-fallback.png"
                ),
                full_page=True,
            )
            page.get_by_role("button", name="Cancel").last.click()
            refine_calls = page.evaluate(
                f"""
                () => {{
                    const originalFetch = window.fetch;
                    let calls = 0;
                    window.fetch = (...args) => {{
                        if (String(args[0]).includes('/cowork/refine')) calls += 1;
                        return Promise.resolve({{
                            ok: true,
                            json: () => Promise.resolve({{}})
                        }});
                    }};
                    _cwRefine[{task_id}] = true;
                    cwRerender({task_id});
                    cwSendRefine({task_id});
                    delete _cwRefine[{task_id}];
                    window.fetch = originalFetch;
                    cwRerender({task_id});
                    return calls;
                }}
                """
            )
            assert refine_calls == 0

            page.get_by_role("button", name="Review meeting").click()

            confirmation = page.get_by_test_id("execute-confirmation")
            expect(confirmation).to_be_visible()
            expect(confirmation.get_by_test_id("meeting-confirm-date-time")).to_contain_text(
                "Monday, August 17, 2026"
            )
            expect(confirmation.get_by_test_id("meeting-confirm-body")).to_contain_text(
                "Review current priorities"
            )
            expect(confirmation).not_to_contain_text(
                "Monday, August 17, 3:05–3:30 PM ET"
            )
            approval_draft = page.evaluate(
                f"_cwExecuteApprovals[{task_id}].draft"
            )
            assert "Monday, August 17, 3:05–3:30 PM ET" in approval_draft
            page.screenshot(
                path=os.path.join(
                    SCREENSHOTS_DIR, "schedule-meeting-finding-fallback.png"
                ),
                full_page=True,
            )
        finally:
            _delete_task(page, base_url, task_id)

    def test_create_meeting_fails_closed_without_canonical_calendar_preview(
        self, page: Page, base_url
    ):
        task_id = _seed_task(page, base_url)
        action = _action(
            task_id,
            action_type="schedule-meeting",
            draft="**Planning review**\n\n**Agenda**\n- Next steps",
            destination_ref="rima.reyes@microsoft.com",
            destination_display="Rima Reyes",
            destination_confirmed_at="2026-08-13T19:00:00Z",
            delivery_channel=None,
        )
        dialogs = []
        page.on(
            "dialog",
            lambda dialog: (dialogs.append(dialog.message), dialog.accept()),
        )
        try:
            _load_dashboard(page, base_url, task_id, action)
            page.evaluate(
                """taskId => {
                    const task = tasks.find(item => item.id === taskId);
                    task.action_type = 'schedule-meeting';
                    task.key_people = JSON.stringify([{
                        name: 'Rima Reyes',
                        email: 'rima.reyes@microsoft.com'
                    }]);
                    renderDetailPane(task);
                }""",
                task_id,
            )

            page.get_by_role("button", name="Review meeting").click()

            expect(page.get_by_test_id("execute-confirmation")).to_have_count(0)
            assert dialogs == [
                "This meeting preview is incomplete. Start over and review a fresh "
                "calendar preview before creating the meeting."
            ]
        finally:
            _delete_task(page, base_url, task_id)

    def test_multi_attendee_meeting_uses_bound_attendee_snapshot(
        self, page: Page, base_url
    ):
        task_id = _seed_task(page, base_url)
        action = _action(
            task_id,
            draft="**Title:** Planning review\n\n**When:** Monday at 10:05 AM",
            destination_kind="none",
            destination_ref=(
                '["kanika@microsoft.com","rima@microsoft.com",'
                '"henry@microsoft.com"]'
            ),
            destination_display="Kanika Ramji, Rima Reyes, Henry James",
            destination_confirmed_at=None,
            destination_source=None,
            delivery_channel=None,
            calendar_preview={
                "attendees": [
                    "kanika@microsoft.com",
                    "rima@microsoft.com",
                    "henry@microsoft.com",
                ],
                "date_time": (
                    "Monday, August 17, 2026 · 10:05 AM–10:35 AM · "
                    "America/New_York"
                ),
                "format": "Teams meeting",
                "subject": "Rockwell CAPE Lighthouse kickoff",
                "body_html": (
                    "<p><strong>Agenda</strong></p><ul>"
                    "<li>Planning review</li></ul>"
                ),
            },
        )
        confirmed = {
            **action,
            "destination_ref": (
                '["kanika@microsoft.com","rima@microsoft.com",'
                '"henry@microsoft.com"]'
            ),
            "destination_display": "Kanika Ramji, Rima Reyes, Henry James",
            "destination_confirmed_at": "2026-08-13T19:00:00Z",
        }
        posted = {}

        def destination_route(route):
            posted.update(route.request.post_data_json)
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"action": confirmed}),
            )

        try:
            _load_dashboard(page, base_url, task_id, action)
            page.evaluate(
                """taskId => {
                    const task = tasks.find(item => item.id === taskId);
                    task.action_type = 'schedule-meeting';
                    task.key_people = JSON.stringify([
                        {name: 'Kanika Ramji', email: 'kanika@microsoft.com'},
                        {name: 'Rima Reyes', email: 'rima@microsoft.com'},
                        {name: 'Henry James', email: 'henry@microsoft.com'}
                    ]);
                    renderDetailPane(task);
                }""",
                task_id,
            )
            page.route(
                f"**/api/tasks/{task_id}/cowork/destination",
                destination_route,
            )

            page.get_by_role("button", name="Review meeting").click()

            confirmation = page.get_by_test_id("execute-confirmation")
            expect(confirmation).to_be_visible()
            expect(page.get_by_test_id("dest-picker")).to_have_count(0)
            expect(confirmation).to_contain_text("Attendees")
            expect(confirmation).to_contain_text("Kanika Ramji")
            expect(confirmation).to_contain_text("Rima Reyes")
            expect(confirmation).to_contain_text("Henry James")
            expect(confirmation.get_by_test_id("meeting-attendee-pill")).to_have_count(3)
            assert posted == {
                "destination_ref": (
                    '["kanika@microsoft.com","rima@microsoft.com",'
                    '"henry@microsoft.com"]'
                ),
                "destination_display": "Kanika Ramji, Rima Reyes, Henry James",
            }
            os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
            page.screenshot(
                path=os.path.join(
                    SCREENSHOTS_DIR, "schedule-meeting-multi-attendee-light.png"
                ),
                full_page=True,
            )
        finally:
            _delete_task(page, base_url, task_id)

    def test_create_meeting_blocks_if_key_people_changed_to_unresolved(
        self, page: Page, base_url
    ):
        task_id = _seed_task(page, base_url)
        action = _action(
            task_id,
            draft="**Title:** Planning review\n\n**When:** Monday at 10:05 AM",
            destination_ref="rima@microsoft.com",
            destination_display="Rima Reyes",
            destination_confirmed_at="2026-08-13T19:00:00Z",
            delivery_channel=None,
        )
        dialogs = []
        page.on(
            "dialog",
            lambda dialog: (dialogs.append(dialog.message), dialog.accept()),
        )
        try:
            _load_dashboard(page, base_url, task_id, action)
            page.evaluate(
                """taskId => {
                    const task = tasks.find(item => item.id === taskId);
                    task.action_type = 'schedule-meeting';
                    task.key_people = JSON.stringify([
                        {name: 'Rima Reyes', email: 'rima@microsoft.com'},
                        {name: 'Henry James', alternatives: []}
                    ]);
                    renderDetailPane(task);
                }""",
                task_id,
            )

            page.evaluate("taskId => cwOpenExecuteConfirm(taskId)", task_id)

            expect(page.get_by_test_id("execute-confirmation")).to_have_count(0)
            assert dialogs == [
                "Resolve Henry James in Key People before scheduling."
            ]
        finally:
            _delete_task(page, base_url, task_id)

    def test_create_meeting_blocks_if_resolved_attendee_set_changed(
        self, page: Page, base_url
    ):
        task_id = _seed_task(page, base_url)
        action = _action(
            task_id,
            draft="**Title:** Planning review\n\n**When:** Monday at 10:05 AM",
            destination_ref='["rima@microsoft.com","henry@microsoft.com"]',
            destination_display="Rima Reyes, Henry James",
            destination_confirmed_at="2026-08-13T19:00:00Z",
            delivery_channel=None,
        )
        dialogs = []
        page.on(
            "dialog",
            lambda dialog: (dialogs.append(dialog.message), dialog.accept()),
        )
        try:
            _load_dashboard(page, base_url, task_id, action)
            page.evaluate(
                """taskId => {
                    const task = tasks.find(item => item.id === taskId);
                    task.action_type = 'schedule-meeting';
                    task.key_people = JSON.stringify([
                        {name: 'Rima Reyes', email: 'rima@microsoft.com'},
                        {name: 'Kanika Ramji', email: 'kanika@microsoft.com'}
                    ]);
                    renderDetailPane(task);
                }""",
                task_id,
            )

            page.get_by_role("button", name="Review meeting").click()

            expect(page.get_by_test_id("execute-confirmation")).to_have_count(0)
            assert dialogs == [
                "The attendee list changed after this preview. Start over so "
                "WorkIQ can check availability for the exact people shown in Key People."
            ]
        finally:
            _delete_task(page, base_url, task_id)

    def test_create_meeting_restarts_when_preview_snapshot_had_no_attendees(
        self, page: Page, base_url
    ):
        task_id = _seed_task(page, base_url)
        action = _action(
            task_id,
            draft="**Title:** Project Whale\n\n**When:** Monday at 10:05 AM",
            destination_ref=None,
            destination_display=None,
            destination_confirmed_at=None,
            delivery_channel=None,
        )
        dialogs = []
        page.on(
            "dialog",
            lambda dialog: (dialogs.append(dialog.message), dialog.accept()),
        )
        try:
            _load_dashboard(page, base_url, task_id, action)
            page.evaluate(
                """taskId => {
                    const task = tasks.find(item => item.id === taskId);
                    task.action_type = 'schedule-meeting';
                    task.key_people = JSON.stringify([
                        {name: 'Azharullah Meer', email: 'ameer@microsoft.com'}
                    ]);
                    renderDetailPane(task);
                }""",
                task_id,
            )

            page.get_by_role("button", name="Review meeting").click()

            expect(page.get_by_test_id("execute-confirmation")).to_have_count(0)
            assert dialogs == [
                "The attendee list changed after this preview. Start over so "
                "WorkIQ can check availability for the exact people shown in Key People."
            ]
        finally:
            _delete_task(page, base_url, task_id)

    def test_todoiq_shows_destination_and_keeps_open_link(self, page: Page, base_url):
        task_id = _seed_task(page, base_url)
        os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
        try:
            _load_todo(page, base_url, task_id, _action(task_id))
            expect(page.get_by_test_id("dest-status")).to_contain_text(
                "Sarah Goodwin"
            )
            expect(page.get_by_test_id("dest-confirmed")).to_be_visible()
            expect(page.get_by_test_id("cw-open-cowork")).to_be_visible()
            page.screenshot(
                path=os.path.join(SCREENSHOTS_DIR, "todo-confirmed-light.png"),
                full_page=True,
            )
            page.evaluate("document.body.classList.add('dark')")
            page.screenshot(
                path=os.path.join(SCREENSHOTS_DIR, "todo-confirmed-dark.png"),
                full_page=True,
            )
        finally:
            _delete_task(page, base_url, task_id)

    def test_todoiq_picker_posts_validated_bundle(self, page: Page, base_url):
        task_id = _seed_task(page, base_url)
        try:
            _load_todo(
                page,
                base_url,
                task_id,
                _action(
                    task_id,
                    destination_kind="none",
                    destination_ref="",
                    destination_display="",
                    destination_confirmed_at=None,
                    delivery_channel=None,
                ),
            )
            page.route(
                f"**/api/tasks/{task_id}/cowork/destination",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "action": _action(
                                task_id,
                                destination_kind="none",
                                destination_ref="sarah@microsoft.com",
                                destination_display="Sarah Goodwin",
                                delivery_channel="email",
                                destination_source="user_picker",
                            )
                        }
                    ),
                ),
            )
            page.get_by_test_id("dest-change-btn").click()
            expect(page.get_by_test_id("dest-picker")).to_be_visible()
            page.select_option('[data-testid="dest-channel"]', "email")
            page.fill('[data-testid="dest-ref"]', "sarah@microsoft.com")
            page.fill('[data-testid="dest-display"]', "Sarah Goodwin")

            with page.expect_request(
                lambda request: request.method == "POST"
                and f"/api/tasks/{task_id}/cowork/destination" in request.url
            ) as request_info:
                page.get_by_test_id("dest-confirm-btn").click()

            body = request_info.value.post_data_json
            assert body["delivery_channel"] == "email"
            assert body["destination_ref"] == "sarah@microsoft.com"
            assert "source" not in body
            expect(page.get_by_test_id("dest-picker")).to_have_count(0)
            expect(page.get_by_test_id("dest-confirmed")).to_be_visible()
            expect(page.get_by_test_id("cw-open-cowork")).to_be_visible()
        finally:
            _delete_task(page, base_url, task_id)

    def test_dashboard_shows_channel_and_drops_the_stale_choose_note(
        self, page: Page, base_url
    ):
        """A bound channel must be visible, and must silence the 'pick one' nag."""
        task_id = _seed_task(page, base_url)
        try:
            _load_dashboard(
                page,
                base_url,
                task_id,
                _action(
                    task_id,
                    destination_kind="none",
                    destination_ref="chjaya@microsoft.com",
                    destination_display="Chitra J",
                    delivery_channel="teams",
                    destination_source="user_picker",
                ),
            )
            expect(page.get_by_test_id("dest-channel-chip")).to_have_text("Teams")
            block = page.locator(".cw-dest").first.inner_text()
            assert "Choose Teams or email" not in block
        finally:
            _delete_task(page, base_url, task_id)

    def test_dashboard_channel_overrides_a_mismatched_shape_note(
        self, page: Page, base_url
    ):
        """Emailing a Teams-sourced task must not claim it lands in the thread."""
        task_id = _seed_task(page, base_url)
        try:
            _load_dashboard(
                page,
                base_url,
                task_id,
                _action(task_id, delivery_channel="email"),
            )
            expect(page.get_by_test_id("dest-channel-chip")).to_have_text("Email")
            block = page.locator(".cw-dest").first.inner_text()
            assert "same thread" not in block
        finally:
            _delete_task(page, base_url, task_id)

    def test_dashboard_broadcast_note_survives_channel_binding(
        self, page: Page, base_url
    ):
        """A broadcast warning outranks any transport note."""
        task_id = _seed_task(page, base_url)
        try:
            _load_dashboard(
                page,
                base_url,
                task_id,
                _action(
                    task_id,
                    destination_kind="channel",
                    destination_display="Copilot CAT channel",
                    delivery_channel="email",
                    is_broadcast=True,
                ),
            )
            block = page.locator(".cw-dest").first.inner_text()
            assert "public post to the whole team" in block
        finally:
            _delete_task(page, base_url, task_id)

    def test_todoiq_shows_channel_and_drops_the_stale_choose_note(
        self, page: Page, base_url
    ):
        task_id = _seed_task(page, base_url)
        try:
            _load_todo(
                page,
                base_url,
                task_id,
                _action(
                    task_id,
                    destination_kind="none",
                    destination_ref="chjaya@microsoft.com",
                    destination_display="Chitra J",
                    delivery_channel="teams",
                    destination_source="user_picker",
                ),
            )
            expect(page.get_by_test_id("dest-channel-chip")).to_have_text("Teams")
            block = page.locator(".cw-dest").first.inner_text()
            assert "Choose Teams or email" not in block
        finally:
            _delete_task(page, base_url, task_id)

    def test_dashboard_keeps_the_choose_note_when_there_is_no_recipient(
        self, page: Page, base_url
    ):
        """An app-wide voice preference is not a destination.

        The voice setting can bind a channel on a task that carries no audience
        signal at all. The channel note reads "Email to this recipient." and
        there is no recipient, so it must NOT displace the standing instruction
        to pick a destination before any send. Losing that would trade a safety
        surface for a preference.
        """
        task_id = _seed_task(page, base_url)
        try:
            _load_dashboard(
                page,
                base_url,
                task_id,
                _action(
                    task_id,
                    destination_kind="none",
                    destination_ref=None,
                    destination_display=None,
                    delivery_channel="email",
                    destination_source=None,
                ),
            )
            expect(page.get_by_test_id("dest-channel-chip")).to_have_text("Email")
            block = page.locator(".cw-dest").first.inner_text()
            assert "Choose Teams or email" in block
            assert "to this recipient" not in block
        finally:
            _delete_task(page, base_url, task_id)

    def test_todoiq_keeps_the_choose_note_when_there_is_no_recipient(
        self, page: Page, base_url
    ):
        task_id = _seed_task(page, base_url)
        try:
            _load_todo(
                page,
                base_url,
                task_id,
                _action(
                    task_id,
                    destination_kind="none",
                    destination_ref=None,
                    destination_display=None,
                    delivery_channel="email",
                    destination_source=None,
                ),
            )
            expect(page.get_by_test_id("dest-channel-chip")).to_have_text("Email")
            block = page.locator(".cw-dest").first.inner_text()
            assert "Choose Teams or email" in block
            assert "to this recipient" not in block
        finally:
            _delete_task(page, base_url, task_id)

    def test_confirmed_destination_exposes_channel_specific_action(self, page: Page, base_url):
        task_id = _seed_task(page, base_url)
        try:
            _load_dashboard(page, base_url, task_id, _action(task_id))
            expect(page.get_by_role("button", name="Send Teams message")).to_be_visible()
            expect(page.get_by_role("button", name="Send email")).to_have_count(0)
            expect(page.get_by_role("button", name="Create meeting")).to_have_count(0)
        finally:
            _delete_task(page, base_url, task_id)



class TestSchedulingDestinationNote:
    """A scheduling task ends in a calendar invite, not a chat reply.

    The one_to_one note says "Linear conversation, so a reply lands in the same
    thread." That is REPLY mechanics, and it reads wrong on a card whose whole
    purpose is to land a meeting - Phil flagged exactly this.

    The audience binding itself stays. It is the safety guarantee: it states WHO
    would receive this and is the only place that is confirmed. What changes is
    the mechanics sentence beside it.
    """

    def test_scheduling_note_talks_about_the_invite_not_a_reply(
        self, page: Page, base_url
    ):
        task_id = _seed_task(page, base_url)
        try:
            page.goto(base_url + "/")
            page.wait_for_function(
                f"Boolean(tasks.find(task => task.id === {task_id}))"
            )
            page.evaluate(
                f"""
                const t = tasks.find(x => x.id === {task_id});
                t.parse_status = 'parsed';
                t.action_type = 'schedule-meeting';
                t.key_people = JSON.stringify([
                    {{name: 'Sarah Goodwin', email: 'sarah@microsoft.com'}}
                ]);
                _cwActions[{task_id}] = {json.dumps(_action(0))};
                _cwActions[{task_id}].task_id = {task_id};
                selectedTaskId = {task_id};
                renderDetailPane(t);
                """
            )
            note = page.get_by_test_id("dest-note")
            expect(note).to_be_visible()
            expect(note).to_contain_text("invite")
            expect(note).not_to_contain_text("reply lands")
        finally:
            _delete_task(page, base_url, task_id)

    def test_scheduling_uses_calendar_attendees_not_source_chat_audience(
        self, page: Page, base_url
    ):
        """A Teams source can identify attendees, but the action targets a calendar."""
        task_id = _seed_task(page, base_url)
        try:
            page.goto(base_url + "/")
            page.wait_for_function(
                f"Boolean(tasks.find(task => task.id === {task_id}))"
            )
            action = _action(0, destination_kind="group", is_broadcast=True,
                             destination_display="group chat with Rima and Greg")
            action["task_id"] = task_id
            page.evaluate(
                f"""
                const t = tasks.find(x => x.id === {task_id});
                t.parse_status = 'parsed';
                t.action_type = 'schedule-meeting';
                t.key_people = JSON.stringify([
                    {{name: 'Rima Reyes', email: 'rima@microsoft.com'}},
                    {{name: 'Greg Howard', email: 'greg@microsoft.com'}}
                ]);
                _cwActions[{task_id}] = {json.dumps(action)};
                selectedTaskId = {task_id};
                renderDetailPane(t);
                """
            )
            expect(page.get_by_test_id("dest-note")).to_contain_text("invite")
            expect(page.get_by_test_id("dest-note")).not_to_contain_text("chat")
            expect(page.get_by_test_id("dest-status")).to_have_text(
                "Rima Reyes, Greg Howard"
            )
            expect(page.get_by_test_id("dest-risky")).to_have_count(0)
            expect(page.get_by_test_id("dest-safe")).to_be_visible()
            expect(page.get_by_test_id("dest-change-btn")).to_have_count(0)
            page.screenshot(
                path=os.path.join(
                    SCREENSHOTS_DIR,
                    "meeting-calendar-attendees-not-chat.png",
                ),
                full_page=True,
            )
        finally:
            _delete_task(page, base_url, task_id)

    def test_a_non_scheduling_task_keeps_the_reply_note(
        self, page: Page, base_url
    ):
        task_id = _seed_task(page, base_url)
        try:
            _load_dashboard(page, base_url, task_id, _action(task_id))
            expect(page.get_by_test_id("dest-note")).to_contain_text("reply lands")
        finally:
            _delete_task(page, base_url, task_id)
