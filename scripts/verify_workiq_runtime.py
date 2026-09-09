"""Opt-in metadata-only proof for the app-owned Work IQ runtime."""

from __future__ import annotations

import json
import os
import platform
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.services.workiq_calendar import get_schedule
from src.services.workiq_runtime import WorkIQError, get_runtime
from src.services.workiq_setup import get_setup


def verify(setup, runtime, calendar_reader, identity: str) -> dict:
    started_at = time.monotonic()
    setup_status = setup.inspect()
    evidence = {
        "package_version": setup_status.get("installed_version"),
        "executable_architecture": platform.machine().lower(),
        "steps": {"setup": setup_status.get("state")},
    }
    if setup_status.get("state") != "mcp_unavailable":
        evidence["error_code"] = setup_status.get("state", "mcp_unavailable")
        return evidence

    snapshot = runtime.start()
    evidence.update({
        "protocol_version": snapshot.get("protocol_version"),
        "server": snapshot.get("server"),
        "capabilities": snapshot.get("allowed_capabilities", []),
        "steps": {**evidence["steps"], "initialize_and_tools_list": snapshot.get("state")},
    })
    if snapshot.get("state") != "ready":
        evidence["error_code"] = (snapshot.get("error") or {}).get(
            "code", "mcp_unavailable"
        )
        evidence["duration_seconds"] = round(time.monotonic() - started_at, 3)
        return evidence

    readiness = runtime.probe()
    ready_snapshot = runtime.snapshot()
    if not readiness.get("ok") or ready_snapshot.get("authenticated") is not True:
        evidence["steps"]["readiness"] = "blocked"
        evidence["error_code"] = (readiness.get("error") or {}).get(
            "code", "not_ready"
        )
        evidence["duration_seconds"] = round(time.monotonic() - started_at, 3)
        return evidence

    evidence["steps"]["readiness"] = "ready"
    if not identity:
        evidence["steps"]["calendar_read"] = "not_run"
        evidence["error_code"] = "identity_required"
        evidence["duration_seconds"] = round(time.monotonic() - started_at, 3)
        return evidence

    start = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    end = start + timedelta(minutes=30)
    try:
        result = calendar_reader(
            {
                "schedules": [identity],
                "startTime": {
                    "dateTime": start.strftime("%Y-%m-%dT%H:%M:%S"),
                    "timeZone": "UTC",
                },
                "endTime": {
                    "dateTime": end.strftime("%Y-%m-%dT%H:%M:%S"),
                    "timeZone": "UTC",
                },
            },
            timeout=45,
        )
    except WorkIQError as exc:
        evidence["steps"]["calendar_read"] = "failed"
        evidence["error_code"] = exc.code
        evidence["duration_seconds"] = round(time.monotonic() - started_at, 3)
        return evidence
    schedules = result.get("value") if isinstance(result, dict) else None
    if (
        not isinstance(schedules, list)
        or len(schedules) != 1
        or not isinstance(schedules[0], dict)
        or str(schedules[0].get("scheduleId") or "").strip().lower()
        != identity.lower()
        or (
            "error" in schedules[0]
            and schedules[0]["error"] is not None
        )
    ):
        evidence["steps"]["calendar_read"] = "failed"
        evidence["error_code"] = "invalid_structured_content"
        evidence["duration_seconds"] = round(time.monotonic() - started_at, 3)
        return evidence
    evidence["steps"]["calendar_read"] = "passed"
    evidence["duration_seconds"] = round(time.monotonic() - started_at, 3)
    return evidence


def main() -> int:
    runtime = get_runtime()
    try:
        evidence = verify(
            get_setup(),
            runtime,
            get_schedule,
            os.environ.get("RIVETER_WORKIQ_SELF_EMAIL", "").strip(),
        )
        print(json.dumps(evidence, sort_keys=True))
        return 0 if evidence.get("steps", {}).get("calendar_read") == "passed" else 1
    finally:
        runtime.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
