"""Manage a safe, isolated Riveter demo server."""

from __future__ import annotations

import json
import os
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
    os.environ["RIVETER_DEMO_ALLOW_TODO_PARSE"] = "1"
    os.environ["RIVETER_DEMO_ALLOW_COWORK_SESSION"] = "1"
    os.environ["RIVETER_DEMO_ALLOW_COWORK_EXECUTE"] = "1"
    os.environ["TODONESS_DB_PATH"] = str(db_path)
    os.environ["TODONESS_SETTINGS_PATH"] = str(SETTINGS_PATH)
    os.environ["TODONESS_LOG_FILE"] = str(LOG_PATH)


def _pid_record():
    try:
        value = json.loads(PID_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return value if isinstance(value, dict) else None


def _pid():
    record = _pid_record()
    try:
        return int(record["pid"]) if record else None
    except (KeyError, TypeError, ValueError):
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


def _process_identity(pid):
    if not _pid_running(pid):
        return None
    if os.name == "nt":
        import ctypes
        import ctypes.wintypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            creation = ctypes.wintypes.FILETIME()
            exit_time = ctypes.wintypes.FILETIME()
            kernel = ctypes.wintypes.FILETIME()
            user = ctypes.wintypes.FILETIME()
            if not ctypes.windll.kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return None
            size = ctypes.wintypes.DWORD(32768)
            executable = ctypes.create_unicode_buffer(size.value)
            if not ctypes.windll.kernel32.QueryFullProcessImageNameW(
                handle, 0, executable, ctypes.byref(size)
            ):
                return None
            created = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
            return {
                "pid": pid,
                "created": created,
                "executable": executable.value.lower(),
            }
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
        executable = str(Path(f"/proc/{pid}/exe").resolve()).lower()
        return {
            "pid": pid,
            "created": int(stat[21]),
            "executable": executable,
        }
    except (OSError, IndexError, ValueError):
        return None


def _demo_process(pid):
    record = _pid_record()
    if (
        not record
        or record.get("pid") != pid
        or record.get("created") is None
        or not record.get("executable")
    ):
        return False
    current = _process_identity(pid)
    return bool(
        current
        and current["created"] == record["created"]
        and current["executable"] == str(record["executable"]).lower()
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
                "cowork_api_transport": True,
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
    from src.models import create_task

    conn = get_connection()
    init_db(conn)
    conn.close()

    people = {
        "meeting": json.dumps([
            {
                "name": "Bobby Chang",
                "email": "bobby.chang@microsoft.com",
                "aad_object_id": "dbebad9c-cce5-4aa8-9df7-d4a43a80db03",
            },
            {
                "name": "Em D'Arcy",
                "email": "emdarcy@microsoft.com",
                "aad_object_id": "f5a69d2c-b748-407e-aa01-f6f3c6ac9250",
            },
        ]),
        "raj": json.dumps([{
            "name": "Raj Gopalakrishnan",
            "email": "rajgopal@microsoft.com",
            "aad_object_id": "fe1c66c5-49f4-49ab-b275-0c1eb2d2cbf6",
        }]),
        "exec_pane": json.dumps([
            {
                "name": "Adrian Maclean",
                "email": "adrian.maclean@microsoft.com",
                "aad_object_id": "de428c82-dd53-45dc-b71d-1e15084805ed",
            },
            {
                "name": "Srini Raghavan",
                "email": "srini.raghavan@microsoft.com",
                "aad_object_id": "0431fcd9-f1c7-49f5-8d9f-1ba00fc5b7cc",
            },
        ]),
    }
    create_task(
        "Review the demo run-of-show",
        "Confirm the opening story, live task flow, and closing call to action.",
        status="suggested",
        priority=3,
        source_type="manual",
        source_id="demo::run-of-show",
        coaching_text="Review the flow and accept it when the demo sequence is ready.",
        action_type="review-document",
    )
    create_task(
        "Follow up on the Kickstarter pilot feedback from Raj",
        (
            "On Tuesday, August 18, Raj Gopalakrishnan shared early reactions to "
            "the Kickstarter adoption materials in Teams and said he had several "
            "pilot notes to review. No follow-up is visible, so the next step is "
            "to collect his remaining feedback and agree on the next revision."
        ),
        status="suggested",
        priority=3,
        source_type="chat",
        source_id="chat::rajgopal@microsoft.com::pilot-feedback",
        source_snippet=(
            "On Tuesday, August 18, Raj Gopalakrishnan said in Teams that he had "
            "several pilot notes about the Kickstarter adoption materials. No "
            "later response or review meeting is visible."
        ),
        coaching_text=(
            "Draft a short Teams follow-up asking Raj for the remaining pilot "
            "feedback and the most important revision."
        ),
        action_type="follow-up",
        key_people=people["raj"],
        due_date="2026-08-24",
    )
    create_task(
        "Check whether Adrian replied about the executive pane layout",
        (
            "On Wednesday, August 19, Adrian Maclean replied to the executive "
            "pane email thread and confirmed that the revised layout works. He "
            "said no further changes are needed before the review, so this task "
            "appears complete."
        ),
        status="suggested",
        priority=3,
        source_type="email",
        source_id="email::adrian.maclean@microsoft.com::exec-pane-reply",
        source_snippet=(
            "On Wednesday, August 19, Adrian Maclean replied by email that the "
            "revised executive pane layout works and no further changes are "
            "needed before the review."
        ),
        coaching_text="This suggestion appears resolved and can be dismissed.",
        action_type="awaiting-response",
        key_people=people["exec_pane"],
    )
    create_task(
        "Confirm the demo video assets are ready to use",
        (
            "On Wednesday, August 19, Bobby Chang and Em D'Arcy confirmed during "
            "the demo preparation meeting that the final screenshots and title "
            "card were delivered to the shared folder. The previously outstanding "
            "asset request therefore appears complete."
        ),
        status="suggested",
        priority=3,
        source_type="meeting",
        source_id="meeting::demo-video-assets::delivered",
        source_snippet=(
            "On Wednesday, August 19, Bobby Chang and Em D'Arcy reported in the "
            "demo preparation meeting that all final video assets were in the "
            "shared folder and ready to use."
        ),
        coaching_text="This suggestion appears resolved and can be dismissed.",
        action_type="follow-up",
        key_people=people["meeting"],
    )
    create_task(
        "Review the run-of-show timing with Em D'Arcy",
        (
            "On Thursday, August 20, Em D'Arcy asked during the demo planning "
            "meeting for a final timing pass before the live introduction. Check "
            "the six-slide pacing and confirm the handoff to the product demo."
        ),
        status="suggested",
        priority=2,
        source_type="meeting",
        source_id="meeting::emdarcy@microsoft.com::run-of-show-timing",
        source_snippet=(
            "On Thursday, August 20, Em D'Arcy asked during the demo planning "
            "meeting for one final timing pass before the Riveter introduction."
        ),
        coaching_text=(
            "Prepare a concise timing review focused on slide pacing and the "
            "handoff to the live demo."
        ),
        action_type="prepare",
        key_people=json.dumps([json.loads(people["meeting"])[1]]),
        due_date="2026-08-21",
    )
    create_task(
        "Schedule the Friday demo review with Bobby Chang and Em D'Arcy",
        "Find a 30-minute time this week to walk through the Friday Riveter demo.",
        priority=2,
        source_type="manual",
        source_id="demo::bobby-em-schedule",
        coaching_text=(
            "Find a 30-minute working-hours slot with Bobby Chang and Em D'Arcy "
            "this week and prepare a focused demo-review agenda."
        ),
        action_type="schedule-meeting",
        key_people=people["meeting"],
        due_date="2026-08-21",
    )
    create_task(
        "Send Raj the complete Kickstarter adoption materials in Teams",
        (
            "Reply to Raj Gopalakrishnan with the complete Kickstarter adoption "
            "module and the fuller deck he asked about."
        ),
        priority=2,
        source_type="chat",
        source_id="chat::rajgopal@microsoft.com::1786489142550",
        source_url=(
            "https://teams.microsoft.com/l/message/"
            "19:08b7be88-37ac-4e2b-82af-f8bb67e5f2f7_"
            "fe1c66c549f449abb2750c1eb2d2cbf6@unq.gbl.spaces/"
            "1786489142550?context=%7B%22contextType%22:%22chat%22%7D"
        ),
        source_snippet=(
            "The adoption module only has seven slides. Is there a more complete "
            "set available?"
        ),
        coaching_text=(
            "Send Raj Gopalakrishnan the complete Kickstarter adoption resources "
            "and directly answer his question about the fuller deck."
        ),
        action_type="follow-up",
        key_people=people["raj"],
        due_date="2026-08-21",
    )
    create_task(
        "Reply to Adrian about Srini's executive pane",
        (
            "Respond to Adrian Maclean about tuning Srini Raghavan's executive "
            "pane so it shows agent status, progress, and blockers clearly."
        ),
        priority=2,
        source_type="email",
        source_id="email::adrian.maclean@microsoft.com::exec-pane",
        source_snippet=(
            "Can we report what agents and progress are deployed, then show the "
            "blockers on the remaining work?"
        ),
        coaching_text=(
            "Reply to Adrian Maclean with a concise proposal for improving Srini "
            "Raghavan's executive pane and the next iteration."
        ),
        action_type="respond-email",
        key_people=people["exec_pane"],
        due_date="2026-08-21",
    )
    create_task(
        "Waiting on the final demo video assets",
        "The latest screenshots and title card are still being finalized.",
        status="waiting",
        priority=3,
        source_type="manual",
        source_id="demo::video-assets",
        action_type="awaiting-response",
        user_notes="Check again Thursday afternoon.",
    )
    create_task(
        "Finish the isolated demo environment",
        "The fictional database, live integrations, and demo URL are ready.",
        status="completed",
        priority=2,
        source_type="manual",
        source_id="demo::environment-complete",
        action_type="general",
    )
    create_task(
        "Send the outdated demo script",
        "This version was superseded by the live run-of-show.",
        status="dismissed",
        priority=5,
        source_type="manual",
        source_id="demo::outdated-script",
        action_type="general",
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
        resolved_suggestions = {
            "email::adrian.maclean@microsoft.com::exec-pane-reply": {
                "status": "likely_resolved",
                "summary": (
                    "Adrian replied that the revised executive pane layout works "
                    "and no further changes are needed."
                ),
                "checked_at": "2026-08-20T09:00:00Z",
            },
            "meeting::demo-video-assets::delivered": {
                "status": "likely_resolved",
                "summary": (
                    "The latest meeting update says all final video assets were "
                    "delivered to the shared folder."
                ),
                "checked_at": "2026-08-20T09:00:00Z",
            },
        }
        for source_id, activity in resolved_suggestions.items():
            conn.execute(
                "UPDATE tasks SET waiting_activity=? WHERE source_id=?",
                (json.dumps(activity), source_id),
            )
        conn.commit()
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
    from src.app import (
        PARSE_CHECK_INTERVAL_MS,
        _check_unparsed,
        make_app,
        setup_logging,
    )
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
    parse_callback = tornado.ioloop.PeriodicCallback(
        _check_unparsed, PARSE_CHECK_INTERVAL_MS
    )
    parse_callback.start()
    app.parse_callback = parse_callback

    def stop_loop(*_args):
        ioloop.add_callback(ioloop.stop)

    signal.signal(signal.SIGTERM, stop_loop)
    signal.signal(signal.SIGINT, stop_loop)
    identity = _process_identity(os.getpid())
    if identity is None:
        raise RuntimeError("Could not establish demo process identity")
    PID_PATH.write_text(json.dumps(identity), encoding="utf-8")
    try:
        ioloop.start()
    finally:
        parse_callback.stop()
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
