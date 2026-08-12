"""Visual gates for Riveter branding and right-pane design alternatives."""

import os

from playwright.sync_api import Page, expect


SCREENSHOTS_DIR = os.path.join("temp", "branding")


def _capture(page: Page, name: str) -> None:
    os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
    page.screenshot(
        path=os.path.join(SCREENSHOTS_DIR, f"{name}.png"),
        full_page=True,
    )


class TestRiveterVisuals:
    def test_dashboard_branding_desktop_and_mobile(self, page: Page, base_url):
        page.goto(base_url + "/")
        expect(page).to_have_title("Riveter")
        _capture(page, "dashboard-desktop-light")

        page.evaluate(
            "document.documentElement.setAttribute('data-theme', 'dark')"
        )
        _capture(page, "dashboard-desktop-dark")

        page.set_viewport_size({"width": 375, "height": 720})
        page.goto(base_url + "/")
        expect(page.locator(".riveter-logo")).to_be_visible()
        _capture(page, "dashboard-mobile-light")

    def test_right_pane_alternatives(self, page: Page, base_url):
        page.set_viewport_size({"width": 1440, "height": 960})
        alternatives = {
            "a": "Cowork-first workspace",
            "b": "Evidence and action",
            "c": "Conversation timeline",
        }
        for suffix, heading in alternatives.items():
            page.goto(
                f"{base_url}/static/mock-riveter-rightpane-{suffix}.html"
                "?scoutTheme=light"
            )
            expect(page.locator("h1")).to_have_text(heading)
            choices = page.locator(".choice")
            expect(choices.first).to_have_attribute("aria-pressed", "true")
            choices.nth(1).click()
            expect(choices.first).to_have_attribute("aria-pressed", "false")
            expect(choices.nth(1)).to_have_attribute("aria-pressed", "true")
            _capture(page, f"rightpane-{suffix}-desktop-light")

    def test_evidence_and_action_iteration(self, page: Page, base_url):
        page.set_viewport_size({"width": 1440, "height": 960})
        page.goto(
            f"{base_url}/static/mock-riveter-rightpane-b.html?scoutTheme=light"
        )

        expect(page.get_by_test_id("person-pill")).to_have_count(2)
        person_picker = page.get_by_test_id("person-picker").first
        expect(person_picker).to_have_value("Mehdi Slaoui Andaloussi")
        person_picker.select_option("Mehdi Benjelloun")
        expect(person_picker).to_have_value("Mehdi Benjelloun")
        expect(page.get_by_test_id("source-channel-meta")).to_contain_text(
            "Teams · April 22 · Last touch 110 days ago"
        )
        expect(page.get_by_test_id("situation-summary")).to_be_visible()
        assert "Topic" not in page.locator(".fact b").all_inner_texts()
        assert "Last touch" not in page.locator(".fact b").all_inner_texts()
        lifecycle = page.get_by_test_id("task-lifecycle-strip")
        expect(lifecycle).to_be_visible()
        assert lifecycle.bounding_box()["y"] < page.locator(".split").bounding_box()["y"]

        notes = page.get_by_test_id("private-notes")
        notes.locator("summary").click()
        notes_editor = page.get_by_test_id("private-notes-editor")
        expect(notes_editor).to_be_visible()
        notes_editor.fill("Keep the roadmap language tentative.")
        notes_editor.press("Control+Enter")
        expect(notes).to_contain_text("Keep the roadmap language tentative.")

        prompt = page.get_by_test_id("cowork-prompt")
        expect(prompt).to_contain_text("Cowork prompt")
        prompt.click()
        editor = page.get_by_test_id("cowork-prompt-editor")
        expect(editor).to_be_visible()
        editor.fill("Draft a shorter Teams follow-up.")
        editor.press("Control+Enter")
        expect(prompt).to_contain_text("Draft a shorter Teams follow-up.")

        progress = page.get_by_test_id("progress-details")
        expect(progress).to_have_count(3)
        expect(progress.first).not_to_have_attribute("open", "")
        progress.first.locator("summary").click()
        expect(progress.first).to_have_attribute("open", "")

        command_strip = page.get_by_test_id("cowork-command-strip")
        expect(command_strip).to_be_visible()
        expect(command_strip).to_contain_text("Answer and continue")
        expect(command_strip).to_contain_text("Edit or redirect")
        expect(command_strip).to_contain_text("Stop")
        expect(page.locator(".ask").get_by_test_id("cowork-command-strip")).to_have_count(1)
        expect(page.get_by_test_id("open-in-cowork-link")).to_be_visible()
        expect(page.get_by_test_id("credit-meter")).to_contain_text("30.2 credits")
        expect(page.get_by_test_id("cowork-finished")).to_contain_text(
            "Cowork finished 8m ago"
        )

        page.get_by_test_id("session-mode-no-interaction").click()
        expect(page.locator(".ask")).to_be_hidden()
        expect(page.get_by_test_id("cowork-command-strip")).to_be_hidden()
        expect(page.get_by_test_id("session-complete")).to_be_visible()
        expect(page.get_by_test_id("open-in-cowork-link")).to_be_visible()
        _capture(page, "rightpane-b-no-interaction-light")

        page.goto(
            f"{base_url}/static/mock-riveter-rightpane-b.html?scoutTheme=light"
        )
        _capture(page, "rightpane-b-iteration-light")
        page.evaluate(
            "document.documentElement.setAttribute('data-theme', 'dark')"
        )
        _capture(page, "rightpane-b-iteration-dark")
