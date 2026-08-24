"""What a suggestion card is allowed to claim.

`/suggestion-check` writes into the same `waiting_activity` column as
`/waiting-check`, with its own vocabulary (likely_resolved / still_pending /
unclear). Two things were wrong with how that came back out.

It had the identical honesty gap `/waiting-check` had: on a WorkIQ error it
skipped the task entirely (.claude/commands/suggestion-check.md:74), so nothing
was written and the badge kept showing the previous verdict under the previous
timestamp. "I could not look" rendered as "I looked, then, and here is what I
found".

And the card read the raw JSON rather than the normalised contract, so a row
that carries no status - which is exactly what a recorded failure looks like -
fell through `cfg[activity.status]` to `{icon: '', label: undefined}` and
rendered the literal text "undefined" with a stale timestamp beside it.

These pin the corrected behaviour on the suggestion surface, matching the
waiting surface: a failure says so, and the earlier verdict is shown as earlier.
"""

import json

from playwright.sync_api import Page, expect


def _seed(page: Page, base_url: str, title="Suggestion probe") -> int:
    response = page.request.post(
        f"{base_url}/api/tasks",
        data={"title": title, "status": "suggested", "parse_status": "parsed"},
    )
    assert response.ok, response.text()
    return response.json()["task"]["id"]


def _delete(page: Page, base_url: str, task_id: int) -> None:
    page.request.delete(f"{base_url}/api/tasks/{task_id}")


def _activity(**over):
    base = {
        "version": 2, "producer": "suggestion-check", "check_state": "ok",
        "status": None, "summary": None, "checked_at": "2026-08-24T09:00:00Z",
        "check_since": None, "return_date": None, "error": None,
        "source_scope": "person", "conversation_id": None, "evidence": [],
        "previous": None,
    }
    base.update(over)
    return base


def _open(page: Page, base_url: str, task_id: int, activity: dict) -> None:
    """Render the detail pane with a server-shaped signal attached."""
    page.goto(base_url + "/")
    page.wait_for_function(f"Boolean(tasks.find(t => t.id === {task_id}))")
    page.evaluate(f"selectTask({task_id})")
    page.wait_for_selector('[data-testid="suggestion-signal"]', state="attached")
    page.evaluate(
        f"""
        const t = tasks.find(x => x.id === {task_id});
        t.waiting_signal = {json.dumps({"signal": "none", "activity": activity})};
        renderDetailPane(t);
        """
    )


class TestTheSuggestionCardIsHonest:
    def test_a_verdict_is_shown(self, page: Page, base_url):
        task_id = _seed(page, base_url)
        try:
            _open(page, base_url, task_id, _activity(
                status="likely_resolved", summary="Aarti sent the PUID list"))
            text = page.get_by_test_id("suggestion-signal").inner_text().lower()
            assert "likely done" in text, text
        finally:
            _delete(page, base_url, task_id)

    def test_a_failed_check_says_so(self, page: Page, base_url):
        task_id = _seed(page, base_url)
        try:
            _open(page, base_url, task_id, _activity(
                check_state="failed",
                error="WorkIQ returned no readable output",
                previous={"status": "still_pending", "summary": "no reply yet",
                          "checked_at": "2026-08-20T10:00:00Z"}))
            text = page.get_by_test_id("suggestion-signal").inner_text().lower()
            assert "couldn't check" in text or "could not check" in text, text
        finally:
            _delete(page, base_url, task_id)

    def test_a_failed_check_never_renders_undefined(self, page: Page, base_url):
        """The literal string a missing status used to produce."""
        task_id = _seed(page, base_url)
        try:
            _open(page, base_url, task_id, _activity(
                check_state="failed", error="timed out"))
            card = page.locator(".waiting-activity-card").inner_text().lower()
            assert "undefined" not in card, card
        finally:
            _delete(page, base_url, task_id)

    def test_a_failed_check_does_not_offer_dismiss_as_done(self, page: Page, base_url):
        """Dismissing on a check that never ran would discard a live suggestion."""
        task_id = _seed(page, base_url)
        try:
            _open(page, base_url, task_id, _activity(
                check_state="failed", error="timed out",
                previous={"status": "likely_resolved", "summary": "looked done",
                          "checked_at": "2026-08-20T10:00:00Z"}))
            card = page.locator(".waiting-activity-card").inner_text().lower()
            assert "already done" not in card, card
        finally:
            _delete(page, base_url, task_id)

    def test_an_earlier_verdict_is_labelled_as_earlier(self, page: Page, base_url):
        task_id = _seed(page, base_url)
        try:
            _open(page, base_url, task_id, _activity(
                check_state="failed", error="timed out",
                previous={"status": "still_pending", "summary": "no reply yet",
                          "checked_at": "2026-08-20T10:00:00Z"}))
            card = page.locator(".waiting-activity-card").inner_text().lower()
            assert "earlier result" in card, card
            assert "no reply yet" in card, card
        finally:
            _delete(page, base_url, task_id)

    def test_the_row_badge_is_hidden_when_nobody_checked(self, page: Page, base_url):
        """A badge is a finding. A failure has none, so it must not show one."""
        task_id = _seed(page, base_url, title="Badge probe")
        try:
            _open(page, base_url, task_id, _activity(
                check_state="failed", error="timed out",
                previous={"status": "likely_resolved", "summary": "looked done",
                          "checked_at": "2026-08-20T10:00:00Z"}))
            page.evaluate("renderTaskList()")
            page.wait_for_timeout(300)
            badge = page.evaluate(
                f"""() => {{
                    const row = document.querySelector('.task-row[data-id="{task_id}"]');
                    if (!row) return null;
                    const el = row.querySelector('.suggestion-check-badge');
                    return el ? el.textContent : '';
                }}"""
            )
            assert badge is not None, "task row not found"
            assert "done" not in (badge or "").lower(), badge
        finally:
            _delete(page, base_url, task_id)
