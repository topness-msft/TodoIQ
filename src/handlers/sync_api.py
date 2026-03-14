"""Sync status and trigger handler.

Launches `copilot -p /todo-refresh` via the shared claude_runner.
Used by the 30-min PeriodicCallback in app.py and by the dashboard's
manual sync button.
"""

import json
import logging
import tornado.web

from ..models import get_last_sync
from ..services.claude_runner import run_copilot, is_running, get_status, get_exit_info

logger = logging.getLogger(__name__)


def is_sync_running() -> bool:
    """Check if a background sync process is still running."""
    return is_running("sync")


def run_sync() -> dict:
    """Launch `copilot -p /todo-refresh` if not already running."""
    return run_copilot("/todo-refresh", label="sync")


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
            "auto_sync_enabled": getattr(self.application, "auto_sync_enabled", True),
        }))

    def post(self):
        try:
            body = json.loads(self.request.body) if self.request.body else {}
        except (json.JSONDecodeError, TypeError):
            body = {}

        # Toggle auto-sync if requested
        if "auto_sync" in body:
            enabled = bool(body["auto_sync"])
            self.application.auto_sync_enabled = enabled
            cb = getattr(self.application, "sync_callback", None)
            if cb:
                if enabled:
                    if not cb.is_running():
                        cb.start()
                    logger.info("Auto-sync enabled")
                else:
                    cb.stop()
                    logger.info("Auto-sync disabled")
            self.write(json.dumps({
                "ok": True,
                "auto_sync_enabled": enabled,
            }))
            return

        # On-demand waiting activity check
        if body.get("waiting_check"):
            result = run_copilot("/waiting-check", label="waiting-check")
            if not result["ok"] and "already running" not in result["message"].lower():
                self.set_status(500)
            self.write(json.dumps(result))
            return

        # Manual sync trigger (existing behavior)
        result = run_sync()
        if not result["ok"] and "already running" not in result["message"].lower():
            self.set_status(500)
        self.write(json.dumps(result))


class RunnerStatusHandler(tornado.web.RequestHandler):
    """GET /api/runner-status — status of all tracked claude subprocesses."""

    def set_default_headers(self):
        self.set_header("Content-Type", "application/json")

    def get(self):
        running = get_status()
        # Flat format for backward compat: {label: true, ...}
        # Plus "completed" key with exit info for error tracking
        result = dict(running)
        result["_completed"] = get_exit_info()
        self.write(json.dumps(result))
