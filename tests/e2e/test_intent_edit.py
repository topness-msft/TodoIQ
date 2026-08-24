"""E2E gate for edit state surviving background re-renders.

Reported from the dogfood 2026-08-04: editing "Asking Cowork to:" drops out of
edit mode every 2-3 seconds, which matches CW_POLL_MS.

`renderDetailPane` already defers while the user is typing, but it decided that
by checking `activeEl.classList.contains('coaching-edit')` - and the intent
textarea carries `class="cw-intent-box"`. Only its *id* starts with
`coaching-edit-`. The class existed nowhere in the codebase, so the guard had
been silently dead for that field since the Cowork card was introduced.

Keying a guard on one hard-coded class is what allowed it to rot unnoticed, so
these tests assert the general behaviour - any text field inside the pane - not
the specific class.
"""

import json

from playwright.sync_api import Page, expect


def _seed(page: Page, base_url: str) -> int:
    # The Cowork card is gated on a parsed task (4aa3bad, "Keep Cowork
    # interactions user-driven"): an unparsed task renders no card at all, so
    # there is no intent line to edit. This test predates that gate and seeded
    # an unparsed task, which is why all six cases sat on a 30s selector
    # timeout. Every other e2e file already seeds parse_status="parsed".
    response = page.request.post(
        f"{base_url}/api/tasks",
        data={
            "title": "Intent edit probe",
            "description": "d",
            "parse_status": "parsed",
        },
    )
    assert response.ok, response.text()
    task_id = response.json()["task"]["id"]
    updated = page.request.put(
        f"{base_url}/api/tasks/{task_id}",
        data={"coaching_text": "Original intent text"},
    )
    assert updated.ok, updated.text()
    return task_id


def _delete(page: Page, base_url: str, task_id: int) -> None:
    page.request.delete(f"{base_url}/api/tasks/{task_id}")


def _open(page: Page, base_url: str, task_id: int) -> None:
    page.goto(base_url + "/")
    page.wait_for_function(f"Boolean(tasks.find(t => t.id === {task_id}))")
    page.evaluate(f"selectTask({task_id})")
    page.wait_for_selector(f"#coaching-edit-{task_id}", state="attached")


def _rerender(page: Page, task_id: int) -> None:
    """Exactly what the Cowork poller does every CW_POLL_MS."""
    page.evaluate(
        f"renderDetailPane(tasks.find(t => t.id === {task_id}))"
    )


class TestIntentEditSurvivesRerender:
    def test_edit_mode_survives_a_background_rerender(self, page: Page, base_url):
        task_id = _seed(page, base_url)
        try:
            _open(page, base_url, task_id)
            page.evaluate(f"toggleCoachingEdit({task_id})")
            box = page.locator(f"#coaching-edit-{task_id}")
            expect(box).to_be_visible()

            _rerender(page, task_id)

            expect(box).to_be_visible()
        finally:
            _delete(page, base_url, task_id)

    def test_typed_text_is_not_discarded(self, page: Page, base_url):
        task_id = _seed(page, base_url)
        try:
            _open(page, base_url, task_id)
            page.evaluate(f"toggleCoachingEdit({task_id})")
            box = page.locator(f"#coaching-edit-{task_id}")
            box.fill("Half-written replacement intent")

            _rerender(page, task_id)

            expect(box).to_have_value("Half-written replacement intent")
        finally:
            _delete(page, base_url, task_id)

    def test_focus_is_retained(self, page: Page, base_url):
        task_id = _seed(page, base_url)
        try:
            _open(page, base_url, task_id)
            page.evaluate(f"toggleCoachingEdit({task_id})")
            page.locator(f"#coaching-edit-{task_id}").click()

            _rerender(page, task_id)

            self_id = page.evaluate("document.activeElement && document.activeElement.id")
            assert self_id == f"coaching-edit-{task_id}", self_id
        finally:
            _delete(page, base_url, task_id)

    def test_repeated_rerenders_do_not_accumulate_damage(self, page: Page, base_url):
        """The poller fires continuously, not once."""
        task_id = _seed(page, base_url)
        try:
            _open(page, base_url, task_id)
            page.evaluate(f"toggleCoachingEdit({task_id})")
            box = page.locator(f"#coaching-edit-{task_id}")
            box.fill("Still typing")
            for _ in range(5):
                _rerender(page, task_id)
            expect(box).to_be_visible()
            expect(box).to_have_value("Still typing")
        finally:
            _delete(page, base_url, task_id)

    def test_deferred_render_is_applied_after_blur(self, page: Page, base_url):
        """Deferring must not mean dropping: the pane has to catch up."""
        task_id = _seed(page, base_url)
        try:
            _open(page, base_url, task_id)
            page.evaluate(f"toggleCoachingEdit({task_id})")
            page.locator(f"#coaching-edit-{task_id}").click()
            page.evaluate(
                f"""
                const t = tasks.find(t => t.id === {task_id});
                t.title = 'Renamed while editing';
                renderDetailPane(t);
                """
            )
            page.evaluate("document.activeElement.blur()")
            expect(page.locator(f"#title-display-{task_id}")).to_have_text(
                "Renamed while editing"
            )
        finally:
            _delete(page, base_url, task_id)

    def test_rerender_still_happens_when_not_editing(self, page: Page, base_url):
        """The guard must not block ordinary updates."""
        task_id = _seed(page, base_url)
        try:
            _open(page, base_url, task_id)
            page.evaluate(
                f"""
                const t = tasks.find(t => t.id === {task_id});
                t.title = 'Updated with no field focused';
                renderDetailPane(t);
                """
            )
            expect(page.locator(f"#title-display-{task_id}")).to_have_text(
                "Updated with no field focused"
            )
        finally:
            _delete(page, base_url, task_id)
