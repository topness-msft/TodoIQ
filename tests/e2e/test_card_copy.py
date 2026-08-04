"""E2E gate on the honesty of the card's no-send claim.

F34 established that `--tool-callback-config` is a per-message barrier carried by
the connected client, so it does NOT follow a conversation into the Cowork web
app. Once the user clicks "Open in Cowork" they are in an ordinary Cowork
session and may well send from there - which is now deliberate.

The old copy read "Preview only - nothing is sent. You copy and send it
yourself." The first half is true of TodoIQ. The second half asserts how the
user will act, and after handoff it is simply wrong.

These tests pin the distinction: the claim must be scoped to this app, and must
not promise anything about what happens after handoff.
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
    page.evaluate(f"selectTask({task_id})")
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

    def test_still_states_this_app_does_not_send(self, page: Page, base_url):
        """The true, load-bearing half of the claim must survive."""
        task_id = _seed(page, base_url)
        try:
            text = _card_text(page, base_url, task_id).lower()
            assert "nothing is sent from here" in text
        finally:
            _delete(page, base_url, task_id)

    def test_no_bare_absolute_nothing_is_sent(self, page: Page, base_url):
        """An unscoped 'nothing is sent' reads as a property of the action."""
        task_id = _seed(page, base_url)
        try:
            text = _card_text(page, base_url, task_id).lower()
            bare = text.replace("nothing is sent from here", "")
            assert "nothing is sent" not in bare
        finally:
            _delete(page, base_url, task_id)

    def test_still_has_no_send_or_execute_control(self, page: Page, base_url):
        """Rewording copy must not smuggle in an execute affordance."""
        task_id = _seed(page, base_url)
        try:
            text = _card_text(page, base_url, task_id)
            for banned in ("Send now", "Execute", "Deliver", "Approve & send"):
                assert banned not in text, banned
        finally:
            _delete(page, base_url, task_id)
