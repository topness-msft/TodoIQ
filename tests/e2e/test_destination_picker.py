"""E2E gates for preview-only destination binding and the picker."""

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
        renderDetailPane(tasks.find(task => task.id === {task_id}));
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
            expect(page.get_by_role("link", name="Open in Cowork")).to_be_visible()
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
            expect(page.get_by_role("link", name="Open in Cowork")).to_be_visible()
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

    def test_todoiq_shows_destination_and_keeps_open_link(self, page: Page, base_url):
        task_id = _seed_task(page, base_url)
        os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
        try:
            _load_todo(page, base_url, task_id, _action(task_id))
            expect(page.get_by_test_id("dest-status")).to_contain_text(
                "Sarah Goodwin"
            )
            expect(page.get_by_test_id("dest-confirmed")).to_be_visible()
            expect(page.get_by_role("link", name="Open in Cowork")).to_be_visible()
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

    def test_no_send_or_execute_control_exists(self, page: Page, base_url):
        task_id = _seed_task(page, base_url)
        try:
            _load_dashboard(page, base_url, task_id, _action(task_id))
            card = page.locator(".cw-card").first.inner_text()
            for banned in ("Send", "Execute", "Deliver", "Approve & send"):
                assert banned not in card
        finally:
            _delete_task(page, base_url, task_id)
