"""Regression tests for the Cowork Run again control."""

import json

import pytest
from playwright.sync_api import Page


def _seed_task(page: Page, base_url: str) -> int:
    response = page.request.post(
        f"{base_url}/api/tasks",
        data={
            "title": "Cowork Run again gate",
            "description": "Run again should never silently no-op",
            "action_type": "follow-up",
            # The Cowork card is gated on a parsed task (4aa3bad); an unparsed
            # one renders no card, so there is no Run again control.
            "parse_status": "parsed",
        },
    )
    assert response.ok
    return response.json()["task"]["id"]


def _delete_task(page: Page, base_url: str, task_id: int) -> None:
    page.request.delete(f"{base_url}/api/tasks/{task_id}")


def _preview_response(task_id: int) -> str:
    return json.dumps(
        {
            "action": {
                "id": task_id,
                "task_id": task_id,
                "state": "previewing",
                "created_at": "2026-07-31T19:00:00Z",
            }
        }
    )


@pytest.mark.parametrize("guidance", ["", "look for times next week"])
def test_dashboard_run_again_posts(page: Page, base_url, guidance):
    task_id = _seed_task(page, base_url)
    try:
        page.goto(base_url + "/")
        page.wait_for_function(
            f"Boolean(tasks.find(task => task.id === {task_id}))"
        )
        page.route(
            f"**/api/tasks/{task_id}/cowork",
            lambda route: route.fulfill(
                status=202,
                content_type="application/json",
                body=_preview_response(task_id),
            ),
        )
        page.evaluate(
            f"""
            _cwActions[{task_id}] = {{
                id: {task_id}, task_id: {task_id}, state: 'ready',
                finding: 'Found', draft: 'Draft', destination_kind: 'none'
            }};
            _cwRedo[{task_id}] = true;
            selectedTaskId = {task_id};
            renderDetailPane(tasks.find(task => task.id === {task_id}));
            """
        )
        page.locator(f"#cw-redo-{task_id}").fill(guidance)

        with page.expect_request(
            lambda request: request.method == "POST"
            and f"/api/tasks/{task_id}/cowork" in request.url
        ) as request_info:
            page.get_by_text("Run again", exact=True).click()

        body = request_info.value.post_data_json or {}
        if guidance:
            assert body["redirect_text"] == guidance
        else:
            assert "redirect_text" not in body
    finally:
        _delete_task(page, base_url, task_id)


@pytest.mark.parametrize("guidance", ["", "look for times next week"])
def test_todoiq_run_again_posts(page: Page, base_url, guidance):
    task_id = _seed_task(page, base_url)
    try:
        page.goto(base_url + "/todo")
        page.wait_for_function(
            f"Boolean(tasks.find(task => task.id === {task_id}))"
        )
        page.route(
            f"**/api/tasks/{task_id}/cowork",
            lambda route: route.fulfill(
                status=202,
                content_type="application/json",
                body=_preview_response(task_id),
            ),
        )
        page.evaluate(
            f"""
            const task = tasks.find(item => item.id === {task_id});
            Object.assign(task, {{
                cw_loaded: true, cw_state: 'ready', cw_seen_at: 'seen',
                cw_finding: 'Found', cw_draft: 'Draft',
                cw_dest_kind: 'none', cw_redo_open: true
            }});
            selectTask({task_id});
            """
        )
        page.locator(f"#cw-redo-{task_id}").fill(guidance)

        with page.expect_request(
            lambda request: request.method == "POST"
            and f"/api/tasks/{task_id}/cowork" in request.url
        ) as request_info:
            page.get_by_text("Run again", exact=True).click()

        body = request_info.value.post_data_json or {}
        if guidance:
            assert body["redirect_text"] == guidance
        else:
            assert "redirect_text" not in body
    finally:
        _delete_task(page, base_url, task_id)
