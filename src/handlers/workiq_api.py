"""Passive Work IQ status and explicitly initiated setup actions."""

from __future__ import annotations

import asyncio
import json

import tornado.web

from ..services.workiq_runtime import get_runtime
from ..services.workiq_setup import get_setup


def _normalized_status() -> dict:
    setup = get_setup().inspect()
    runtime = get_runtime().snapshot()
    if not isinstance(setup, dict):
        setup = {"state": "mcp_unavailable"}
    if not isinstance(runtime, dict):
        runtime = {"state": "stopped", "error": None}
    runtime = {
        key: runtime.get(key)
        for key in (
            "state",
            "error",
            "protocol_version",
            "server",
            "allowed_capabilities",
            "authenticated",
            "active_job_id",
            "queue_depth",
        )
    }
    state = setup.get("state", "mcp_unavailable")
    if state == "mcp_unavailable":
        runtime_state = runtime.get("state")
        error_code = (runtime.get("error") or {}).get("code")
        if error_code in {"eula_required", "auth_required", "consent_required"}:
            state = error_code
        elif error_code == "capability_denied":
            state = "capability_missing"
        elif error_code == "version_mismatch":
            state = "version_mismatch"
        elif error_code == "dependency_missing":
            state = "missing_cli"
        elif runtime.get("authenticated") is True and runtime_state == "ready":
            state = "ready"
        else:
            state = "mcp_unavailable"
    eula_status = "unknown"
    if state == "eula_required":
        eula_status = "required"
    elif runtime.get("authenticated") is True:
        eula_status = "accepted"
    return {
        "state": state,
        "setup": setup,
        "runtime": runtime,
        "eula": {
            "url": setup.get("eula_url", "https://github.com/microsoft/work-iq"),
            "status": eula_status,
        },
    }


class _WorkIQHandler(tornado.web.RequestHandler):
    _LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}

    def set_default_headers(self):
        self.set_header("Content-Type", "application/json")

    def _same_origin(self) -> bool:
        host_name = self.request.host_name.lower()
        if host_name.startswith("[") and host_name.endswith("]"):
            host_name = host_name[1:-1]
        host_name = host_name.rstrip(".")
        if host_name not in self._LOOPBACK_HOSTS:
            return False
        origin = self.request.headers.get("Origin")
        expected = f"{self.request.protocol}://{self.request.host}"
        return origin is None or origin == expected

    def _require_mutation_request(self) -> bool:
        if not self._same_origin():
            self.set_status(403)
            self.write(json.dumps({
                "ok": False,
                "error": {"code": "forbidden", "message": "Same-origin request required."},
            }))
            return False
        content_type = self.request.headers.get("Content-Type", "").split(";", 1)[0]
        if content_type.lower() != "application/json":
            self.set_status(415)
            self.write(json.dumps({
                "ok": False,
                "error": {"code": "invalid_content_type", "message": "JSON is required."},
            }))
            return False
        return True

    def _json_body(self):
        try:
            return json.loads(self.request.body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None


class WorkIQStatusHandler(_WorkIQHandler):
    def get(self):
        self.write(json.dumps(_normalized_status()))


class WorkIQReadinessHandler(_WorkIQHandler):
    async def post(self):
        if not self._require_mutation_request():
            return
        body = self._json_body()
        if not isinstance(body, dict):
            self.set_status(400)
            self.write(json.dumps({
                "ok": False,
                "error": {"code": "invalid_request", "message": "A JSON object is required."},
            }))
            return
        setup = get_setup().inspect()
        if setup["state"] in {"missing_cli", "version_mismatch"}:
            self.set_status(409)
            self.write(json.dumps({
                "ok": False,
                "error": {
                    "code": setup["state"],
                    "message": "The pinned Work IQ runtime is not ready.",
                },
                "status": _normalized_status(),
            }))
            return
        runtime = get_runtime()
        result = await asyncio.get_running_loop().run_in_executor(None, runtime.probe)
        if not result["ok"]:
            self.set_status(409 if result["error"]["code"] in {
                "auth_required",
                "consent_required",
                "eula_required",
                "capability_denied",
                "version_mismatch",
                "dependency_missing",
            } else 503)
        self.write(json.dumps({**result, "status": _normalized_status()}))


class WorkIQEulaHandler(_WorkIQHandler):
    async def post(self):
        if not self._require_mutation_request():
            return
        body = self._json_body()
        if (
            not isinstance(body, dict)
            or set(body) != {"acknowledged"}
            or body.get("acknowledged") is not True
        ):
            self.set_status(400)
            self.write(json.dumps({
                "ok": False,
                "error": {
                    "code": "acknowledgment_required",
                    "message": "Explicit EULA acknowledgment is required.",
                },
            }))
            return
        runtime = get_runtime()
        setup = get_setup()

        def accept_after_shutdown():
            runtime.shutdown()
            return setup.accept_eula()

        result = await asyncio.get_running_loop().run_in_executor(
            None, accept_after_shutdown
        )
        if result["ok"]:
            runtime.invalidate_setup_state()
        if not result["ok"]:
            self.set_status(409)
        status = _normalized_status()
        if result["ok"]:
            status["eula"] = {**status["eula"], "status": "accepted"}
            status["notice"] = (
                "Work IQ EULA accepted. Select Check readiness to verify sign-in."
            )
        self.write(json.dumps({**result, "status": status}))
