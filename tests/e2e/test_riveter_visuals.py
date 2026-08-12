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
