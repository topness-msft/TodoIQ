"""E2E gates for destination binding, direct actions, and the picker."""

import json
import os

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
                renderDetailPane(task);
                """
            )
            page.route(
                f"**/api/tasks/{task_id}/cowork/destination",
                destination_route,
            )

            page.get_by_role("button", name="Create meeting").click()

            expect(page.get_by_test_id("execute-confirmation")).to_be_visible()
            expect(page.get_by_test_id("dest-picker")).to_have_count(0)
            expect(page.get_by_test_id("dest-channel")).to_have_count(0)
            expect(page.get_by_test_id("execute-confirmation")).to_contain_text(
                "Meeting details"
            )
            expect(page.get_by_test_id("execute-confirmation")).to_contain_text(
                "Monday, August 17 at 10:05 AM"
            )
            expect(page.get_by_test_id("execute-confirmation")).to_contain_text(
                "Rima Reyes"
            )
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

    def test_a_broadcast_warning_still_outranks_the_scheduling_note(
        self, page: Page, base_url
    ):
        """Safety must never be traded away for nicer copy. A group chat still
        has to say everyone would see it, scheduling or not."""
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
                _cwActions[{task_id}] = {json.dumps(action)};
                selectedTaskId = {task_id};
                renderDetailPane(t);
                """
            )
            expect(page.get_by_test_id("dest-note")).to_contain_text("Everyone")
            expect(page.get_by_test_id("dest-risky")).to_be_visible()
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
