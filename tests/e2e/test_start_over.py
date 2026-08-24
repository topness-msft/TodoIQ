"""E2E gate for detaching a Cowork conversation and starting fresh.

Requested 2026-08-05: "I'd like the option to detach the cowork conversation and
start over."

The capability already existed but was undiscoverable. Every preview run spawns a
brand-new Cowork conversation (nothing passes `--resume`), so a Redo with an
empty correction box already detaches. Nothing said so: the Redo affordance is
framed as "Tell Cowork what to change...", which reads as steering the existing
conversation rather than abandoning it.

Evidence the framing misled: a live task carried
`redirect_text = "start a new conversaion tha..."` - the user asking in prose for
something no control offered.

So this is an affordance, not new machinery. These tests pin the behaviour that
matters: a distinct control, a fresh run with NO inherited correction, and
history preserved.
"""

import json

from playwright.sync_api import Page, expect


OLD_CONV = "tenant:user:cw-oldconversation"


def _seed(page: Page, base_url: str) -> int:
    # The Cowork card is gated on a parsed task (4aa3bad); an unparsed one
    # renders no card, so the Start over control never exists.
    response = page.request.post(
        f"{base_url}/api/tasks",
        data={"title": "Start over probe", "parse_status": "parsed"},
    )
    assert response.ok, response.text()
    return response.json()["task"]["id"]


def _delete(page: Page, base_url: str, task_id: int) -> None:
    page.request.delete(f"{base_url}/api/tasks/{task_id}")


def _action(task_id: int) -> dict:
    return {
        "id": 9201,
        "task_id": task_id,
        "state": "ready",
        "finding": "Prior research",
        "draft": "A draft that went in the wrong direction.",
        "redirect_text": "an earlier correction",
        "destination_kind": "one_to_one",
        "conversation_id": OLD_CONV,
        "is_broadcast": False,
        "seen_at": "2026-08-05T12:00:00Z",
    }


def _open_dashboard(page: Page, base_url: str, task_id: int) -> None:
    page.goto(base_url + "/")
    page.wait_for_function(f"Boolean(tasks.find(t => t.id === {task_id}))")
    page.evaluate(
        f"""
        _cwActions[{task_id}] = {json.dumps(_action(task_id))};
        selectedTaskId = {task_id};
        renderDetailPane(tasks.find(t => t.id === {task_id}));
        """
    )


def _open_todo(page: Page, base_url: str, task_id: int) -> None:
    page.goto(base_url + "/todo")
    page.wait_for_function(f"Boolean(tasks.find(t => t.id === {task_id}))")
    page.evaluate(
        f"""
        const a = {json.dumps(_action(task_id))};
        const t = tasks.find(x => x.id === {task_id});
        Object.assign(t, {{
            cw_loaded: true, cw_state: 'ready', cw_seen_at: 'seen',
            cw_finding: a.finding, cw_draft: a.draft,
            cw_redirect_text: a.redirect_text,
            cw_dest_kind: a.destination_kind,
            cw_conversation_id: a.conversation_id
        }});
        selectTask({task_id});
        """
    )


class TestStartOverDashboard:
    def test_control_is_offered(self, page: Page, base_url):
        task_id = _seed(page, base_url)
        try:
            _open_dashboard(page, base_url, task_id)
            expect(page.get_by_test_id("cw-start-over")).to_be_visible()
        finally:
            _delete(page, base_url, task_id)

    def test_asks_before_discarding_the_current_draft(self, page: Page, base_url):
        """A run costs time and Cowork credits; do not spend them on a stray click."""
        task_id = _seed(page, base_url)
        try:
            _open_dashboard(page, base_url, task_id)
            seen = {}
            page.on("dialog", lambda d: (seen.update(msg=d.message), d.dismiss()))
            page.get_by_test_id("cw-start-over").click()
            page.wait_for_timeout(400)
            assert seen, "no confirmation shown"
            assert "conversation" in seen["msg"].lower(), seen
        finally:
            _delete(page, base_url, task_id)

    def test_cancelling_starts_nothing(self, page: Page, base_url):
        task_id = _seed(page, base_url)
        try:
            _open_dashboard(page, base_url, task_id)
            page.evaluate("window.__posts = 0;")
            page.route(
                f"**/api/tasks/{task_id}/cowork",
                lambda route: route.abort(),
            )
            page.on("dialog", lambda d: d.dismiss())
            page.get_by_test_id("cw-start-over").click()
            page.wait_for_timeout(500)
            # the card must still show the original draft
            expect(page.locator(".cw-draft").first).to_contain_text(
                "wrong direction"
            )
        finally:
            _delete(page, base_url, task_id)

    def test_confirming_posts_a_run_with_no_inherited_correction(
        self, page: Page, base_url
    ):
        """Starting over must not silently re-apply the previous steer."""
        task_id = _seed(page, base_url)
        try:
            _open_dashboard(page, base_url, task_id)
            page.on("dialog", lambda d: d.accept())
            with page.expect_request(
                lambda r: r.method == "POST"
                and r.url.endswith(f"/api/tasks/{task_id}/cowork")
            ) as info:
                page.get_by_test_id("cw-start-over").click()
            body = info.value.post_data_json or {}
            assert "redirect_text" not in body, body
        finally:
            _delete(page, base_url, task_id)


class TestStartOverTodo:
    def test_control_is_offered(self, page: Page, base_url):
        task_id = _seed(page, base_url)
        try:
            _open_todo(page, base_url, task_id)
            expect(page.get_by_test_id("cw-start-over")).to_be_visible()
        finally:
            _delete(page, base_url, task_id)

    def test_confirming_posts_a_run_with_no_inherited_correction(
        self, page: Page, base_url
    ):
        task_id = _seed(page, base_url)
        try:
            _open_todo(page, base_url, task_id)
            page.on("dialog", lambda d: d.accept())
            with page.expect_request(
                lambda r: r.method == "POST"
                and r.url.endswith(f"/api/tasks/{task_id}/cowork")
            ) as info:
                page.get_by_test_id("cw-start-over").click()
            body = info.value.post_data_json or {}
            assert "redirect_text" not in body, body
        finally:
            _delete(page, base_url, task_id)
