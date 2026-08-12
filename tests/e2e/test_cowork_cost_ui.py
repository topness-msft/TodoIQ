"""What the preview cost, shown on the card.

`cw-cost-display` was blocked for weeks on "there is no cost signal", which was
true of everything the CLI returns and false of the runtime. `GET /v1/cost`
gives month-to-date credits for the signed-in user, and the counter is stable
when nothing is running but moves immediately when something does, so the
difference across a preview is that preview's cost. Measured: a trivial 12.6s
turn with no tools cost 30.23 credits.

Both surfaces are covered because `/` and `/todo` have diverged four times in
this project, every time because only one was changed.
"""

import json

from playwright.sync_api import Page


def _seed(page: Page, base_url: str) -> int:
    r = page.request.post(f"{base_url}/api/tasks", data={"title": "Cost probe"})
    assert r.ok, r.text()
    return r.json()["task"]["id"]


def _delete(page: Page, base_url: str, task_id: int) -> None:
    page.request.delete(f"{base_url}/api/tasks/{task_id}")


def _stub(page: Page, task_id: int, cost):
    action = {
        "id": 1,
        "task_id": task_id,
        "state": "ready",
        "progress": [],
        "finding": "Checked Teams and mail.",
        "draft": "Here is the draft.",
        "intent": "draft a reply",
        "channel": "email",
        "cost_credits": cost,
    }
    page.route(
        f"**/api/tasks/{task_id}/cowork*",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"action": action}),
        ),
    )


def _open_dashboard(page: Page, base_url: str, tid: int):
    page.goto(base_url + "/")
    page.wait_for_function(f"Boolean(tasks.find(t => t.id === {tid}))")
    page.evaluate(f"selectTask({tid})")
    page.wait_for_selector(".cw-card, .cw-shell", timeout=15000)


def _open_todo(page: Page, base_url: str, tid: int):
    page.goto(base_url + "/todo")
    page.wait_for_function(
        f"typeof tasks !== 'undefined' && tasks.some(t => t.id === {tid})"
    )
    page.evaluate(f"selectTask({tid})")
    page.wait_for_selector(".cw-card", timeout=15000)


class TestDashboardCost:
    def test_cost_is_shown(self, page: Page, base_url):
        tid = _seed(page, base_url)
        try:
            _stub(page, tid, 30.231125)
            _open_dashboard(page, base_url, tid)
            assert "30.2 credits" in page.locator(".cw-card, .cw-shell").first.inner_text()
        finally:
            _delete(page, base_url, tid)

    def test_a_free_run_says_so_rather_than_showing_nothing(self, page: Page, base_url):
        tid = _seed(page, base_url)
        try:
            _stub(page, tid, 0)
            _open_dashboard(page, base_url, tid)
            assert "no credits" in page.locator(".cw-card, .cw-shell").first.inner_text()
        finally:
            _delete(page, base_url, tid)

    def test_an_unattributable_run_shows_no_cost_at_all(self, page: Page, base_url):
        """Two overlapping previews cannot be told apart, so we say nothing
        rather than print a wrong number."""
        tid = _seed(page, base_url)
        try:
            _stub(page, tid, None)
            _open_dashboard(page, base_url, tid)
            assert "credits" not in page.locator(".cw-card, .cw-shell").first.inner_text()
        finally:
            _delete(page, base_url, tid)

    def test_a_large_cost_is_grouped_and_rounded(self, page: Page, base_url):
        tid = _seed(page, base_url)
        try:
            _stub(page, tid, 1234.56)
            _open_dashboard(page, base_url, tid)
            assert "1,235 credits" in page.locator(".cw-card, .cw-shell").first.inner_text()
        finally:
            _delete(page, base_url, tid)


class TestTodoCost:
    """The second surface. Four divergences say check both."""

    def test_cost_is_shown(self, page: Page, base_url):
        tid = _seed(page, base_url)
        try:
            _stub(page, tid, 30.231125)
            _open_todo(page, base_url, tid)
            assert "30.2 credits" in page.locator(".cw-card").first.inner_text()
        finally:
            _delete(page, base_url, tid)

    def test_an_unattributable_run_shows_no_cost_at_all(self, page: Page, base_url):
        tid = _seed(page, base_url)
        try:
            _stub(page, tid, None)
            _open_todo(page, base_url, tid)
            assert "credits" not in page.locator(".cw-card").first.inner_text()
        finally:
            _delete(page, base_url, tid)
