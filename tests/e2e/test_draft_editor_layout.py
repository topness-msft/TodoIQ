"""E2E gate on the draft editor filling its card.

Reported 2026-08-05: the Cowork draft edit box renders as a narrow column
instead of spanning the card.

`.cw-draft` is used for both the rendered draft (a `div`) and the editor (a
`textarea`). The rule set never declares a width, which is invisible for a div
because a block element fills its container, but a textarea falls back to its
default `cols` width of roughly 20 characters.

`/todo` already carried `textarea.cw-draft { width: 100%; font: inherit; }`;
only the dashboard stylesheet was missing it, so this is the same
divergence-between-the-two-surfaces class of bug as the intent edit guard.
"""

import json

from playwright.sync_api import Page, expect


CONV = "tenant:user:cw-drafteditor"
DRAFT = (
    "Hi Saurabh - picking up a thread from last September. After the Adaptive "
    "Leadership Awareness workshop landed really well with Power CAT, you "
    "mentioned it might be worth bringing to CAPE."
)


def _seed(page: Page, base_url: str) -> int:
    response = page.request.post(
        f"{base_url}/api/tasks", data={"title": "Draft editor layout probe"}
    )
    assert response.ok, response.text()
    return response.json()["task"]["id"]


def _delete(page: Page, base_url: str, task_id: int) -> None:
    page.request.delete(f"{base_url}/api/tasks/{task_id}")


def _open_ready(page: Page, base_url: str, task_id: int) -> None:
    action = {
        "id": 9101,
        "task_id": task_id,
        "state": "ready",
        "finding": "Found the thread.",
        "draft": DRAFT,
        "destination_kind": "one_to_one",
        "conversation_id": CONV,
        "is_broadcast": False,
        "seen_at": "2026-08-05T12:00:00Z",
    }
    page.goto(base_url + "/")
    page.wait_for_function(f"Boolean(tasks.find(t => t.id === {task_id}))")
    page.evaluate(
        f"""
        _cwActions[{task_id}] = {json.dumps(action)};
        selectedTaskId = {task_id};
        renderDetailPane(tasks.find(t => t.id === {task_id}));
        """
    )


class TestDraftEditorLayout:
    def test_editor_spans_the_card(self, page: Page, base_url):
        task_id = _seed(page, base_url)
        try:
            _open_ready(page, base_url, task_id)
            page.get_by_role("button", name="Edit").click()
            box = page.locator(f"#cw-draft-{task_id}")
            expect(box).to_be_visible()

            widths = page.evaluate(
                f"""() => {{
                    const ta = document.getElementById('cw-draft-{task_id}');
                    const card = ta.closest('.cw-card');
                    return {{ ta: ta.getBoundingClientRect().width,
                              card: card.getBoundingClientRect().width }};
                }}"""
            )
            # Allow for card padding, but reject the ~150px cols default.
            assert widths["ta"] > widths["card"] * 0.8, widths
        finally:
            _delete(page, base_url, task_id)

    def test_editor_matches_the_rendered_draft_width(self, page: Page, base_url):
        """Entering edit mode must not change the column the text sits in."""
        task_id = _seed(page, base_url)
        try:
            _open_ready(page, base_url, task_id)
            rendered = page.evaluate(
                "document.querySelector('.cw-draft').getBoundingClientRect().width"
            )
            page.get_by_role("button", name="Edit").click()
            edited = page.evaluate(
                f"document.getElementById('cw-draft-{task_id}')"
                ".getBoundingClientRect().width"
            )
            assert abs(edited - rendered) < 8, (rendered, edited)
        finally:
            _delete(page, base_url, task_id)

    def test_editor_does_not_overflow_the_card(self, page: Page, base_url):
        """width:100% without border-box would push past the padding."""
        task_id = _seed(page, base_url)
        try:
            _open_ready(page, base_url, task_id)
            page.get_by_role("button", name="Edit").click()
            fits = page.evaluate(
                f"""() => {{
                    const ta = document.getElementById('cw-draft-{task_id}');
                    const card = ta.closest('.cw-card');
                    const a = ta.getBoundingClientRect();
                    const b = card.getBoundingClientRect();
                    return a.left >= b.left - 1 && a.right <= b.right + 1;
                }}"""
            )
            assert fits
        finally:
            _delete(page, base_url, task_id)

    def test_editor_uses_the_card_font_not_monospace(self, page: Page, base_url):
        """A bare textarea defaults to monospace, which looks nothing like the draft."""
        task_id = _seed(page, base_url)
        try:
            _open_ready(page, base_url, task_id)
            page.get_by_role("button", name="Edit").click()
            fonts = page.evaluate(
                f"""() => {{
                    const ta = document.getElementById('cw-draft-{task_id}');
                    const card = ta.closest('.cw-card');
                    return {{ ta: getComputedStyle(ta).fontFamily,
                              card: getComputedStyle(card).fontFamily }};
                }}"""
            )
            assert "monospace" not in fonts["ta"].lower(), fonts
            assert fonts["ta"] == fonts["card"], fonts
        finally:
            _delete(page, base_url, task_id)
