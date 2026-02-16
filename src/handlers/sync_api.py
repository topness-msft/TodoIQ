"""Sync status and trigger handler.

Launches `claude -p /todo-refresh` as a subprocess.  Used by the 30-min
PeriodicCallback in app.py and by the dashboard's manual sync button.
"""

import json
import os
import subprocess
import logging
import tornado.web
from pathlib import Path

from ..models import get_last_sync

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SYNC_LOG_FILE = PROJECT_ROOT / "data" / "sync_output.log"

# Track the running subprocess in-process (no PID files needed)
_sync_proc: subprocess.Popen | None = None


def is_sync_running() -> bool:
    """Check if a background sync process is still running."""
    global _sync_proc
    if _sync_proc is None:
        return False
    if _sync_proc.poll() is not None:
        _sync_proc = None
        return False
    return True


def run_sync() -> dict:
    """Launch `claude -p /todo-refresh` if not already running."""
    global _sync_proc
    if is_sync_running():
        return {"ok": False, "message": "Sync already running."}

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)

    try:
        SYNC_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        log_file = open(str(SYNC_LOG_FILE), "w")
        _sync_proc = subprocess.Popen(
            [
                "claude", "-p", "/todo-refresh",
                "--no-session-persistence",
                "--allowedTools",
                "mcp__workiq__ask_work_iq,Bash,Read,Write,Glob,Grep",
            ],
            cwd=str(PROJECT_ROOT),
            env=env,
            stdout=log_file,
            stderr=log_file,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        logger.info(f"Sync started: PID {_sync_proc.pid}")
        return {"ok": True, "message": f"Sync started (PID {_sync_proc.pid})."}
    except FileNotFoundError:
        logger.warning("claude CLI not found on PATH")
        return {"ok": False, "message": "claude CLI not found on PATH."}
    except Exception as e:
        logger.error(f"Sync launch failed: {e}")
        return {"ok": False, "message": str(e)}


class SyncStatusHandler(tornado.web.RequestHandler):
    """GET /api/sync-status — last sync info + running state.
    POST /api/sync-status — launch sync subprocess.
    """

    def set_default_headers(self):
        self.set_header("Content-Type", "application/json")

    def get(self):
        last_sync = get_last_sync("full_scan") or get_last_sync("flagged_emails")
        self.write(json.dumps({
            "last_sync": dict(last_sync) if last_sync else None,
            "sync_running": is_sync_running(),
        }))

    def post(self):
        result = run_sync()
        if not result["ok"] and "already running" not in result["message"].lower():
            self.set_status(500)
        self.write(json.dumps(result))
