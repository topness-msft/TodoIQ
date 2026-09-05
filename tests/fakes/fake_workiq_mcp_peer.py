"""Line-delimited JSON-RPC fake used only by Work IQ runtime tests."""

import argparse
import json
import os
import sys


def emit(payload):
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def trace(path, payload):
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, separators=(",", ":")) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", default="ok")
    parser.add_argument("--trace")
    args = parser.parse_args()
    call_count = 0
    list_count = 0

    if args.scenario == "stderr-secret":
        print(
            "Authorization: Bearer synthetic-secret-token "
            "refresh_token=synthetic-refresh cookie=synthetic-cookie",
            file=sys.stderr,
            flush=True,
        )

    for raw in sys.stdin:
        request = json.loads(raw)
        trace(args.trace, request)
        method = request.get("method")
        request_id = request.get("id")

        if method == "initialize":
            if args.scenario == "eof-initialize":
                return
            if args.scenario == "hang-initialize":
                continue
            protocol_version = "2025-06-18"
            capabilities = {"tools": {}}
            if args.scenario == "unsupported-protocol":
                protocol_version = "1900-01-01"
            if args.scenario == "missing-tools-capability":
                capabilities = {}
            server_name = "Other" if args.scenario == "wrong-server" else "WorkIQ"
            server_version = (
                "9.9.9" if args.scenario == "wrong-version" else "1.0.0"
            )
            emit({
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": protocol_version,
                    "capabilities": capabilities,
                    "serverInfo": {
                        "name": server_name,
                        "version": server_version,
                    },
                },
            })
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            if args.scenario == "eof-list":
                return
            list_count += 1
            if args.scenario == "hang-list":
                continue
            tools = [
                {"name": "ask_work_iq", "description": "Read-only question"},
                {"name": "do_action", "description": "Structured action"},
            ]
            if (
                args.scenario == "missing-capability"
                or (
                    args.scenario == "missing-capability-once"
                    and list_count == 1
                )
            ):
                tools = [{"name": "accept_eula"}]
            emit({
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"tools": tools},
            })
            if args.scenario == "idle-server-ping":
                emit({"jsonrpc": "2.0", "id": 9002, "method": "ping", "params": {}})
        elif method == "tools/call":
            call_count += 1
            if args.scenario == "hang":
                continue
            if args.scenario == "eof-call":
                return
            if args.scenario == "malformed":
                sys.stdout.write("not-json\n")
                sys.stdout.flush()
                continue
            response_id = request_id
            if args.scenario == "wrong-id":
                response_id = request_id + 100
            if args.scenario == "notification-first":
                emit({"jsonrpc": "2.0", "method": "notifications/progress", "params": {}})
            if args.scenario == "server-ping":
                emit({"jsonrpc": "2.0", "id": 9001, "method": "ping", "params": {}})
            tool_name = (request.get("params") or {}).get("name")
            if tool_name == "do_action":
                if args.scenario == "hang-action":
                    continue
                arguments = (request.get("params") or {}).get("arguments") or {}
                path = arguments.get("actionUrl")
                if args.scenario in {"action-eula", "action-auth", "action-consent"}:
                    messages = {
                        "action-eula": (
                            "eula_required",
                            "Accept the End User License Agreement",
                        ),
                        "action-auth": ("auth_required", "Sign in is required"),
                        "action-consent": (
                            "consent_required",
                            "Administrator consent is required",
                        ),
                    }
                    code, message = messages[args.scenario]
                    emit({
                        "jsonrpc": "2.0",
                        "id": response_id,
                        "result": {
                            "content": [{"type": "text", "text": message}],
                            "isError": True,
                            "_meta": {"code": code},
                        },
                    })
                    continue
                if args.scenario == "action-tool-error":
                    emit({
                        "jsonrpc": "2.0",
                        "id": response_id,
                        "result": {"content": [], "isError": True},
                    })
                    continue
                if args.scenario == "action-no-structured":
                    structured = None
                elif args.scenario == "action-status-string":
                    structured = {"statusCode": "200", "data": {}}
                elif args.scenario == "action-http":
                    structured = {"statusCode": 503, "data": {}}
                elif args.scenario == "action-no-data":
                    structured = {"statusCode": 200, "data": None}
                elif args.scenario == "action-wrong-list":
                    field = (
                        "meetingTimeSuggestions"
                        if path == "/me/findMeetingTimes"
                        else "value"
                    )
                    structured = {"statusCode": 200, "data": {field: {}}}
                else:
                    data = (
                        {"meetingTimeSuggestions": [], "emptySuggestionsReason": ""}
                        if path == "/me/findMeetingTimes"
                        else {"value": []}
                    )
                    structured = {"statusCode": 200, "data": data}
                result = {"content": [], "isError": False}
                if structured is not None:
                    result["structuredContent"] = structured
                emit({"jsonrpc": "2.0", "id": response_id, "result": result})
                continue
            if args.scenario in {"eula", "auth", "consent", "remote-error"}:
                messages = {
                    "eula": ("eula_required", "Accept the End User License Agreement"),
                    "auth": ("auth_required", "Sign in is required"),
                    "consent": ("consent_required", "Administrator consent is required"),
                    "remote-error": ("remote_failure", "The service rejected the request"),
                }
                code, message = messages[args.scenario]
                emit({
                    "jsonrpc": "2.0",
                    "id": response_id,
                    "result": {
                        "content": [{"type": "text", "text": message}],
                        "isError": True,
                        "_meta": {"code": code},
                    },
                })
                continue
            text = "ready"
            if args.scenario == "duplicated-ready":
                text = "readyready"
            if args.scenario == "not-ready":
                text = "Please sign in before continuing"
            if args.scenario == "oversized":
                text = "x" * (300 * 1024)
            emit({
                "jsonrpc": "2.0",
                "id": response_id,
                "result": {
                    "content": [{"type": "text", "text": text}],
                    "isError": False,
                },
            })


if __name__ == "__main__":
    main()
