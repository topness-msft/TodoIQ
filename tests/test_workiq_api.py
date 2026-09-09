import json
from unittest.mock import Mock, patch

import tornado.testing

from src.app import make_app


class TestWorkIQAPI(tornado.testing.AsyncHTTPTestCase):
    def get_app(self):
        return make_app()

    def test_status_is_passive_and_reports_setup_blocker(self):
        setup = Mock()
        setup.inspect.return_value = {
            "state": "missing_cli",
            "required_version": "1.0.0",
            "installed_version": None,
            "eula_url": "https://github.com/microsoft/work-iq",
        }
        runtime = Mock()
        runtime.snapshot.return_value = {"state": "stopped", "error": None}
        with (
            patch("src.handlers.workiq_api.get_setup", return_value=setup),
            patch("src.handlers.workiq_api.get_runtime", return_value=runtime),
        ):
            response = self.fetch("/api/workiq/status")
        assert response.code == 200
        assert json.loads(response.body)["state"] == "missing_cli"
        setup.accept_eula.assert_not_called()
        runtime.start.assert_not_called()
        runtime.probe.assert_not_called()

    def test_eula_requires_literal_acknowledgment(self):
        setup = Mock()
        for body in (
            {},
            {"acknowledged": False},
            {"acknowledged": "true"},
            {"acknowledged": True, "extra": True},
            [],
        ):
            with patch("src.handlers.workiq_api.get_setup", return_value=setup):
                response = self.fetch(
                    "/api/workiq/eula",
                    method="POST",
                    body=json.dumps(body),
                    headers={"Content-Type": "application/json"},
                )
            assert response.code == 400
        setup.accept_eula.assert_not_called()

    def test_eula_acceptance_is_explicit_and_same_origin(self):
        setup = Mock()
        setup.accept_eula.return_value = {"ok": True, "message": "accepted"}
        setup.inspect.return_value = {
            "state": "mcp_unavailable",
            "eula_url": "https://github.com/microsoft/work-iq",
        }
        runtime = Mock()
        runtime.snapshot.return_value = {
            "state": "stopped",
            "error": None,
            "authenticated": False,
        }
        with (
            patch("src.handlers.workiq_api.get_setup", return_value=setup),
            patch("src.handlers.workiq_api.get_runtime", return_value=runtime),
        ):
            blocked = self.fetch(
                "/api/workiq/eula",
                method="POST",
                body=json.dumps({"acknowledged": True}),
                headers={
                    "Content-Type": "application/json",
                    "Origin": "https://attacker.example",
                },
            )
            accepted = self.fetch(
                "/api/workiq/eula",
                method="POST",
                body=json.dumps({"acknowledged": True}),
                headers={"Content-Type": "application/json"},
            )
        assert blocked.code == 403
        assert accepted.code == 200
        body = json.loads(accepted.body)
        assert body["status"]["state"] == "mcp_unavailable"
        assert body["status"]["eula"]["status"] == "accepted"
        assert body["status"]["notice"] == (
            "Work IQ EULA accepted. Select Check readiness to verify sign-in."
        )
        setup.accept_eula.assert_called_once_with()
        runtime.shutdown.assert_called_once_with()
        runtime.invalidate_setup_state.assert_called_once_with()
        runtime.start.assert_not_called()
        runtime.probe.assert_not_called()

    def test_eula_rejects_dns_rebinding_host_even_when_origin_matches(self):
        setup = Mock()
        setup.accept_eula.return_value = {"ok": True, "message": "accepted"}
        with patch("src.handlers.workiq_api.get_setup", return_value=setup):
            response = self.fetch(
                "/api/workiq/eula",
                method="POST",
                body=json.dumps({"acknowledged": True}),
                headers={
                    "Content-Type": "application/json",
                    "Host": "attacker.example",
                    "Origin": "http://attacker.example",
                },
            )
        assert response.code == 403
        setup.accept_eula.assert_not_called()

    def test_eula_accepts_bracketed_ipv6_loopback_host(self):
        setup = Mock()
        setup.accept_eula.return_value = {"ok": True, "message": "accepted"}
        runtime = Mock()
        with (
            patch("src.handlers.workiq_api.get_setup", return_value=setup),
            patch("src.handlers.workiq_api.get_runtime", return_value=runtime),
        ):
            response = self.fetch(
                "/api/workiq/eula",
                method="POST",
                body=json.dumps({"acknowledged": True}),
                headers={
                    "Content-Type": "application/json",
                    "Host": "[::1]:8888",
                    "Origin": "http://[::1]:8888",
                },
            )
        assert response.code == 200
        setup.accept_eula.assert_called_once_with()

    def test_readiness_runs_off_handler_and_returns_honest_failure(self):
        setup = Mock()
        setup.inspect.return_value = {"state": "mcp_unavailable"}
        runtime = Mock()
        runtime.probe.return_value = {
            "ok": False,
            "error": {"code": "auth_required", "message": "Sign in required."},
        }
        with (
            patch("src.handlers.workiq_api.get_setup", return_value=setup),
            patch("src.handlers.workiq_api.get_runtime", return_value=runtime),
        ):
            response = self.fetch(
                "/api/workiq/readiness",
                method="POST",
                body="{}",
                headers={"Content-Type": "application/json"},
            )
        assert response.code == 409
        assert json.loads(response.body)["error"]["code"] == "auth_required"
        runtime.probe.assert_called_once_with()

    def test_readiness_maps_blockers_and_generic_failures(self):
        setup = Mock()
        setup.inspect.return_value = {"state": "mcp_unavailable"}
        for code, expected_status, expected_state in (
            ("eula_required", 409, "eula_required"),
            ("auth_required", 409, "auth_required"),
            ("consent_required", 409, "consent_required"),
            ("capability_denied", 409, "capability_missing"),
            ("mcp_unavailable", 503, "mcp_unavailable"),
            ("tool", 503, "mcp_unavailable"),
            ("action_http", 503, "mcp_unavailable"),
            ("invalid_structured_content", 503, "mcp_unavailable"),
            ("timeout", 503, "mcp_unavailable"),
            ("protocol", 503, "mcp_unavailable"),
            ("transport", 503, "mcp_unavailable"),
        ):
            runtime = Mock()
            runtime.probe.return_value = {
                "ok": False,
                "error": {"code": code, "message": "Readiness failed."},
            }
            runtime.snapshot.return_value = {
                "state": code if expected_status == 409 else "faulted",
                "error": {"code": code, "message": "Readiness failed."},
                "authenticated": False,
            }
            with (
                self.subTest(code=code),
                patch("src.handlers.workiq_api.get_setup", return_value=setup),
                patch("src.handlers.workiq_api.get_runtime", return_value=runtime),
            ):
                response = self.fetch(
                    "/api/workiq/readiness",
                    method="POST",
                    body="{}",
                    headers={"Content-Type": "application/json"},
                )
                assert response.code == expected_status
                body = json.loads(response.body)
                assert body["error"]["code"] == code
                assert body["status"]["state"] == expected_state

    def test_status_never_reports_ready_without_authenticated_runtime(self):
        setup = Mock()
        setup.inspect.return_value = {"state": "mcp_unavailable"}
        runtime = Mock()
        runtime.snapshot.return_value = {
            "state": "ready",
            "error": None,
            "authenticated": False,
        }
        with (
            patch("src.handlers.workiq_api.get_setup", return_value=setup),
            patch("src.handlers.workiq_api.get_runtime", return_value=runtime),
        ):
            response = self.fetch("/api/workiq/status")

        assert response.code == 200
        assert json.loads(response.body)["state"] == "mcp_unavailable"

    def test_readiness_rejects_caller_supplied_account_or_action(self):
        runtime = Mock()
        for body in (
            {"account": "attacker@example.com"},
            {"actionUrl": "/me/sendMail"},
            {"schedules": ["attacker@example.com"]},
        ):
            with patch(
                "src.handlers.workiq_api.get_runtime",
                return_value=runtime,
            ):
                response = self.fetch(
                    "/api/workiq/readiness",
                    method="POST",
                    body=json.dumps(body),
                    headers={"Content-Type": "application/json"},
                )
            assert response.code == 400
        runtime.probe.assert_not_called()

    def test_failed_eula_acceptance_preserves_required_blocker(self):
        setup = Mock()
        setup.inspect.return_value = {
            "state": "mcp_unavailable",
            "eula_url": "https://github.com/microsoft/work-iq",
        }
        setup.accept_eula.return_value = {
            "ok": False,
            "error": {"code": "eula_required", "message": "Acceptance failed."},
        }
        runtime = Mock()
        runtime.snapshot.return_value = {
            "state": "stopped",
            "error": {"code": "eula_required", "message": "EULA required."},
            "authenticated": False,
        }
        with (
            patch("src.handlers.workiq_api.get_setup", return_value=setup),
            patch("src.handlers.workiq_api.get_runtime", return_value=runtime),
        ):
            response = self.fetch(
                "/api/workiq/eula",
                method="POST",
                body=json.dumps({"acknowledged": True}),
                headers={"Content-Type": "application/json"},
            )
        assert response.code == 409
        body = json.loads(response.body)
        assert body["error"]["code"] == "eula_required"
        assert body["status"]["state"] == "eula_required"
        runtime.invalidate_setup_state.assert_not_called()
