"""The preview card shows what Cowork is doing, on both surfaces.

A preview runs for a median of 119s (p90 224s, max 279s, 93% over 60s, measured
across 14 real logs). It used to render a fixed string, "Cowork is reading M365",
for that entire time.

The CLI was streaming liveness to stderr the whole while and we were writing it
to a log nobody read. GET now returns the tail as `action.progress`, and both
cards render the most recent line.

Both surfaces are covered deliberately: `/` and `/todo` have diverged four times
in this project, every time because only one was changed.
"""

import json

from playwright.sync_api import Page


PROGRESS = [
    "init: Ready",
    "tool: tool_search_tool",
    "Searching for your training sessions",
]


def _seed(page: Page, base_url: str) -> int:
    # The Cowork card is gated on a parsed task (4aa3bad); an unparsed one
    # renders no card, so there is no progress line to read.
    r = page.request.post(
        f"{base_url}/api/tasks",
        data={"title": "Progress probe", "parse_status": "parsed"},
    )
    assert r.ok, r.text()
    return r.json()["task"]["id"]


def _delete(page: Page, base_url: str, task_id: int) -> None:
    page.request.delete(f"{base_url}/api/tasks/{task_id}")


def _stub_preview(page: Page, task_id: int, progress, state="previewing"):
    """Serve a running preview with progress, without invoking Cowork."""
    # cwActionMatchesTask discards any action whose action_type/cowork_revision
    # does not match the task it is being attached to, so a stub that omits
    # them is dropped and the card falls back to "not run" with no progress
    # line. Tasks default to action_type 'general' (src/db.py:503).
    body = json.dumps(
        {
            "action": {
                "id": 1,
                "task_id": task_id,
                "state": state,
                "progress": progress,
                "finding": "",
                "draft": "",
                "intent": "draft a reply",
                "channel": "email",
                "action_type": "general",
                "cowork_revision": 0,
            }
        }
    )
    page.route(
        f"**/api/tasks/{task_id}/cowork*",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=body
        ),
    )


class TestDashboardLiveProgress:
    def test_card_shows_the_latest_progress_line(self, page: Page, base_url):
        tid = _seed(page, base_url)
        try:
            _stub_preview(page, tid, PROGRESS)
            page.goto(base_url + "/")
            page.wait_for_function(f"Boolean(tasks.find(t => t.id === {tid}))")
            page.evaluate(f"selectTask({tid})")
            page.wait_for_selector(f"#cw-live-{tid}", timeout=15000)
            assert page.locator(f"#cw-live-{tid}").inner_text().strip() == PROGRESS[-1]
        finally:
            _delete(page, base_url, tid)

    def test_falls_back_when_there_is_no_progress_yet(self, page: Page, base_url):
        """A run that has not emitted anything must not render an empty box."""
        tid = _seed(page, base_url)
        try:
            _stub_preview(page, tid, [])
            page.goto(base_url + "/")
            page.wait_for_function(f"Boolean(tasks.find(t => t.id === {tid}))")
            page.evaluate(f"selectTask({tid})")
            page.wait_for_selector(f"#cw-live-{tid}", timeout=15000)
            assert page.locator(f"#cw-live-{tid}").inner_text().strip()
        finally:
            _delete(page, base_url, tid)

    def test_progress_is_escaped_not_injected(self, page: Page, base_url):
        """Progress text originates from CLI output; treat it as untrusted."""
        tid = _seed(page, base_url)
        try:
            _stub_preview(page, tid, ["<img src=x onerror=alert(1)>"])
            page.goto(base_url + "/")
            page.wait_for_function(f"Boolean(tasks.find(t => t.id === {tid}))")
            page.evaluate(f"selectTask({tid})")
            page.wait_for_selector(f"#cw-live-{tid}", timeout=15000)
            assert page.locator(f"#cw-live-{tid} img").count() == 0
        finally:
            _delete(page, base_url, tid)


class TestTodoLiveProgress:
    """The second surface. Four divergences in this project say check both."""

    def test_card_shows_the_latest_progress_line(self, page: Page, base_url):
        tid = _seed(page, base_url)
        try:
            _stub_preview(page, tid, PROGRESS)
            page.goto(base_url + "/todo")
            page.wait_for_function(
                f"typeof tasks !== 'undefined' && tasks.some(t => t.id === {tid})"
            )
            page.evaluate(f"selectTask({tid})")
            page.wait_for_selector(f"#cw-live-{tid}", timeout=15000)
            assert page.locator(f"#cw-live-{tid}").inner_text().strip() == PROGRESS[-1]
        finally:
            _delete(page, base_url, tid)

    def test_progress_is_escaped_not_injected(self, page: Page, base_url):
        tid = _seed(page, base_url)
        try:
            _stub_preview(page, tid, ["<img src=x onerror=alert(1)>"])
            page.goto(base_url + "/todo")
            page.wait_for_function(
                f"typeof tasks !== 'undefined' && tasks.some(t => t.id === {tid})"
            )
            page.evaluate(f"selectTask({tid})")
            page.wait_for_selector(f"#cw-live-{tid}", timeout=15000)
            assert page.locator(f"#cw-live-{tid} img").count() == 0
        finally:
            _delete(page, base_url, tid)
