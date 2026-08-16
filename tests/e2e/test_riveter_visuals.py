"""Visual gates for Riveter branding and right-pane design alternatives."""

import os
from pathlib import Path

from PIL import Image
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
        expect(page.get_by_test_id("brand-tagline")).to_have_count(0)
        _capture(page, "dashboard-desktop-light")

        page.evaluate(
            "document.documentElement.setAttribute('data-theme', 'dark')"
        )
        expect(page.get_by_test_id("riveter-logo-dark")).to_be_visible()
        expect(page.get_by_test_id("riveter-logo-light")).to_be_hidden()
        expect(page.get_by_test_id("brand-tagline")).to_have_count(0)
        _capture(page, "dashboard-desktop-dark")

        page.set_viewport_size({"width": 375, "height": 720})
        page.goto(base_url + "/")
        expect(page.get_by_test_id("riveter-logo-light")).to_be_visible()
        expect(page.get_by_test_id("brand-tagline")).to_have_count(0)
        assert page.evaluate(
            "() => document.documentElement.scrollWidth"
            " <= document.documentElement.clientWidth"
        )
        _capture(page, "dashboard-mobile-light")

        for filename in ("riveter-light.png", "riveter-dark.png"):
            image = Image.open(Path("static") / "img" / filename)
            assert image.mode == "RGBA"
            assert image.getpixel((0, 0))[3] == 0
            width, height = image.size
            assert 2.8 <= width / height <= 3.2
            assert image.getpixel((width - 1, 0))[3] == 0

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
        source_profile = page.get_by_test_id("source-profile")
        expect(source_profile).to_be_visible()
        expect(source_profile).to_have_attribute("alt", "Profile image placeholder for Mehdi")
        assert source_profile.evaluate("image => image.naturalWidth") > 0
        meeting_card = page.get_by_test_id("meeting-request-card")
        expect(meeting_card).to_be_visible()
        expect(meeting_card).to_contain_text("Draft meeting request")
        expect(meeting_card).to_contain_text("NOT SENT")
        expect(meeting_card).to_contain_text("Microsoft Teams")
        expect(meeting_card).to_contain_text("25 minutes")
        expect(page.get_by_test_id("person-picker")).to_have_count(2)
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

        progress = page.get_by_test_id("progress-details")
        expect(progress).to_have_count(3)
        expect(progress.first).not_to_have_attribute("open", "")
        progress.first.locator("summary").click()
        expect(progress.first).to_have_attribute("open", "")
        tool_icons = page.get_by_test_id("tool-call-icon")
        expect(tool_icons).to_have_count(2)
        expect(page.get_by_role("img", name="Teams tool call")).to_be_visible()
        expect(page.get_by_role("img", name="Outlook tool call")).to_be_visible()

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

        prompt = page.get_by_test_id("cowork-prompt")
        expect(prompt).to_contain_text("Cowork prompt")
        prompt.click()
        editor = page.get_by_test_id("cowork-prompt-editor")
        expect(editor).to_be_visible()
        editor.fill("Draft a shorter Teams follow-up.")
        editor.press("Control+Enter")
        expect(prompt).to_contain_text("Draft a shorter Teams follow-up.")
        expect(page.get_by_test_id("session-timeline")).to_be_hidden()
        expect(page.get_by_test_id("interaction-card")).to_be_hidden()
        send_direction = page.get_by_test_id("send-direction")
        expect(send_direction).to_be_visible()
        page.get_by_test_id("session-mode-no-interaction").click()
        expect(page.get_by_test_id("session-timeline")).to_be_hidden()
        expect(page.get_by_test_id("session-complete")).to_be_hidden()
        expect(send_direction).to_be_visible()
        page.get_by_test_id("session-mode-interaction").click()
        expect(page.get_by_test_id("interaction-card")).to_be_hidden()
        expect(send_direction).to_be_visible()
        _capture(page, "rightpane-b-direction-cleared-light")
        send_direction.click()
        expect(send_direction).to_be_hidden()
        expect(page.get_by_test_id("redirect-sent")).to_be_visible()

        page.goto(
            f"{base_url}/static/mock-riveter-rightpane-b.html?scoutTheme=light"
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
        expect(page.get_by_test_id("meeting-request-card")).to_be_visible()
        _capture(page, "rightpane-b-iteration-dark")
        page.get_by_test_id("cowork-prompt").click()
        dark_editor = page.get_by_test_id("cowork-prompt-editor")
        dark_editor.fill("Redirect the follow-up to current support only.")
        dark_editor.press("Control+Enter")
        expect(page.get_by_test_id("send-direction")).to_be_visible()
        _capture(page, "rightpane-b-direction-cleared-dark")

    def test_evidence_and_action_live_running_state(self, page: Page, base_url):
        page.set_viewport_size({"width": 1440, "height": 960})
        page.goto(
            f"{base_url}/static/mock-riveter-rightpane-b.html"
            "?scoutTheme=dark&mode=running"
        )

        expect(page.get_by_test_id("live-running")).to_be_visible()
        expect(page.get_by_test_id("cowork-finished")).to_be_hidden()
        page.get_by_test_id("session-mode-no-interaction").click()
        expect(page.get_by_test_id("session-complete")).to_be_hidden()
        expect(page.get_by_test_id("live-running")).to_be_visible()
        dots = page.get_by_test_id("step-dot")
        expect(dots).to_have_count(3)
        assert page.evaluate(
            "getComputedStyle(document.querySelectorAll('[data-testid=\"step-dot\"]')[0]).animationName"
        ) == "none"
        assert page.evaluate(
            "getComputedStyle(document.querySelector('[data-testid=\"step-dot\"].active')).animationName"
        ) == "cowork-pulse"

        elapsed = page.get_by_test_id("elapsed-timer")
        before = elapsed.inner_text()
        page.wait_for_timeout(1100)
        assert elapsed.inner_text() != before
        expect(page.locator("#current-step-time")).to_have_text(elapsed.inner_text())

        completed_before = page.locator(".event.completed").all_inner_texts()
        page.evaluate(
            """window.dispatchEvent(new CustomEvent('cowork:progress', {
                detail: {line: 'Drafting the Teams follow-up'}
            }))"""
        )
        live_status = page.get_by_test_id("live-status")
        expect(live_status).to_have_attribute("aria-live", "polite")
        expect(live_status).to_have_text("Drafting the Teams follow-up")
        assert page.locator(".event.completed").all_inner_texts() == completed_before
        _capture(page, "rightpane-b-live-running-dark")

        page.emulate_media(reduced_motion="reduce")
        assert page.evaluate(
            "getComputedStyle(document.querySelector('[data-testid=\"step-dot\"].active')).animationName"
        ) == "none"

        page.emulate_media(reduced_motion="no-preference")
        page.evaluate("window.dispatchEvent(new CustomEvent('cowork:done'))")
        expect(page.get_by_test_id("live-running")).to_be_hidden()
        expect(page.get_by_test_id("cowork-finished")).to_be_visible()
        assert page.evaluate(
            "getComputedStyle(document.querySelectorAll('[data-testid=\"step-dot\"]')[2]).animationName"
        ) == "none"

        page.goto(
            f"{base_url}/static/mock-riveter-rightpane-b.html"
            "?scoutTheme=dark&mode=running"
        )
        page.get_by_test_id("cowork-prompt").click()
        editor = page.get_by_test_id("cowork-prompt-editor")
        editor.fill("Redirect this turn to current support only.")
        editor.press("Control+Enter")
        expect(page.get_by_test_id("live-running")).to_be_hidden()
        expect(page.get_by_test_id("send-direction")).to_be_visible()
        stopped_at = page.get_by_test_id("elapsed-timer").inner_text()
        page.wait_for_timeout(1100)
        expect(page.get_by_test_id("elapsed-timer")).to_have_text(stopped_at)
