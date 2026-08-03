"""E2E gates for the task-triage keyboard shortcuts.

Two defects reported from the live dogfood on 2026-08-03, both of which make
`d` (dismiss) look like it "stopped working":

1. The handler compares ``e.key === 'd'``. Holding Shift produces ``'D'``, so
   Shift+D silently does nothing.
2. After a dismiss the selection stays on the now-dismissed task. Dismiss is not
   a legal transition out of ``dismissed``, so every later press is a silent
   no-op until the user manually clicks another task. That is the behaviour that
   reads as "the shortcut is dead".
"""

import json

from playwright.sync_api import Page, expect


def _seed(page: Page, base_url: str, title: str) -> int:
    """Create a task and move it to 'suggested' via legal transitions."""
    response = page.request.post(f"{base_url}/api/tasks", data={"title": title})
    assert response.ok, f"create failed: {response.status} {response.text()}"
    task_id = response.json()["task"]["id"]
    dismissed = page.request.post(
        f"{base_url}/api/tasks/{task_id}/action", data={"action": "dismiss"}
    )
    assert dismissed.ok, f"dismiss failed: {dismissed.status} {dismissed.text()}"
    promoted = page.request.post(
        f"{base_url}/api/tasks/{task_id}/action",
        data={"action": "transition", "status": "suggested"},
    )
    assert promoted.ok, f"transition failed: {promoted.status} {promoted.text()}"
    assert promoted.json()["task"]["status"] == "suggested", promoted.json()
    return task_id


def _delete(page: Page, base_url: str, task_id: int) -> None:
    page.request.delete(f"{base_url}/api/tasks/{task_id}")


def _open(page: Page, base_url: str, task_id: int) -> None:
    page.goto(base_url + "/")
    page.wait_for_function(f"Boolean(tasks.find(t => t.id === {task_id}))")
    page.evaluate(f"selectTask({task_id})")


def _stub_actions(page: Page) -> None:
    """Record doAction calls instead of mutating state."""
    page.evaluate(
        """() => {
            window.__calls = [];
            window.doAction = function(id, action, status) {
                window.__calls.push({id: id, action: action, status: status});
            };
        }"""
    )


def _reset_calls(page: Page) -> None:
    page.evaluate("() => { window.__calls = []; }")


def _calls(page: Page) -> list:
    return page.evaluate("() => window.__calls")


class TestDismissShortcut:
    def test_lowercase_d_dismisses(self, page: Page, base_url):
        task_id = _seed(page, base_url, "KB probe lowercase")
        try:
            _open(page, base_url, task_id)
            _stub_actions(page)
            _reset_calls(page)
            page.keyboard.press("d")
            assert _calls(page) == [
                {"id": task_id, "action": "dismiss", "status": None}
            ]
        finally:
            _delete(page, base_url, task_id)

    def test_shift_d_also_dismisses(self, page: Page, base_url):
        """Shift+D emits 'D'; a capitalised action key must still be honoured."""
        task_id = _seed(page, base_url, "KB probe shift")
        try:
            _open(page, base_url, task_id)
            _stub_actions(page)
            _reset_calls(page)
            page.keyboard.press("Shift+D")
            assert _calls(page) == [
                {"id": task_id, "action": "dismiss", "status": None}
            ]
        finally:
            _delete(page, base_url, task_id)

    def test_other_action_keys_accept_shift(self, page: Page, base_url):
        task_id = _seed(page, base_url, "KB probe promote")
        try:
            _open(page, base_url, task_id)
            _stub_actions(page)
            _reset_calls(page)
            page.keyboard.press("Shift+P")
            assert _calls(page) == [
                {"id": task_id, "action": "promote", "status": None}
            ]
        finally:
            _delete(page, base_url, task_id)

    def test_shift_slash_still_opens_shortcuts_overlay(self, page: Page, base_url):
        """'?' is Shift+/ on most layouts and must not be swallowed."""
        task_id = _seed(page, base_url, "KB probe overlay")
        try:
            _open(page, base_url, task_id)
            page.keyboard.press("?")
            expect(page.locator("#shortcuts-overlay")).to_have_class(
                __import__("re").compile(r"\bopen\b")
            )
        finally:
            _delete(page, base_url, task_id)

    def test_selection_advances_so_repeated_dismiss_keeps_working(
        self, page: Page, base_url
    ):
        """The reported bug: the second press must not be a silent no-op."""
        first = _seed(page, base_url, "KB probe chain one")
        second = _seed(page, base_url, "KB probe chain two")
        try:
            _open(page, base_url, first)
            page.keyboard.press("d")
            page.wait_for_function(
                f"(tasks.find(t => t.id === {first}) || {{}}).status === 'dismissed'"
            )
            # Selection must have left the dismissed task for a still-actionable one.
            page.wait_for_function(
                "selectedTaskId !== null && "
                "(VALID_TRANSITIONS[(tasks.find(t => t.id === selectedTaskId) || {}).status] || [])"
                ".indexOf('dismissed') !== -1"
            )
            _stub_actions(page)
            _reset_calls(page)
            page.keyboard.press("d")
            calls = _calls(page)
            assert len(calls) == 1, calls
            assert calls[0]["action"] == "dismiss"
            assert calls[0]["id"] != first
        finally:
            _delete(page, base_url, first)
            _delete(page, base_url, second)

    def test_selection_clears_when_nothing_is_left(self, page: Page, base_url):
        task_id = _seed(page, base_url, "KB probe last one")
        try:
            _open(page, base_url, task_id)
            page.evaluate(
                "window.__origRows = _getVisibleRows;"
                "window._getVisibleRows = function() { return []; };"
            )
            page.keyboard.press("d")
            page.wait_for_function("selectedTaskId === null")
        finally:
            _delete(page, base_url, task_id)
