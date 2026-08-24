"""Visual gates for engine-aware marks and the Teams destination link.

Two behaviours are pinned here:

1. The mark shown beside a task names the engine actually doing the work.
   The card header already said "WorkIQ" while the icon was always Cowork's,
   so the two could contradict each other in front of the user.

2. A structured Teams send goes straight to the confirmation dialog, and that
   dialog offers the conversation as a link. The destination picker used to
   appear first showing a raw "19:...@thread.v2" chat id -- unverifiable by
   eye, and uneditable in any case because the server refuses to change a
   destination the preview resolved.
"""
import os

from playwright.sync_api import Page, expect


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
TEMP_DIR = os.path.join(PROJECT_ROOT, "temp", "engine-marks")
os.makedirs(TEMP_DIR, exist_ok=True)

CHAT_ID = "19:bf7c91898b9c408383b3cf3f1f3cf3a4@thread.v2"


def _seed(page: Page, base_url: str, title: str) -> int:
    response = page.request.post(base_url + "/api/tasks", data={"title": title})
    assert response.ok, response.text()
    return response.json()["task"]["id"]


def _delete(page: Page, base_url: str, task_id: int) -> None:
    page.request.delete(f"{base_url}/api/tasks/{task_id}")


def test_running_mark_names_the_engine_doing_the_work(page: Page, base_url: str):
    page.set_viewport_size({"width": 1280, "height": 900})
    task_id = _seed(page, base_url, "Engine mark gate")
    try:
        page.goto(base_url + "/")
        page.wait_for_function(
            f"typeof tasks !== 'undefined' && tasks.some(t => t.id === {task_id})"
        )
        row = page.locator(f'.task-row[data-id="{task_id}"]')

        # A WorkIQ-routed task pulses the Copilot mark.
        page.evaluate(
            """taskId => {
                const task = tasks.find(t => t.id === taskId);
                task.action_type = 'teams-message';
                task.source_type = 'manual';
                task.parse_status = 'parsed';
                task.cw_state = 'previewing';
                task.cw_seen_at = null;
                renderTaskList();
            }""",
            task_id,
        )
        expect(row.locator(".cw-status-running")).to_be_visible()
        expect(
            row.locator('.cw-status-running img[src="/static/img/copilot.png"]')
        ).to_have_count(1)
        expect(
            row.locator('.cw-status-running img[src="/static/img/coworker.svg"]')
        ).to_have_count(0)
        # It must actually pulse, not merely be present.
        assert row.locator(".cw-status-running").evaluate(
            "node => getComputedStyle(node).animationName"
        ) != "none"
        page.screenshot(
            path=os.path.join(TEMP_DIR, "row-workiq-mark.png"), full_page=False
        )

        # A Cowork-routed task keeps the Cowork mark.
        page.evaluate(
            """taskId => {
                const task = tasks.find(t => t.id === taskId);
                task.action_type = 'general';
                task.source_type = 'manual';
                task.cw_state = 'previewing';
                renderTaskList();
            }""",
            task_id,
        )
        expect(
            row.locator('.cw-status-running img[src="/static/img/coworker.svg"]')
        ).to_have_count(1)
        expect(
            row.locator('.cw-status-running img[src="/static/img/copilot.png"]')
        ).to_have_count(0)
        page.screenshot(
            path=os.path.join(TEMP_DIR, "row-cowork-mark.png"), full_page=False
        )
    finally:
        _delete(page, base_url, task_id)


def test_structured_teams_confirm_links_the_conversation(page: Page, base_url: str):
    page.set_viewport_size({"width": 1280, "height": 900})
    task_id = _seed(page, base_url, "Teams destination link gate")
    try:
        page.goto(base_url + "/")
        page.wait_for_function(
            f"typeof tasks !== 'undefined' && tasks.some(t => t.id === {task_id})"
        )
        page.evaluate(
            """([taskId, chatId]) => {
                const task = tasks.find(t => t.id === taskId);
                task.parse_status = 'parsed';
                task.action_type = 'teams-message';
                task.source_type = 'manual';
                selectedTaskId = taskId;
                _cwActions[taskId] = {
                    id: 901,
                    task_id: taskId,
                    action_type: 'teams-message',
                    state: 'ready',
                    delivery_channel: 'teams',
                    structured_payload: JSON.stringify({
                        schema_version: 1, channel: 'teams',
                        chat_id: chatId, body: 'Approved body'
                    }),
                    draft: 'Approved body',
                    destination_ref: chatId,
                    destination_display: 'Riveter delivery test (safe to delete)',
                    destination_source: 'workiq_preview',
                    destination_confirmed_at: '2026-08-23T13:14:14Z'
                };
                renderDetailPane(task);
                cwOpenExecuteConfirm(taskId);
            }""",
            [task_id, CHAT_ID],
        )

        modal = page.get_by_test_id("execute-confirmation")
        expect(modal).to_be_visible()
        # The raw chat id must not be what the user is asked to verify.
        expect(modal).to_contain_text("Riveter delivery test (safe to delete)")

        link = page.get_by_test_id("teams-destination-link")
        expect(link).to_be_visible()
        href = link.get_attribute("href")
        assert href.startswith("https://teams.microsoft.com/l/chat/"), href
        # Matches the webUrl Microsoft Graph reports for this chat.
        assert "19%3Abf7c91898b9c408383b3cf3f1f3cf3a4%40thread.v2" in href, href
        assert link.get_attribute("target") == "_blank"
        assert "noopener" in (link.get_attribute("rel") or "")

        page.screenshot(
            path=os.path.join(TEMP_DIR, "teams-confirm-with-link.png"),
            full_page=False,
        )
    finally:
        _delete(page, base_url, task_id)


def test_structured_teams_skips_the_destination_picker(page: Page, base_url: str):
    """An unconfirmed structured destination must not raise the picker."""
    page.set_viewport_size({"width": 1280, "height": 900})
    task_id = _seed(page, base_url, "Teams picker skip gate")
    try:
        page.goto(base_url + "/")
        page.wait_for_function(
            f"typeof tasks !== 'undefined' && tasks.some(t => t.id === {task_id})"
        )
        page.evaluate(
            """([taskId, chatId]) => {
                const task = tasks.find(t => t.id === taskId);
                task.parse_status = 'parsed';
                task.action_type = 'teams-message';
                selectedTaskId = taskId;
                window.__confirmedDest = null;
                // Record the confirm call instead of letting it reach the server.
                window.cwConfirmDest = function(id, cont) {
                    window.__confirmedDest = {id: id, cont: cont};
                };
                _cwActions[taskId] = {
                    id: 902,
                    task_id: taskId,
                    action_type: 'teams-message',
                    state: 'ready',
                    delivery_channel: 'teams',
                    structured_payload: JSON.stringify({
                        schema_version: 1, channel: 'teams', chat_id: chatId
                    }),
                    draft: 'Approved body',
                    destination_ref: chatId,
                    destination_display: 'Riveter delivery test (safe to delete)',
                    destination_source: 'workiq_preview',
                    destination_confirmed_at: null
                };
                renderDetailPane(task);
                cwOpenExecuteConfirm(taskId);
            }""",
            [task_id, CHAT_ID],
        )
        # It auto-confirms rather than showing an uneditable picker.
        assert page.evaluate("window.__confirmedDest") == {
            "id": task_id, "cont": True
        }
        expect(page.locator("#dest-modal-ref")).to_have_count(0)
    finally:
        _delete(page, base_url, task_id)
