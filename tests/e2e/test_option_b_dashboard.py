"""Production gates for the Option B evidence-and-action detail layout."""

import json
import os

from playwright.sync_api import Page, expect


SCREENSHOTS_DIR = os.path.join("temp", "option-b-dashboard")


def _seed_task(page: Page, base_url: str) -> int:
    response = page.request.post(
        f"{base_url}/api/tasks",
        data={
            "title": "Schedule Copilot Kit FinOps follow-up",
            "description": (
                "Confirm current FinOps support and whether roadmap guidance "
                "can be shared."
            ),
            "source_type": "chat",
            "source_snippet": (
                "The deployment path is clear, but the FinOps position remains "
                "unresolved."
            ),
            "source_url": "https://teams.microsoft.com/l/message/example",
            "key_people": json.dumps(
                [
                    {
                        "name": "Mehdi Slaoui Andaloussi",
                        "role": "Customer lead",
                        "alternatives": [],
                    }
                ]
            ),
            "parse_status": "parsed",
        },
    )
    assert response.ok
    return response.json()["task"]["id"]


def _open_task(page: Page, base_url: str, task_id: int) -> None:
    page.goto(base_url + "/")
    page.wait_for_function(f"Boolean(tasks.find(task => task.id === {task_id}))")
    page.evaluate(f"selectTask({task_id})")
    expect(page.locator(".detail-split")).to_be_visible()


def _delete_task(page: Page, base_url: str, task_id: int) -> None:
    page.request.delete(f"{base_url}/api/tasks/{task_id}")


class TestOptionBStructure:
    def test_evidence_and_action_layout_uses_real_task_data(
        self, page: Page, base_url
    ):
        task_id = _seed_task(page, base_url)
        os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
        try:
            _open_task(page, base_url, task_id)

            header = page.locator(".detail-task-header")
            lifecycle = page.locator(".detail-lifecycle-strip")
            evidence = page.locator(".detail-evidence")
            workspace = page.locator(".detail-workspace")

            expect(header).to_contain_text("Schedule Copilot Kit FinOps follow-up")
            expect(lifecycle).to_be_visible()
            expect(evidence).to_contain_text("Source and context")
            expect(evidence).to_contain_text("Mehdi Slaoui Andaloussi")
            expect(evidence).to_contain_text("FinOps position remains unresolved")
            expect(evidence).to_contain_text("Task brief")
            expect(evidence).to_contain_text("Confirm current FinOps support")
            expect(
                evidence.locator(".detail-source-card").locator(
                    f"#desc-display-{task_id}"
                )
            ).to_be_visible()
            expect(workspace.locator(".cw-card")).to_be_visible()

            evidence_box = evidence.bounding_box()
            workspace_box = workspace.bounding_box()
            assert evidence_box and workspace_box
            assert evidence_box["x"] < workspace_box["x"]

            page.screenshot(
                path=os.path.join(SCREENSHOTS_DIR, "option-b-light.png"),
                full_page=True,
            )
            page.evaluate(
                "document.documentElement.setAttribute('data-theme', 'dark')"
            )
            page.screenshot(
                path=os.path.join(SCREENSHOTS_DIR, "option-b-dark.png"),
                full_page=True,
            )
        finally:
            _delete_task(page, base_url, task_id)

    def test_mobile_stacks_panes_and_hides_separator(self, page: Page, base_url):
        task_id = _seed_task(page, base_url)
        try:
            page.set_viewport_size({"width": 375, "height": 812})
            _open_task(page, base_url, task_id)

            expect(page.locator(".detail-split-handle")).to_be_hidden()
            evidence_box = page.locator(".detail-evidence").bounding_box()
            workspace_box = page.locator(".detail-workspace").bounding_box()
            assert evidence_box and workspace_box
            assert workspace_box["y"] >= evidence_box["y"] + evidence_box["height"]
            assert page.evaluate(
                "document.documentElement.scrollWidth <= "
                "document.documentElement.clientWidth"
            )
        finally:
            _delete_task(page, base_url, task_id)

    def test_narrow_detail_container_stacks_before_minimums_collide(
        self, page: Page, base_url
    ):
        task_id = _seed_task(page, base_url)
        try:
            _open_task(page, base_url, task_id)
            page.evaluate(
                """() => {
                    const panel = document.querySelector('.right-panel');
                    panel.style.flex = '0 0 570px';
                    panel.style.width = '570px';
                }"""
            )
            expect(page.locator(".detail-split")).to_have_class(
                __import__("re").compile(r"\bis-stacked\b")
            )
            expect(page.locator(".detail-split-handle")).to_be_hidden()
        finally:
            _delete_task(page, base_url, task_id)


class TestOptionBSeparator:
    def test_drag_persists_and_survives_detail_rerender(
        self, page: Page, base_url
    ):
        task_id = _seed_task(page, base_url)
        try:
            _open_task(page, base_url, task_id)
            evidence = page.locator(".detail-evidence")
            handle = page.locator(".detail-split-handle")
            before = evidence.bounding_box()
            handle_box = handle.bounding_box()
            assert before and handle_box

            page.mouse.move(
                handle_box["x"] + handle_box["width"] / 2,
                handle_box["y"] + 30,
            )
            page.mouse.down()
            page.mouse.move(handle_box["x"] + 90, handle_box["y"] + 30)
            page.mouse.up()

            widened = evidence.bounding_box()
            assert widened and widened["width"] >= before["width"] + 60
            stored = page.evaluate(
                "Number(localStorage.getItem('todoness-evidence-width'))"
            )
            assert 25 <= stored <= 65

            page.evaluate(
                f"renderDetailPane(tasks.find(task => task.id === {task_id}))"
            )
            rerendered = evidence.bounding_box()
            assert rerendered
            assert abs(rerendered["width"] - widened["width"]) <= 3

            handle.focus()
            page.keyboard.press("ArrowLeft")
            after_key = evidence.bounding_box()
            assert after_key and after_key["width"] < rerendered["width"]
        finally:
            _delete_task(page, base_url, task_id)


class TestSourceContextCard:
    def test_equivalent_source_and_description_render_once(
        self, page: Page, base_url
    ):
        response = page.request.post(
            f"{base_url}/api/tasks",
            data={
                "title": "Deduplicate source context",
                "description": "Confirm the current FinOps position.",
                "source_type": "meeting",
                "source_snippet": "  confirm   THE current finops position.  ",
                "coaching_text": "Draft the next-step recommendation for review.",
                "parse_status": "parsed",
            },
        )
        assert response.ok
        task_id = response.json()["task"]["id"]
        update = page.request.put(
            f"{base_url}/api/tasks/{task_id}",
            data={
                "coaching_text": (
                    "Draft the next-step recommendation for review."
                )
            },
        )
        assert update.ok
        os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
        try:
            _open_task(page, base_url, task_id)
            context = page.locator(".detail-source-card")
            expect(context).to_contain_text("Source and context")
            expect(context).to_contain_text("confirm THE current finops position")
            expect(context.locator(".detail-source-link")).not_to_contain_text(
                "confirm THE current finops position"
            )
            expect(context.get_by_text("Task brief", exact=True)).to_have_count(0)
            expect(context.locator(f"#desc-display-{task_id}")).to_be_hidden()
            editor_details = context.locator(".detail-task-brief-collapsed")
            expect(editor_details).to_be_visible()
            expect(editor_details.locator("summary")).to_have_text(
                "Edit stored summary"
            )
            expect(page.locator(".detail-evidence")).not_to_contain_text(
                "Draft the next-step recommendation"
            )
            expect(page.locator(".detail-workspace")).to_contain_text(
                "Draft the next-step recommendation"
            )

            page.screenshot(
                path=os.path.join(
                    SCREENSHOTS_DIR, "source-context-deduplicated-light.png"
                ),
                full_page=True,
            )
            page.evaluate(
                "document.documentElement.setAttribute('data-theme', 'dark')"
            )
            page.screenshot(
                path=os.path.join(
                    SCREENSHOTS_DIR, "source-context-deduplicated-dark.png"
                ),
                full_page=True,
            )

            editor_details.locator("summary").click()
            page.evaluate(f"toggleDescriptionEdit({task_id})")
            editor = context.locator(f"#desc-edit-{task_id}")
            editor.fill("A refined task brief that adds new information.")
            editor.blur()
            expect(context.locator(".detail-task-brief")).to_contain_text(
                "A refined task brief that adds new information."
            )
        finally:
            _delete_task(page, base_url, task_id)

    def test_manual_description_is_editable_inside_context_card(
        self, page: Page, base_url
    ):
        response = page.request.post(
            f"{base_url}/api/tasks",
            data={
                "title": "Manual context task",
                "description": "Original manual task brief.",
                "source_type": "manual",
                "source_snippet": "Original manual task brief.",
                "parse_status": "parsed",
            },
        )
        assert response.ok
        task_id = response.json()["task"]["id"]
        try:
            _open_task(page, base_url, task_id)
            context = page.locator(".detail-source-card")
            expect(context.locator(".detail-task-brief .detail-label")).to_contain_text(
                "Task brief"
            )
            display = context.locator(f"#desc-display-{task_id}")
            editor = context.locator(f"#desc-edit-{task_id}")
            expect(display).to_have_text("Original manual task brief.")

            page.evaluate(f"toggleDescriptionEdit({task_id})")
            editor.fill("Updated manual task brief.")
            page.evaluate(
                f"renderDetailPane(tasks.find(task => task.id === {task_id}))"
            )
            expect(editor).to_be_visible()
            expect(editor).to_have_value("Updated manual task brief.")
            editor.blur()
            expect(display).to_have_text("Updated manual task brief.")
        finally:
            _delete_task(page, base_url, task_id)


class TestOptionBSeparatorKeyboard:
    def test_keyboard_resize_is_scoped_and_exposes_aria_values(
        self, page: Page, base_url
    ):
        task_id = _seed_task(page, base_url)
        try:
            _open_task(page, base_url, task_id)
            evidence = page.locator(".detail-evidence")
            handle = page.locator(".detail-split-handle")
            expect(handle).to_have_attribute("role", "separator")
            expect(handle).to_have_attribute("aria-orientation", "vertical")
            minimum_value = int(handle.get_attribute("aria-valuemin"))
            maximum_value = int(handle.get_attribute("aria-valuemax"))
            assert 25 < minimum_value < maximum_value < 65

            evidence_focusables = page.locator(
                ".detail-evidence button, .detail-evidence a, "
                ".detail-evidence input, .detail-evidence textarea, "
                ".detail-evidence select, .detail-evidence [tabindex='0']"
            )
            evidence_focusables.last.focus()
            page.keyboard.press("Tab")
            expect(handle).to_be_focused()
            page.evaluate(
                f"renderDetailPane(tasks.find(task => task.id === {task_id}))"
            )
            expect(handle).to_be_focused()
            page.keyboard.press("Tab")
            active = page.evaluate(
                """({
                    inWorkspace: document.activeElement.closest('.detail-workspace') !== null,
                    html: document.activeElement.outerHTML
                })"""
            )
            assert active["inWorkspace"], active["html"]

            initial = evidence.bounding_box()
            assert initial
            page.locator(".detail-lifecycle-strip button").first.focus()
            page.keyboard.press("ArrowRight")
            unchanged = evidence.bounding_box()
            assert unchanged
            assert abs(unchanged["width"] - initial["width"]) <= 1

            handle.focus()
            page.keyboard.press("End")
            expect(handle).to_have_attribute(
                "aria-valuenow", str(maximum_value)
            )
            maximum = evidence.bounding_box()
            assert maximum and maximum["width"] > initial["width"]
            workspace = page.locator(".detail-workspace").bounding_box()
            assert workspace and workspace["width"] >= 320

            page.keyboard.press("Home")
            expect(handle).to_have_attribute(
                "aria-valuenow", str(minimum_value)
            )
            minimum = evidence.bounding_box()
            assert minimum and minimum["width"] < maximum["width"]
            assert minimum["width"] >= 260
        finally:
            _delete_task(page, base_url, task_id)
