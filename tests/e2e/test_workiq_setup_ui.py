import json
from pathlib import Path

from PIL import Image
from playwright.sync_api import Page, expect


SCREENSHOTS = Path("temp") / "workiq-visuals"


def status_for(state):
    return {
        "state": state,
        "setup": {
            "state": state if state in {"missing_cli", "version_mismatch"} else "mcp_unavailable",
            "required_version": "1.0.0",
            "installed_version": (
                None if state == "missing_cli"
                else "0.9.0" if state == "version_mismatch"
                else "1.0.0"
            ),
            "eula_url": "https://github.com/microsoft/work-iq",
            "install_command": r"powershell -ExecutionPolicy Bypass -File scripts\setup_workiq.ps1",
        },
        "runtime": {
            "state": "ready" if state == "ready" else state,
            "allowed_capabilities": (
                ["ask_work_iq", "do_action"] if state == "ready" else []
            ),
            "authenticated": state == "ready",
        },
        "eula": {
            "url": "https://github.com/microsoft/work-iq",
            "status": "required" if state == "eula_required" else (
                "accepted" if state == "ready" else "unknown"
            ),
        },
    }


def capture(page, state, size):
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    page.evaluate(
        """document.querySelectorAll('div').forEach((node) => {
            if (node.style.zIndex === '2147483647') node.remove();
        })"""
    )
    assert page.evaluate(
        "() => document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    )
    card = page.get_by_test_id("workiq-setup-card")
    box = card.bounding_box()
    viewport = page.viewport_size
    assert box and box["width"] > 0 and box["height"] > 0
    assert box["x"] >= 0 and box["x"] + box["width"] <= viewport["width"]
    assert box["y"] >= 0 and box["y"] < viewport["height"]
    path = SCREENSHOTS / f"{state}-{size}.png"
    page.screenshot(path=str(path), full_page=True)
    with Image.open(path) as image:
        assert image.width == page.viewport_size["width"]
        assert image.height >= page.viewport_size["height"]


def mock_status(page, state):
    page.route(
        "**/api/workiq/status",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(status_for(state)),
        ),
    )


class TestWorkIQSetupVisuals:
    def test_unchecked_runtime_offers_explicit_eula_and_readiness_actions(
        self, page: Page, base_url
    ):
        mock_status(page, "mcp_unavailable")
        page.goto(base_url + "/")
        expect(page.get_by_test_id("workiq-eula-ack")).to_be_visible()
        expect(page.get_by_test_id("workiq-eula-accept")).to_be_disabled()
        expect(page.get_by_test_id("workiq-readiness")).to_be_visible()

    def test_readiness_blocker_immediately_reveals_eula_controls(
        self, page: Page, base_url
    ):
        initial = status_for("mcp_unavailable")
        blocked = status_for("eula_required")
        page.route(
            "**/api/workiq/status",
            lambda route: route.fulfill(
                status=200, content_type="application/json", body=json.dumps(initial)
            ),
        )
        page.route(
            "**/api/workiq/readiness",
            lambda route: route.fulfill(
                status=409,
                content_type="application/json",
                body=json.dumps({
                    "ok": False,
                    "error": {
                        "code": "eula_required",
                        "message": "Work IQ requires EULA acceptance.",
                    },
                    "status": blocked,
                }),
            ),
        )
        page.goto(base_url + "/")
        page.get_by_test_id("workiq-readiness").click()
        expect(page.get_by_test_id("workiq-state")).to_have_attribute(
            "data-state", "eula_required"
        )
        expect(page.get_by_test_id("workiq-eula-ack")).to_be_visible()

    def test_all_setup_states_desktop(self, page: Page, base_url):
        page.set_viewport_size({"width": 1440, "height": 960})
        states = {
            "missing_cli": (
                "Setup required",
                "Install the pinned Work IQ runtime before checking readiness.",
                True, False, False,
            ),
            "version_mismatch": (
                "Version mismatch",
                "The installed Work IQ package does not match Riveter’s supported version.",
                True, False, False,
            ),
            "mcp_unavailable": (
                "Not checked",
                "The local runtime is installed. Check readiness to verify MCP and authentication.",
                False, True, True,
            ),
            "eula_required": (
                "EULA required",
                "Read the official EULA and explicitly acknowledge it below.",
                False, True, True,
            ),
            "auth_required": (
                "Sign in required",
                "Sign in to Work IQ, then check readiness again.",
                False, False, True,
            ),
            "consent_required": (
                "Consent required",
                "Administrator consent is required before Work IQ can be used.",
                False, False, True,
            ),
            "capability_missing": (
                "Capability missing",
                "This Work IQ runtime does not advertise the required calendar capabilities.",
                False, False, True,
            ),
            "ready": (
                "Ready",
                "Owned Work IQ MCP calendar reads are authenticated and ready.",
                False, False, False,
            ),
        }
        for state, (badge, help_copy, install, eula, readiness) in states.items():
            page.unroute("**/api/workiq/status")
            mock_status(page, state)
            page.goto(base_url + "/")
            card = page.get_by_test_id("workiq-setup-card")
            expect(card).to_be_visible()
            expect(page.get_by_test_id("workiq-state")).to_have_attribute(
                "data-state", state
            )
            expect(page.get_by_test_id("workiq-state")).to_have_text(badge)
            expect(page.locator("#workiq-message")).to_have_text(help_copy)
            expected_installed = (
                "not installed" if state == "missing_cli"
                else "0.9.0" if state == "version_mismatch"
                else "1.0.0"
            )
            expect(page.locator("#workiq-version")).to_have_text(
                f"Installed: {expected_installed} · Supported: 1.0.0"
            )
            command = page.locator("#workiq-install-command")
            expect(command).to_have_text(
                r"powershell -ExecutionPolicy Bypass -File scripts\setup_workiq.ps1"
            )
            (expect(command).to_be_visible() if install else expect(command).to_be_hidden())
            controls = page.locator("#workiq-eula-controls")
            (expect(controls).to_be_visible() if eula else expect(controls).to_be_hidden())
            ready_button = page.get_by_test_id("workiq-readiness")
            (
                expect(ready_button).to_be_visible()
                if readiness
                else expect(ready_button).to_be_hidden()
            )
            link = page.locator("#workiq-eula-link")
            expect(link).to_have_attribute("href", "https://github.com/microsoft/work-iq")
            expect(link).to_have_attribute("rel", "noopener noreferrer")
            box = card.bounding_box()
            assert box and box["width"] > 0 and box["height"] > 0
            assert box["x"] >= 0 and box["x"] + box["width"] <= 1440
            capture(page, state, "desktop")

    def test_eula_confirmation_failure_and_ready_mobile(self, page: Page, base_url):
        page.set_viewport_size({"width": 375, "height": 720})
        current = {"state": "eula_required"}
        calls = []

        page.route(
            "**/api/workiq/status",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(status_for(current["state"])),
            ),
        )

        def eula(route, request):
            calls.append(json.loads(request.post_data))
            route.fulfill(
                status=409,
                content_type="application/json",
                body=json.dumps({
                    "ok": False,
                    "error": {"code": "eula_required", "message": "Acceptance failed."},
                }),
            )

        page.route("**/api/workiq/eula", eula)
        page.goto(base_url + "/")
        checkbox = page.get_by_test_id("workiq-eula-ack")
        accept = page.get_by_test_id("workiq-eula-accept")
        expect(checkbox).to_be_visible()
        expect(accept).to_be_disabled()
        capture(page, "eula-required", "mobile")
        checkbox.check()
        expect(accept).to_be_enabled()
        accept.click()
        expect(page.get_by_test_id("workiq-error")).to_contain_text(
            "Acceptance failed."
        )
        assert calls == [{"acknowledged": True}]
        capture(page, "eula-failure", "mobile")

        current["state"] = "ready"
        page.reload()
        expect(page.get_by_test_id("workiq-state")).to_have_attribute(
            "data-state", "ready"
        )
        expect(checkbox).to_be_hidden()
        assert page.evaluate(
            "() => document.documentElement.scrollWidth <= document.documentElement.clientWidth"
        )
        capture(page, "ready", "mobile")

    def test_eula_acceptance_then_explicit_readiness_through_real_handlers(
        self, page: Page, base_url
    ):
        page.goto(base_url + "/")
        expect(page.get_by_test_id("workiq-state")).to_have_attribute(
            "data-state", "eula_required"
        )
        page.get_by_test_id("workiq-eula-ack").check()
        page.get_by_test_id("workiq-eula-accept").click()

        expect(page.get_by_test_id("workiq-state")).to_have_attribute(
            "data-state", "mcp_unavailable"
        )
        expect(page.get_by_test_id("workiq-eula-ack")).to_be_hidden()
        expect(page.locator("#workiq-message")).to_have_text(
            "Work IQ EULA accepted. Select Check readiness to verify sign-in."
        )
        expect(page.get_by_test_id("workiq-readiness")).to_be_visible()
        expect(page.get_by_test_id("workiq-error")).to_be_hidden()
        capture(page, "eula-accepted-not-checked", "desktop")

        page.get_by_test_id("workiq-readiness").click()
        expect(page.get_by_test_id("workiq-state")).to_have_attribute(
            "data-state", "ready"
        )
        expect(page.get_by_test_id("workiq-readiness")).to_be_hidden()
