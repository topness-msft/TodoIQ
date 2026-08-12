"""E2E gate on the task list and detail pane agreeing.

Reported 2026-08-05 for task 2182: the parse finished, the detail pane showed
the new title "Research Jay Steinke's customer portfolio" with a Parsed badge,
and the list row still read "Research Jay's customers" - the pre-parse
`raw_input`.

Why the two can diverge: parsing is done by an external command writing straight
to SQLite, so no `task_updated` WebSocket broadcast fires. The only sync path is
`pollParseStatus`, which runs solely while the LOCAL array still believes a task
is pending. If the local array misses the transition for any reason, it stays
stale forever - and `selectTask` fetches fresh data for the DETAIL PANE ONLY,
never writing it back to `tasks` or re-rendering the list.

So clicking a task showed correct detail beside a stale row, indefinitely.
Clicking a task is the natural moment to reconcile, which is what these tests
pin.
"""

from playwright.sync_api import Page, expect


FRESH_TITLE = "Research Jay Steinke's customer portfolio"
STALE_TITLE = "Research Jay's customers"


def _seed(page: Page, base_url: str) -> int:
    """A task whose server-side state is already the parsed one."""
    response = page.request.post(
        f"{base_url}/api/tasks",
        data={"title": FRESH_TITLE, "description": "Portfolio research."},
    )
    assert response.ok, response.text()
    return response.json()["task"]["id"]


def _delete(page: Page, base_url: str, task_id: int) -> None:
    page.request.delete(f"{base_url}/api/tasks/{task_id}")


def _load_with_stale_row(page: Page, base_url: str, task_id: int) -> None:
    """Reproduce the reported state: server parsed, local array pre-parse."""
    page.goto(base_url + "/")
    page.wait_for_function(f"Boolean(tasks.find(t => t.id === {task_id}))")
    page.evaluate(
        f"""
        const t = tasks.find(x => x.id === {task_id});
        t.title = {STALE_TITLE!r};
        t.parse_status = 'parsed';   // poller will not re-fetch: nothing pending
        renderTaskList();
        """
    )


def _row(page: Page, task_id: int):
    return page.locator(f'.task-row[data-id="{task_id}"] .task-title')


class TestListReconcilesOnSelect:
    def test_precondition_row_is_stale(self, page: Page, base_url):
        """Guard the fixture itself, so a passing suite cannot be vacuous."""
        task_id = _seed(page, base_url)
        try:
            _load_with_stale_row(page, base_url, task_id)
            expect(_row(page, task_id)).to_have_text(STALE_TITLE)
        finally:
            _delete(page, base_url, task_id)

    def test_selecting_the_task_refreshes_its_row(self, page: Page, base_url):
        task_id = _seed(page, base_url)
        try:
            _load_with_stale_row(page, base_url, task_id)
            page.evaluate(f"selectTask({task_id})")
            expect(_row(page, task_id)).to_have_text(FRESH_TITLE)
        finally:
            _delete(page, base_url, task_id)

    def test_detail_and_row_agree_after_select(self, page: Page, base_url):
        """The actual complaint: the two panes disagreed."""
        task_id = _seed(page, base_url)
        try:
            _load_with_stale_row(page, base_url, task_id)
            page.evaluate(f"selectTask({task_id})")
            expect(page.locator(f"#title-display-{task_id}")).to_have_text(
                FRESH_TITLE
            )
            expect(_row(page, task_id)).to_have_text(FRESH_TITLE)
        finally:
            _delete(page, base_url, task_id)

    def test_in_memory_task_is_updated_not_just_the_markup(
        self, page: Page, base_url
    ):
        """A markup-only patch would be undone by the next render."""
        task_id = _seed(page, base_url)
        try:
            _load_with_stale_row(page, base_url, task_id)
            page.evaluate(f"selectTask({task_id})")
            expect(_row(page, task_id)).to_have_text(FRESH_TITLE)
            page.evaluate("renderTaskList()")
            expect(_row(page, task_id)).to_have_text(FRESH_TITLE)
        finally:
            _delete(page, base_url, task_id)

    def test_selection_highlight_survives_the_refresh(self, page: Page, base_url):
        """Re-rendering the list must not drop the selected styling."""
        task_id = _seed(page, base_url)
        try:
            _load_with_stale_row(page, base_url, task_id)
            page.evaluate(f"selectTask({task_id})")
            expect(_row(page, task_id)).to_have_text(FRESH_TITLE)
            expect(
                page.locator(f'.task-row[data-id="{task_id}"]')
            ).to_have_class(__import__("re").compile(r"\bselected\b"))
        finally:
            _delete(page, base_url, task_id)
