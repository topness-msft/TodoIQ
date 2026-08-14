"""E2E gate on the honesty of the card's approval claim.

F34 established that `--tool-callback-config` is a per-message barrier carried by
the connected client, so it does NOT follow a conversation into the Cowork web
app. Once the user clicks "Finish in Cowork" they are in an ordinary Cowork
session and may well send from there - which is now deliberate.

The old copy read "Preview only - nothing is sent. You copy and send it
yourself." The first half is true of TodoIQ. The second half asserts how the
user will act, and after handoff it is simply wrong.

These tests pin the current distinction: research/drafting is read-only, while a
separate direct action requires exact review and confirmation.
"""

import json

from playwright.sync_api import Page, expect


def _seed(page: Page, base_url: str) -> int:
    response = page.request.post(
        f"{base_url}/api/tasks", data={"title": "Card copy probe"}
    )
    assert response.ok, response.text()
    return response.json()["task"]["id"]


def _delete(page: Page, base_url: str, task_id: int) -> None:
    page.request.delete(f"{base_url}/api/tasks/{task_id}")


def _card_text(page: Page, base_url: str, task_id: int) -> str:
    page.goto(base_url + "/")
    page.wait_for_function(f"Boolean(tasks.find(t => t.id === {task_id}))")
    page.evaluate(
        f"""
        const task = tasks.find(t => t.id === {task_id});
        task.parse_status = 'parsed';
        selectedTaskId = {task_id};
        renderDetailPane(task);
        """
    )
    page.wait_for_selector(".cw-card")
    return page.locator(".cw-card").first.inner_text()


class TestNoSendClaimIsScopedToTodoIQ:
    def test_does_not_promise_the_user_will_send_it_themselves(
        self, page: Page, base_url
    ):
        task_id = _seed(page, base_url)
        try:
            text = _card_text(page, base_url, task_id).lower()
            assert "send it yourself" not in text
        finally:
            _delete(page, base_url, task_id)

    def test_states_that_actions_require_review_and_confirmation(
        self, page: Page, base_url
    ):
        task_id = _seed(page, base_url)
        try:
            text = _card_text(page, base_url, task_id).lower()
            assert "nothing happens without your explicit review and confirmation" in text
        finally:
            _delete(page, base_url, task_id)

    def test_no_bare_absolute_nothing_is_sent(self, page: Page, base_url):
        """An unscoped 'nothing is sent' reads as a property of the action."""
        task_id = _seed(page, base_url)
        try:
            text = _card_text(page, base_url, task_id).lower()
            assert "nothing is sent" not in text
        finally:
            _delete(page, base_url, task_id)

    def test_unprepared_card_has_no_direct_action_control(
        self, page: Page, base_url
    ):
        task_id = _seed(page, base_url)
        try:
            _card_text(page, base_url, task_id)
            expect(page.get_by_role("button", name="Send Teams message")).to_have_count(0)
            expect(page.get_by_role("button", name="Send email")).to_have_count(0)
            expect(page.get_by_role("button", name="Create meeting")).to_have_count(0)
        finally:
            _delete(page, base_url, task_id)
