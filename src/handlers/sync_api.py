"""Sync status and trigger handler.

Launches `claude -p /todo-refresh` via the shared claude_runner.
Used by the 30-min PeriodicCallback in app.py and by the dashboard's
manual sync button.
"""

import json
import logging
import tornado.web

from ..models import get_last_sync
from ..services.claude_runner import run_claude, is_running, get_status

logger = logging.getLogger(__name__)


def is_sync_running() -> bool:
    """Check if a background sync process is still running."""
    return is_running("sync")


def run_sync() -> dict:
    """Launch `claude -p /todo-refresh` if not already running."""
    return run_claude("/todo-refresh", label="sync")


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


class RunnerStatusHandler(tornado.web.RequestHandler):
    """GET /api/runner-status — status of all tracked claude subprocesses."""

    def set_default_headers(self):
        self.set_header("Content-Type", "application/json")

    def get(self):
        self.write(json.dumps(get_status()))
