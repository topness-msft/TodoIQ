r"""Read-only spike for one-call scheduling enrichment through Work IQ A2A.

This does not update TodoIQ or create a meeting. It requires either:

    $env:WORKIQ_ACCESS_TOKEN = "<Work IQ token>"

or an Entra public-client registration with the delegated WorkIQAgent.Ask
permission:

    python scripts\spike_workiq_a2a.py Rima `
      --client-id <app-id> --tenant-id <tenant-id>
"""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from typing import Any

import msal
import requests

WORKIQ_A2A_URL = "https://workiq.svc.cloud.microsoft/a2a/"
WORKIQ_SCOPE = "api://workiq.svc.cloud.microsoft/WorkIQAgent.Ask"
DEFAULT_TIME_ZONE = "America/New_York"
AUTH_SCHEME = "Bear" + "er"


class WorkIQError(RuntimeError):
    """A Work IQ authentication, transport, or response error."""


def normalize_token(token: str) -> str:
    value = token.strip()
    prefix = f"{AUTH_SCHEME} "
    if value.lower().startswith(prefix.lower()):
        value = value[len(prefix):].strip()
    if not value:
        raise WorkIQError("Work IQ access token is empty")
    return prefix + value


def build_question(person: str) -> str:
    return (
        f'Resolve the person Phil Topness means by "{person}" for a 1:1 meeting, '
        "then find the next three mutually available 30-minute slots during both "
        "attendees' working hours. Return only JSON with attendee "
        "{name,email,role}, alternatives (only if genuinely ambiguous), slots "
        "[{start,end,timeZone}], and explanation if fewer than three slots exist. "
        "Do not check presence and do not create or send a meeting."
    )


def build_request(
    question: str, time_zone: str, time_zone_offset: int
) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "SendMessage",
        "params": {
            "message": {
                "role": "ROLE_USER",
                "messageId": str(uuid.uuid4()),
                "parts": [{"text": question}],
                "metadata": {
                    "Location": {
                        "timeZoneOffset": time_zone_offset,
                        "timeZone": time_zone,
                    }
                },
            }
        },
    }


def validate_schedule(data: dict[str, Any]) -> dict[str, Any]:
    attendee = data.get("attendee")
    if not isinstance(attendee, dict):
        raise ValueError("Work IQ result is missing attendee")
    if not attendee.get("name"):
        raise ValueError("Work IQ attendee is missing name")

    slots = data.get("slots")
    if not isinstance(slots, list) or not slots:
        raise ValueError("Work IQ result has no slot choices")
    for slot in slots:
        if not isinstance(slot, dict) or not slot.get("start") or not slot.get("end"):
            raise ValueError("Work IQ slot is missing start or end")
        if not slot.get("timeZone"):
            raise ValueError("Work IQ slot is missing timeZone")
    return data


def extract_artifact(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("error"):
        raise WorkIQError(f"Work IQ JSON-RPC error: {payload['error']}")
    try:
        task = payload["result"]["task"]
        state = task["status"]["state"]
    except (KeyError, TypeError) as exc:
        raise WorkIQError("Work IQ response is missing task status") from exc
    if state != "TASK_STATE_COMPLETED":
        raise WorkIQError(f"Work IQ task did not complete: {state}")

    for artifact in task.get("artifacts") or []:
        for part in artifact.get("parts") or []:
            text = part.get("text")
            if not text:
                continue
            try:
                return validate_schedule(json.loads(text))
            except json.JSONDecodeError as exc:
                raise WorkIQError("Work IQ artifact was not strict JSON") from exc
    raise WorkIQError("Work IQ response has no text artifact")


def send_question(
    token: str,
    question: str,
    *,
    time_zone: str = DEFAULT_TIME_ZONE,
    time_zone_offset: int = -240,
    timeout: float = 120,
) -> dict[str, Any]:
    try:
        response = requests.post(
            WORKIQ_A2A_URL,
            headers={
                "Authorization": normalize_token(token),
                "Content-Type": "application/json",
                "A2A-Version": "1.0",
            },
            json=build_request(question, time_zone, time_zone_offset),
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise WorkIQError(f"Work IQ request failed: {exc}") from exc
    if response.status_code >= 400:
        raise WorkIQError(f"Work IQ returned HTTP {response.status_code}")
    try:
        return extract_artifact(response.json())
    except requests.JSONDecodeError as exc:
        raise WorkIQError("Work IQ returned invalid JSON") from exc


def acquire_token(client_id: str | None, tenant_id: str | None) -> str:
    env_token = os.environ.get("WORKIQ_ACCESS_TOKEN")
    if env_token:
        return env_token
    if not client_id or not tenant_id:
        raise WorkIQError(
            "Set WORKIQ_ACCESS_TOKEN or provide WORKIQ_CLIENT_ID and "
            "WORKIQ_TENANT_ID (or --client-id and --tenant-id)."
        )

    app = msal.PublicClientApplication(
        client_id,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
    )
    result = app.acquire_token_interactive(scopes=[WORKIQ_SCOPE])
    token = result.get("access_token")
    if not token:
        detail = result.get("error_description") or result.get("error") or "unknown error"
        raise WorkIQError(f"Work IQ sign-in failed: {detail}")
    return token


def run_spike(
    token: str,
    person: str,
    time_zone: str,
    time_zone_offset: int,
) -> dict[str, Any]:
    started = time.perf_counter()
    result = send_question(
        token,
        build_question(person),
        time_zone=time_zone,
        time_zone_offset=time_zone_offset,
    )
    report = {
        "transport": "Work IQ A2A v1.0 REST",
        "request_count": 1,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "result": result,
    }
    print(json.dumps(report, indent=2))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("person", help='person name or alias, for example "Rima"')
    parser.add_argument("--client-id", default=os.environ.get("WORKIQ_CLIENT_ID"))
    parser.add_argument("--tenant-id", default=os.environ.get("WORKIQ_TENANT_ID"))
    parser.add_argument("--time-zone", default=DEFAULT_TIME_ZONE)
    parser.add_argument("--time-zone-offset", type=int, default=-240)
    args = parser.parse_args()

    try:
        token = acquire_token(args.client_id, args.tenant_id)
        run_spike(token, args.person, args.time_zone, args.time_zone_offset)
    except (ValueError, WorkIQError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
