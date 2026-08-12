"""E2E coverage for concurrent Cowork preview polling."""

import json
import os

from playwright.sync_api import Page


SCREENSHOTS_DIR = os.path.join("temp", "cowork-concurrent")


def _seed_task(page: Page, base_url: str, title: str) -> int:
    response = page.request.post(
        f"{base_url}/api/tasks",
        data={
            "title": title,
            "description": "Concurrent Cowork visual test",
            "action_type": "follow-up",
        },
    )
    assert response.ok
    return response.json()["task"]["id"]


def _delete_task(page: Page, base_url: str, task_id: int) -> None:
    page.request.delete(f"{base_url}/api/tasks/{task_id}")


def _track_intervals(page: Page) -> None:
    page.evaluate(
        """
        window.__cwIntervals = [];
        window.__cwCleared = [];
        const originalSet = window.setInterval.bind(window);
        const originalClear = window.clearInterval.bind(window);
        window.setInterval = function (fn, ms) {
            const timer = originalSet(fn, ms);
            window.__cwIntervals.push(timer);
            return timer;
        };
        window.clearInterval = function (timer) {
            window.__cwCleared.push(timer);
            return originalClear(timer);
        };
        """
    )


def _mock_action(page: Page, task_id: int, state: str) -> None:
    action = {
        "id": task_id,
        "task_id": task_id,
        "state": state,
        "created_at": "2026-07-31T18:00:00Z",
        "finding": "Current state found.",
        "draft": "Draft response.",
        "draft_edited": None,
        "redirect_text": None,
        "destination_kind": "one_to_one",
        "destination_ref": "person@example.com",
        "conversation_id": "cw-test",
        "terminal_status": "ok" if state == "ready" else None,
        "error": None,
    }
    page.route(
        f"**/api/tasks/{task_id}/cowork",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"action": action}),
        ),
    )


class TestTodoIQConcurrentCowork:
    def test_starting_second_task_does_not_cancel_first(self, page: Page, base_url):
        page.goto(base_url + "/todo")
        page.wait_for_function('typeof cwStartPoller === "function"')
        _track_intervals(page)

        page.evaluate("cwStartPoller(91001)")
        first_timer = page.evaluate("window.__cwIntervals[0]")
        page.evaluate("cwStartPoller(91002)")

        assert first_timer not in page.evaluate("window.__cwCleared")
        assert page.evaluate("Boolean(_cwPollers[91001])")
        assert page.evaluate("Boolean(_cwPollers[91002])")
        page.evaluate("cwStopPoller(91001); cwStopPoller(91002)")

    def test_stopping_second_task_leaves_first_running(self, page: Page, base_url):
        page.goto(base_url + "/todo")
        page.wait_for_function('typeof cwStartPoller === "function"')
        page.evaluate("cwStartPoller(92001); cwStartPoller(92002)")

        page.evaluate("cwStopPoller(92002)")

        assert page.evaluate("Boolean(_cwPollers[92001])")
        assert page.evaluate("typeof _cwPollers[92002] === 'undefined'")
        page.evaluate("cwStopPoller(92001)")

    def test_completed_task_stops_without_stopping_other_task(
        self, page: Page, base_url
    ):
        page.goto(base_url + "/todo")
        page.wait_for_function('typeof cwStartPoller === "function"')
        page.evaluate(
            """
            tasks.push(
                {id: 93001, action_type: 'follow-up', cw_state: 'previewing'},
                {id: 93002, action_type: 'follow-up', cw_state: 'previewing'}
            );
            """
        )
        _mock_action(page, 93001, "ready")
        _mock_action(page, 93002, "previewing")
        page.evaluate("cwStartPoller(93001); cwStartPoller(93002)")

        page.evaluate("cwPoll(93001)")
        page.wait_for_function(
            "(tasks.find(t => t.id === 93001) || {}).cw_state === 'ready'"
        )

        assert page.evaluate("typeof _cwPollers[93001] === 'undefined'")
        assert page.evaluate("Boolean(_cwPollers[93002])")
        page.evaluate("cwStopPoller(93002)")

    def test_poll_after_stop_is_a_safe_noop(self, page: Page, base_url):
        page.goto(base_url + "/todo")
        page.wait_for_function('typeof cwStartPoller === "function"')
        page.evaluate("cwStartPoller(94001); cwStopPoller(94001)")

        error = page.evaluate(
            """
            async () => {
                try { await cwPoll(94001); return null; }
                catch (err) { return err.message; }
            }
            """
        )
        assert error is None

    def test_running_card_visual_and_elapsed_gate(self, page: Page, base_url):
        task_id = _seed_task(page, base_url, "Concurrent Cowork visual gate")
        os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
        try:
            page.goto(base_url + "/todo")
            page.wait_for_function(
                f"Boolean(tasks.find(task => task.id === {task_id}))"
            )
            _mock_action(page, task_id, "previewing")
            page.evaluate(
                f"""
                const task = tasks.find(item => item.id === {task_id});
                task.cw_loaded = true;
                task.cw_state = 'previewing';
                task.action_type = 'follow-up';
                _cwStartedAt[{task_id}] = Date.now() - 10000;
                selectTask({task_id});
                """
            )
            assert page.evaluate(f"Boolean(_cwPollers[{task_id}])")

            spinner = page.locator(".cw-spinner").first
            box = spinner.bounding_box()
            assert box and box["width"] > 0 and box["height"] > 0
            heartbeat = page.locator(f"#cw-hb-{task_id}")
            before = heartbeat.text_content()
            page.evaluate(
                f"""
                async () => {{
                    _cwStartedAt[{task_id}] -= 3000;
                    await cwPoll({task_id});
                }}
                """
            )
            after = heartbeat.text_content()
            assert before != after
            assert "elapsed" in (after or "")

            for theme in ("light", "dark"):
                page.evaluate(
                    f"document.body.classList.toggle('dark', {str(theme == 'dark').lower()})"
                )
                page.screenshot(
                    path=os.path.join(
                        SCREENSHOTS_DIR, f"concurrent-running-{theme}.png"
                    ),
                    full_page=True,
                )
            page.evaluate(f"cwStopPoller({task_id})")
        finally:
            _delete_task(page, base_url, task_id)


class TestDashboardConcurrentCowork:
    def test_starting_second_task_does_not_cancel_first(self, page: Page, base_url):
        page.goto(base_url + "/")
        page.wait_for_function('typeof startCoworkPoller === "function"')
        _track_intervals(page)

        page.evaluate("startCoworkPoller(95001)")
        first_timer = page.evaluate("window.__cwIntervals[0]")
        page.evaluate("startCoworkPoller(95002)")

        assert first_timer not in page.evaluate("window.__cwCleared")
        assert page.evaluate("Boolean(_cwPollers[95001])")
        assert page.evaluate("Boolean(_cwPollers[95002])")
        page.evaluate("stopCoworkPoller(95001); stopCoworkPoller(95002)")

    def test_preview_render_self_heals_missing_poller(self, page: Page, base_url):
        page.goto(base_url + "/")
        page.wait_for_function('typeof renderCoworkCard === "function"')
        page.evaluate(
            """
            _cwActions[96001] = {
                id: 96001,
                task_id: 96001,
                state: 'previewing',
                created_at: new Date(Date.now() - 10000).toISOString()
            };
            renderCoworkCard({
                id: 96001,
                title: 'Self-heal',
                action_type: 'follow-up',
                coaching_text: ''
            });
            """
        )

        assert page.evaluate("Boolean(_cwPollers[96001])")
        page.evaluate("stopCoworkPoller(96001)")
