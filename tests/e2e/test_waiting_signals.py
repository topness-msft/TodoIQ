"""What a waiting card is allowed to claim.

Riveter's rule is that it must never present something as verified when it was
not. Two things in the waiting card broke that rule:

1. `/waiting-check` skips a task entirely when WorkIQ errors
   (.claude/commands/waiting-check.md:79). Nothing is written, so the card goes
   on showing the previous answer with its original timestamp. "I could not
   look" was rendered as "I looked, on that date, and here is what I found".

2. `may_be_resolved` - an LLM's inference from reading a thread - was rendered
   with a green tick, the same symbol the product uses for a completed task.
   The status name hedges; the icon did not. A glance at the list said "done".

These tests pin the corrected behaviour: three visually distinct states, a
"looks done" signal that reads as a question rather than a conclusion, evidence
shown so the user can judge the claim, and no path from any of it to a task
completing itself.

The server derives `waiting_signal` (src/models.py `_row_to_dict`), so these
seed it exactly as the API emits it and exercise the renderer.
"""

import json

from playwright.sync_api import Page, expect


def _seed(page: Page, base_url: str, title="Waiting probe") -> int:
    response = page.request.post(
        f"{base_url}/api/tasks",
        data={"title": title, "status": "waiting", "parse_status": "parsed"},
    )
    assert response.ok, response.text()
    return response.json()["task"]["id"]


def _delete(page: Page, base_url: str, task_id: int) -> None:
    page.request.delete(f"{base_url}/api/tasks/{task_id}")


def _open_with_signal(page: Page, base_url: str, task_id: int, signal: dict) -> None:
    """Render the detail pane for a task carrying a server-shaped signal.

    Selection happens FIRST: selectTask reconciles the row against the server
    (tests/e2e/test_list_detail_sync.py pins that), which would overwrite an
    injected field. Assign after it has settled, then render.
    """
    page.goto(base_url + "/")
    page.wait_for_function(f"Boolean(tasks.find(t => t.id === {task_id}))")
    page.evaluate(f"selectTask({task_id})")
    # Wait for the card to exist rather than sleeping: the reconcile is async,
    # and a fixed delay lost the race on a cold first run.
    page.wait_for_selector('[data-testid="waiting-signal"]', state="attached")
    page.evaluate(
        f"""
        const t = tasks.find(x => x.id === {task_id});
        t.waiting_signal = {json.dumps(signal)};
        renderDetailPane(t);
        """
    )


def _sig(signal, **activity):
    base = {
        "version": 2, "check_state": "ok", "status": None, "summary": None,
        "checked_at": "2026-08-24T09:00:00Z", "check_since": None,
        "return_date": None, "error": None, "producer": "waiting-check",
        "source_scope": "person", "conversation_id": None, "evidence": [],
        "previous": None,
    }
    base.update(activity)
    return {"signal": signal, "activity": base}


class TestTheThreeStatesAreDistinguishable:
    def test_new_activity_is_announced(self, page: Page, base_url):
        task_id = _seed(page, base_url)
        try:
            _open_with_signal(page, base_url, task_id, _sig(
                "activity", status="activity_detected",
                summary="Jason replied on Tuesday"))
            card = page.get_by_test_id("waiting-signal")
            expect(card).to_be_visible()
            assert "new activity" in card.inner_text().lower()
        finally:
            _delete(page, base_url, task_id)

    def test_looks_done_does_not_claim_to_be_done(self, page: Page, base_url):
        """The whole point: a hedge in the label, and no completion tick."""
        task_id = _seed(page, base_url)
        try:
            _open_with_signal(page, base_url, task_id, _sig(
                "looks_done", status="may_be_resolved",
                summary="Jason sent the signed copy"))
            text = page.get_by_test_id("waiting-signal").inner_text()
            assert "looks done" in text.lower(), text
            # A green tick is how this product says "completed". Using it for an
            # inference is the claim we are refusing to make.
            assert "\u2705" not in text, text
        finally:
            _delete(page, base_url, task_id)

    def test_a_failed_check_says_so_instead_of_showing_the_old_answer(
            self, page: Page, base_url):
        task_id = _seed(page, base_url)
        try:
            _open_with_signal(page, base_url, task_id, _sig(
                "check_failed", check_state="failed",
                error="WorkIQ returned no readable output",
                previous={"status": "may_be_resolved",
                          "summary": "Looked resolved on the 20th",
                          "checked_at": "2026-08-20T10:00:00Z"}))
            text = page.get_by_test_id("waiting-signal").inner_text().lower()
            assert "couldn't check" in text or "could not check" in text, text
            # The previous finding may be shown, but never as this check's result.
            assert "looks done" not in text, text
        finally:
            _delete(page, base_url, task_id)

    def test_a_failed_check_is_not_mistaken_for_silence(self, page: Page, base_url):
        """`no_activity` and `check_failed` are the pair that must never merge."""
        task_id = _seed(page, base_url)
        try:
            _open_with_signal(page, base_url, task_id, _sig(
                "check_failed", check_state="failed", error="timed out"))
            failed = page.get_by_test_id("waiting-signal").inner_text().lower()
            _open_with_signal(page, base_url, task_id, _sig(
                "quiet", status="no_activity", summary="No response since the 1st"))
            quiet = page.get_by_test_id("waiting-signal").inner_text().lower()
            assert failed != quiet, (failed, quiet)
        finally:
            _delete(page, base_url, task_id)

    def test_an_unchecked_task_still_offers_the_check(self, page: Page, base_url):
        task_id = _seed(page, base_url)
        try:
            _open_with_signal(page, base_url, task_id, _sig("unchecked"))
            expect(page.locator("#check-now-btn")).to_be_visible()
        finally:
            _delete(page, base_url, task_id)


class TestEvidenceIsShown:
    """A summary is an assertion; the excerpts are what make it checkable."""

    def test_excerpts_are_rendered(self, page: Page, base_url):
        task_id = _seed(page, base_url)
        try:
            _open_with_signal(page, base_url, task_id, _sig(
                "activity", status="activity_detected", summary="two replies",
                evidence=[
                    {"excerpt": "Sending the numbers over now",
                     "when": "2026-08-21T09:00:00Z", "where": "Teams", "url": None},
                    {"excerpt": "Ignore my last, wrong file",
                     "when": "2026-08-22T09:00:00Z", "where": "Teams", "url": None},
                ]))
            evidence = page.get_by_test_id("waiting-evidence")
            expect(evidence).to_be_visible()
            text = evidence.inner_text()
            assert "Sending the numbers over now" in text
            assert "Ignore my last, wrong file" in text
        finally:
            _delete(page, base_url, task_id)

    def test_evidence_is_escaped_not_injected(self, page: Page, base_url):
        task_id = _seed(page, base_url)
        try:
            _open_with_signal(page, base_url, task_id, _sig(
                "activity", status="activity_detected", summary="s",
                evidence=[{"excerpt": "<img src=x onerror=alert(1)>",
                           "when": None, "where": None, "url": None}]))
            expect(page.get_by_test_id("waiting-evidence")).to_be_visible()
            assert page.evaluate(
                "document.querySelectorAll('[data-testid=waiting-evidence] img').length"
            ) == 0
        finally:
            _delete(page, base_url, task_id)

    def test_no_evidence_renders_no_empty_container(self, page: Page, base_url):
        task_id = _seed(page, base_url)
        try:
            _open_with_signal(page, base_url, task_id, _sig(
                "quiet", status="no_activity", summary="nothing since the 1st"))
            expect(page.get_by_test_id("waiting-evidence")).to_have_count(0)
        finally:
            _delete(page, base_url, task_id)


class TestScopeIsStatedHonestly:
    """"Nothing on this thread" and "nothing from this person" differ."""

    def test_a_person_scoped_check_does_not_claim_the_thread(self, page: Page, base_url):
        task_id = _seed(page, base_url)
        try:
            _open_with_signal(page, base_url, task_id, _sig(
                "quiet", status="no_activity", summary="No response",
                source_scope="person"))
            text = page.get_by_test_id("waiting-signal").inner_text().lower()
            assert "thread" not in text, text
        finally:
            _delete(page, base_url, task_id)

    def test_a_thread_scoped_check_may_say_so(self, page: Page, base_url):
        task_id = _seed(page, base_url)
        try:
            _open_with_signal(page, base_url, task_id, _sig(
                "quiet", status="no_activity", summary="No reply",
                source_scope="thread",
                conversation_id="19:abc@thread.v2"))
            text = page.get_by_test_id("waiting-signal").inner_text().lower()
            assert "thread" in text, text
        finally:
            _delete(page, base_url, task_id)


class TestNothingCompletesItself:
    def test_looks_done_leaves_the_task_waiting(self, page: Page, base_url):
        task_id = _seed(page, base_url)
        try:
            _open_with_signal(page, base_url, task_id, _sig(
                "looks_done", status="may_be_resolved", summary="appears sent"))
            page.wait_for_timeout(600)
            status = page.request.get(
                f"{base_url}/api/tasks/{task_id}").json()["task"]["status"]
            assert status == "waiting", status
        finally:
            _delete(page, base_url, task_id)


class TestTheListRowAgrees:
    """The row icon and the card must not tell different stories."""

    def test_looks_done_row_icon_is_not_a_completion_tick(self, page: Page, base_url):
        task_id = _seed(page, base_url, title="Row icon probe")
        try:
            _open_with_signal(page, base_url, task_id, _sig(
                "looks_done", status="may_be_resolved", summary="appears sent"))
            page.evaluate("renderTaskList()")
            page.wait_for_timeout(300)
            icon = page.evaluate(
                f"""() => {{
                    const row = document.querySelector('.task-row[data-id="{task_id}"]');
                    if (!row) return null;
                    const el = row.querySelector('.waiting-activity-icon');
                    return el ? el.textContent : '';
                }}"""
            )
            assert icon is not None, "task row not found"
            assert "\u2705" not in icon, icon
        finally:
            _delete(page, base_url, task_id)

    def test_a_failed_check_is_visible_in_the_row(self, page: Page, base_url):
        task_id = _seed(page, base_url, title="Row failure probe")
        try:
            _open_with_signal(page, base_url, task_id, _sig(
                "check_failed", check_state="failed", error="no output"))
            page.evaluate("renderTaskList()")
            page.wait_for_timeout(300)
            title = page.evaluate(
                f"""() => {{
                    const row = document.querySelector('.task-row[data-id="{task_id}"]');
                    if (!row) return null;
                    const el = row.querySelector('.waiting-activity-icon');
                    return el ? el.getAttribute('title') : '';
                }}"""
            )
            assert title, "no waiting icon rendered for a failed check"
            assert "check" in title.lower(), title
        finally:
            _delete(page, base_url, task_id)
