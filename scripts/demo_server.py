"""Manage a safe, isolated Riveter demo server."""

from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEMO_DIR = Path(
    os.environ.get("RIVETER_DEMO_DIR", PROJECT_ROOT / "data" / "demo")
).resolve()
PORT = int(os.environ.get("RIVETER_DEMO_PORT", "8776"))
DB_PATH = DEMO_DIR / "riveter-demo.db"
SETTINGS_PATH = DEMO_DIR / "settings.json"
LOG_PATH = DEMO_DIR / "riveter-demo.log"
PID_PATH = DEMO_DIR / "riveter-demo.pid"


def _configure(db_path=DB_PATH):
    os.environ["RIVETER_DEMO_MODE"] = "1"
    os.environ["TODONESS_DB_PATH"] = str(db_path)
    os.environ["TODONESS_SETTINGS_PATH"] = str(SETTINGS_PATH)
    os.environ["TODONESS_LOG_FILE"] = str(LOG_PATH)


def _pid():
    try:
        return int(PID_PATH.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _pid_running(pid):
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        import ctypes.wintypes

        handle = ctypes.windll.kernel32.OpenProcess(
            0x1000, False, pid  # PROCESS_QUERY_LIMITED_INFORMATION
        )
        if not handle:
            return False
        try:
            exit_code = ctypes.wintypes.DWORD()
            if not ctypes.windll.kernel32.GetExitCodeProcess(
                handle, ctypes.byref(exit_code)
            ):
                return False
            return exit_code.value == 259  # STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _demo_process(pid):
    if not _pid_running(pid):
        return False
    if os.name == "nt":
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                (
                    "(Get-CimInstance Win32_Process -Filter "
                    f"\"ProcessId = {pid}\").CommandLine"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        command_line = result.stdout
    else:
        try:
            command_line = Path(f"/proc/{pid}/cmdline").read_text(
                encoding="utf-8", errors="replace"
            ).replace("\0", " ")
        except OSError:
            return False
    return _command_is_demo_serve(command_line)


def _command_is_demo_serve(command_line):
    script = re.escape(str(Path(__file__).resolve()))
    return bool(
        re.search(
            rf'(?i)(?:"{script}"|{script})\s+serve(?:\s|$)',
            command_line or "",
        )
    )


def _port_open():
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=0.3):
            return True
    except OSError:
        return False


def _write_settings():
    DEMO_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(
        json.dumps(
            {
                "demo_mode": True,
                "cowork_api_transport": False,
                "task_workspaces": {"enabled": False},
                "meeting_preferences": {
                    "default_minutes": 25,
                    "start_offset_minutes": 5,
                    "notes": "Demo data only",
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _set_action_result(action_id, **fields):
    from src.db import get_connection

    allowed = {
        "state",
        "finding",
        "draft",
        "terminal_status",
        "tool_trace",
        "error",
        "delivery_confirmed_at",
        "completed_at",
    }
    values = {key: value for key, value in fields.items() if key in allowed}
    if not values:
        return
    conn = get_connection()
    try:
        assignments = ",".join(f"{key}=?" for key in values)
        conn.execute(
            f"UPDATE task_actions SET {assignments} WHERE id=?",
            (*values.values(), action_id),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_database(path):
    _configure(path)
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from src.db import get_connection, init_db
    from src.models import create_task, create_task_action

    conn = get_connection()
    init_db(conn)
    conn.close()

    people = {
        "maya": json.dumps(
            [{"name": "Maya Chen", "email": "maya.chen@example.test"}]
        ),
        "jonah": json.dumps(
            [{"name": "Jonah Lee", "email": "jonah.lee@example.test"}]
        ),
    }
    create_task(
        "Review Northwind launch readiness",
        "Confirm the pilot owners, launch risks, and Friday decision.",
        status="suggested",
        priority=2,
        source_type="chat",
        source_id="demo::northwind-launch",
        source_snippet="Can you pull the launch decision points together?",
        coaching_text="Summarize the decision and propose a concise follow-up.",
        action_type="follow-up",
        key_people=people["maya"],
    )
    ready = create_task(
        "Reply to Maya with the pilot recap",
        "Send the agreed outcomes and the two owners from yesterday's review.",
        priority=1,
        source_type="email",
        source_id="demo::pilot-recap",
        source_snippet="Could you send me the final recap before noon?",
        coaching_text="Draft a short recap in email form.",
        action_type="respond-email",
        key_people=people["maya"],
        due_date="2026-08-21",
    )
    action = create_task_action(
        ready["id"],
        intent=ready["coaching_text"],
        destination_kind="one_to_one",
        destination_ref="maya.chen@example.test",
        destination_display="Maya Chen",
        destination_source="demo",
        destination_confirmed_at="2026-08-19T09:00:00Z",
        delivery_channel="email",
        conversation_id="demo:user:ready",
    )
    _set_action_result(
        action["id"],
        state="ready",
        finding="The pilot is approved. Maya owns enablement; Jonah owns telemetry.",
        draft=(
            "Subject: Northwind pilot recap\n\n"
            "Hi Maya,\n\nThe pilot is approved. You have enablement, Jonah has "
            "telemetry, and Friday is the launch checkpoint.\n\nThanks,\nPhil"
        ),
        terminal_status="ok",
        tool_trace="[]",
        completed_at="2026-08-19T09:02:00Z",
    )
    schedule = create_task(
        "Schedule the Northwind decision review",
        "Find a 25-minute slot with Jonah before Friday.",
        priority=2,
        source_type="manual",
        source_id="demo::decision-review",
        coaching_text="Check Jonah's work schedule before offering times.",
        action_type="schedule-meeting",
        key_people=people["jonah"],
        due_date="2026-08-21",
    )
    blocked = {
        "invocation_id": "demo-timezone-fallback",
        "questions": [
            {
                "id": "0",
                "producer_id": "slot",
                "header": "Availability needs another check",
                "question": (
                    "I could not verify suitable working-hours slots for every "
                    "attendee. Tell me what to check or change."
                ),
                "options": [],
                "multi_select": False,
                "image_url": "",
            }
        ],
        "schedule_evidence": {
            "valid": False,
            "source": "FindMeetingTimes",
            "attendees": ["jonah.lee@example.test"],
            "working_hours_checked": False,
            "rejected_option_values": [],
        },
    }
    create_task_action(
        schedule["id"],
        intent=schedule["coaching_text"],
        conversation_id="demo:user:timezone",
        blocked_question=json.dumps(blocked, separators=(",", ":")),
        interaction_mode="interaction",
    )
    delivered = create_task(
        "Send the partner handoff",
        "Share the approved handoff summary with Maya.",
        priority=3,
        source_type="chat",
        source_id="demo::partner-handoff",
        coaching_text="Send the approved handoff note.",
        action_type="follow-up",
        key_people=people["maya"],
    )
    action = create_task_action(
        delivered["id"],
        destination_kind="one_to_one",
        destination_ref="maya.chen@example.test",
        destination_display="Maya Chen",
        destination_source="demo",
        destination_confirmed_at="2026-08-18T16:00:00Z",
        delivery_channel="teams",
        conversation_id="demo:user:delivered",
    )
    _set_action_result(
        action["id"],
        state="executed",
        finding="The approved handoff was delivered.",
        draft="Maya — the partner handoff is complete. Owners and next steps are attached.",
        terminal_status="ok",
        tool_trace="[]",
        delivery_confirmed_at="2026-08-18T16:05:00Z",
        completed_at="2026-08-18T16:05:00Z",
    )
    uncertain = create_task(
        "Confirm the telemetry update",
        "Check whether the update reached the project channel.",
        priority=3,
        source_type="chat",
        source_id="demo::telemetry-update",
        coaching_text="Post the approved telemetry update.",
        action_type="follow-up",
        key_people=people["jonah"],
    )
    action = create_task_action(
        uncertain["id"],
        destination_kind="group",
        destination_ref="demo-project-channel",
        destination_display="Northwind project channel",
        destination_source="demo",
        delivery_channel="teams",
        conversation_id="demo:user:unconfirmed",
    )
    _set_action_result(
        action["id"],
        state="execute_unconfirmed",
        draft="Telemetry is green and the dashboard is ready for Friday.",
        terminal_status="ok",
        error="Delivery could not be confirmed in the demo.",
        tool_trace="[]",
        completed_at="2026-08-18T15:00:00Z",
    )
    create_task(
        "Waiting on the security checklist",
        "Maya is confirming the final exception owners.",
        status="waiting",
        priority=2,
        source_type="email",
        source_id="demo::security-checklist",
        action_type="awaiting-response",
        key_people=people["maya"],
        user_notes="Follow up Thursday afternoon if there is no response.",
    )
    create_task(
        "Prepare Friday's launch briefing",
        "Build the five-minute executive briefing for the pilot decision.",
        status="active",
        priority=1,
        source_type="meeting",
        source_id="demo::launch-briefing",
        action_type="prepare",
        key_people=people["maya"],
        related_meeting="Northwind pilot launch review — Friday 10:00 AM",
        skill_output=(
            "## Decision\nApprove the pilot launch.\n\n"
            "## Watch items\n- Security checklist\n- Telemetry owner coverage"
        ),
    )
    create_task(
        "Publish the customer FAQ",
        "The approved FAQ is now available to the field team.",
        status="completed",
        priority=3,
        source_type="manual",
        source_id="demo::customer-faq",
        action_type="review-document",
    )
    create_task(
        "Check pilot adoption metrics",
        "Review the first-week dashboard after launch.",
        status="snoozed",
        priority=4,
        source_type="manual",
        source_id="demo::adoption-metrics",
        action_type="general",
    )

    conn = get_connection()
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode=DELETE")
    finally:
        conn.close()


def reset():
    if _demo_process(_pid()) or _port_open():
        print("ERROR: Riveter Demo is running. Stop it before reset.", file=sys.stderr)
        return 1
    PID_PATH.unlink(missing_ok=True)
    DEMO_DIR.mkdir(parents=True, exist_ok=True)
    temp_path = DEMO_DIR / "riveter-demo.reset.db"
    for path in (
        temp_path,
        Path(str(temp_path) + "-wal"),
        Path(str(temp_path) + "-shm"),
    ):
        path.unlink(missing_ok=True)
    _write_settings()
    _seed_database(temp_path)
    for path in (DB_PATH, Path(str(DB_PATH) + "-wal"), Path(str(DB_PATH) + "-shm")):
        path.unlink(missing_ok=True)
    os.replace(temp_path, DB_PATH)
    print(f"Demo data reset: {DB_PATH}")
    return 0


def serve():
    _configure()
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from src.app import make_app, setup_logging
    from src.db import get_connection, init_db
    import tornado.ioloop

    setup_logging(str(LOG_PATH))
    conn = get_connection()
    init_db(conn)
    conn.close()
    app = make_app()
    app.auto_sync_enabled = False
    app.auto_suggestion_check_enabled = False
    app.demo_mode = True
    app.listen(PORT, address="127.0.0.1")
    ioloop = tornado.ioloop.IOLoop.current()

    def stop_loop(*_args):
        ioloop.add_callback(ioloop.stop)

    signal.signal(signal.SIGTERM, stop_loop)
    signal.signal(signal.SIGINT, stop_loop)
    PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
    try:
        ioloop.start()
    finally:
        if _pid() == os.getpid():
            PID_PATH.unlink(missing_ok=True)
    return 0


def start():
    if _demo_process(_pid()) and _port_open():
        print(f"Riveter Demo is already running at http://localhost:{PORT}")
        return 0
    if _port_open():
        print(f"ERROR: Port {PORT} is already in use.", file=sys.stderr)
        return 1
    PID_PATH.unlink(missing_ok=True)
    if not DB_PATH.exists() and reset() != 0:
        return 1
    _write_settings()
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log_handle = LOG_PATH.open("a", encoding="utf-8")
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "serve"],
        cwd=PROJECT_ROOT,
        env={**os.environ, "RIVETER_DEMO_MODE": "1"},
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        creationflags=flags,
        close_fds=True,
    )
    log_handle.close()
    for _ in range(40):
        if proc.poll() is not None:
            print(f"ERROR: Demo server exited with code {proc.returncode}.", file=sys.stderr)
            return 1
        if _port_open():
            print(f"Riveter Demo: http://localhost:{PORT} (PID {proc.pid})")
            return 0
        time.sleep(0.25)
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    print("ERROR: Demo server did not become ready.", file=sys.stderr)
    return 1


def stop():
    pid = _pid()
    if not _pid_running(pid):
        PID_PATH.unlink(missing_ok=True)
        print("Riveter Demo is not running.")
        return 0
    if not _demo_process(pid):
        print(
            f"ERROR: PID {pid} is not the Riveter Demo process; refusing to stop it.",
            file=sys.stderr,
        )
        return 1
    os.kill(pid, signal.SIGTERM)
    for _ in range(40):
        if not _pid_running(pid):
            PID_PATH.unlink(missing_ok=True)
            print("Riveter Demo stopped.")
            return 0
        time.sleep(0.25)
    print(f"ERROR: Demo PID {pid} did not stop.", file=sys.stderr)
    return 1


def status():
    pid = _pid()
    running = _demo_process(pid) and _port_open()
    state = "running" if running else "stopped"
    print(f"Riveter Demo is {state}. URL: http://localhost:{PORT}")
    return 0 if running else 1


def main():
    commands = {
        "start": start,
        "stop": stop,
        "status": status,
        "reset": reset,
        "serve": serve,
    }
    command = sys.argv[1].lower() if len(sys.argv) > 1 else "start"
    if command not in commands:
        print("Usage: python scripts/demo_server.py start|stop|status|reset")
        return 2
    return commands[command]()


if __name__ == "__main__":
    raise SystemExit(main())
