"""Sync status and trigger handler.

Launches `copilot -p /todo-refresh` via the shared claude_runner.
Used by the 30-min PeriodicCallback in app.py and by the dashboard's
manual sync button.
"""

import json
import logging
import sqlite3
import tornado.web

from ..db import get_connection
from ..models import get_last_sync, get_task
from ..services.claude_runner import run_copilot, is_running, get_status, get_exit_info
from ..services.runtime_mode import DEMO_DISABLED_MESSAGE, demo_mode

logger = logging.getLogger(__name__)

# A single-task check gets a generous budget, not a small one.
#
# This was first set to 180s on the reasoning that one task should not wait
# behind a budget sized for the whole list. That reasoning was wrong: the cost
# is dominated by WorkIQ latency, not by how many tasks are being checked. A
# check chains several calls - presence, then the thread read, then possibly a
# person-scoped fallback - and this project has measured individual WorkIQ
# calls at 95-250s. Observed single-task runs: 155s, and one that blew the 180s
# limit outright, which killed the subprocess before it could write anything -
# leaving the card showing its previous answer with the previous timestamp,
# which is the exact confusion the check exists to remove.
SINGLE_WAITING_CHECK_TIMEOUT = 420
SINGLE_SUGGESTION_CHECK_TIMEOUT = 420
SUGGESTION_CHECK_BUSY_MESSAGE = (
    "A suggestion check is already running. Try again when it finishes."
)


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
            "suggestion_check_running": is_running("suggestion-check"),
            "auto_suggestion_check_enabled": getattr(self.application, "auto_suggestion_check_enabled", True),
        }))

    def post(self):
        if demo_mode():
            self.set_status(403)
            self.write(json.dumps({"error": DEMO_DISABLED_MESSAGE}))
            return
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

        # On-demand waiting activity check.
        #
        # With a task_id this checks that ONE task. The Check Now button on a
        # card used to pass an id that was thrown away, so clicking it re-ran
        # every waiting task - one WorkIQ subprocess each - which is a poor
        # answer to "retry this one". The label stays "waiting-check" either
        # way, so claude_runner's single-flight guard still prevents a per-task
        # run and a global run from writing the same rows at once.
        if body.get("waiting_check"):
            raw_id = body.get("task_id")
            if raw_id is None:
                result = run_copilot("/waiting-check", label="waiting-check")
            else:
                # The command string reaches a subprocess, so the id may be
                # nothing but digits.
                try:
                    task_id = int(str(raw_id).strip())
                except (TypeError, ValueError):
                    self.set_status(400)
                    self.write(json.dumps({"error": "task_id must be a number"}))
                    return
                if not get_task(task_id):
                    # Falling through to the global check here would spend a
                    # run per waiting task because of a stale dashboard row.
                    self.set_status(404)
                    self.write(json.dumps({"error": "Not found"}))
                    return
                result = run_copilot(
                    f"/waiting-check {task_id}",
                    label="waiting-check",
                    timeout=SINGLE_WAITING_CHECK_TIMEOUT,
                )
            if not result["ok"] and "already running" not in result["message"].lower():
                self.set_status(500)
            self.write(json.dumps(result))
            return

        # On-demand suggestion check
        if body.get("suggestion_check"):
            targeted = "task_id" in body
            task_id = None
            if targeted:
                raw_id = body["task_id"]
                if type(raw_id) is not int or raw_id <= 0:
                    self.set_status(400)
                    self.write(json.dumps({
                        "error": "task_id must be a strict positive integer",
                    }))
                    return
                task_id = raw_id
                task = get_task(task_id)
                if not task:
                    self.set_status(404)
                    self.write(json.dumps({"error": "Not found"}))
                    return
                if task["status"] != "suggested":
                    self.set_status(409)
                    self.write(json.dumps({
                        "error": "This task is no longer suggested.",
                    }))
                    return

            if is_running("suggestion-check"):
                self.set_status(409)
                self.write(json.dumps({
                    "ok": False,
                    "message": SUGGESTION_CHECK_BUSY_MESSAGE,
                }))
                return

            if targeted:
                result = run_copilot(
                    f"/suggestion-check {task_id}",
                    label="suggestion-check",
                    timeout=SINGLE_SUGGESTION_CHECK_TIMEOUT,
                )
            else:
                conn = get_connection()
                try:
                    row = conn.execute(
                        "SELECT COUNT(*) FROM tasks WHERE status = 'suggested'"
                    ).fetchone()
                    count = row[0] if row else 0
                finally:
                    conn.close()
                timeout = 120 + (count * 60)  # 2 min base + 1 min per task
                result = run_copilot(
                    "/suggestion-check",
                    label="suggestion-check",
                    timeout=timeout,
                )

            if not result["ok"]:
                if "already running" in result["message"].lower():
                    self.set_status(409)
                    self.write(json.dumps({
                        "ok": False,
                        "message": SUGGESTION_CHECK_BUSY_MESSAGE,
                    }))
                else:
                    self.set_status(500)
                    self.write(json.dumps(result))
                return

            if targeted:
                attempt = {
                    "task_id": task_id,
                    "run_id": result["run_id"],
                    "started_at": result["started_at"],
                    "state": "running",
                }
                self.application.suggestion_check_attempt = attempt
                result = dict(result)
                result["suggestion_check_attempt"] = {
                    "task_id": task_id,
                    "run_id": result["run_id"],
                    "started_at": result["started_at"],
                }
            elif hasattr(self.application, "suggestion_check_attempt"):
                # Only a newly accepted explicit run supersedes retained
                # targeted evidence. Validation, busy, and launch failures do
                # not erase the last result another poller may still need.
                delattr(self.application, "suggestion_check_attempt")

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
        completed = get_exit_info()
        # Flat format for backward compat: {label: true, ...}
        # Plus "completed" key with exit info for error tracking
        result = dict(running)
        result["_completed"] = completed

        attempt = getattr(self.application, "suggestion_check_attempt", None)
        if attempt:
            if attempt.get("state") == "finished":
                metadata = dict(attempt)
            else:
                expected_run_id = attempt["run_id"]
                active = (running.get("_runs") or {}).get("suggestion-check")
                current_exit = completed.get("suggestion-check")
                if active and active.get("run_id") == expected_run_id:
                    metadata = {
                        "task_id": attempt["task_id"],
                        "run_id": expected_run_id,
                        "started_at": attempt["started_at"],
                        "state": "running",
                    }
                elif current_exit and current_exit.get("run_id") == expected_run_id:
                    completion = dict(current_exit)
                    metadata = {
                        "task_id": attempt["task_id"],
                        "run_id": expected_run_id,
                        "started_at": attempt["started_at"],
                        "finished_at": completion["finished_at"],
                        "state": "finished",
                        "superseded": False,
                        "completion": completion,
                    }
                    self.application.suggestion_check_attempt = metadata
                else:
                    # The exact completion was evicted/lost or another
                    # same-label run took over. Never borrow that run's result.
                    metadata = {
                        "task_id": attempt["task_id"],
                        "run_id": expected_run_id,
                        "started_at": attempt["started_at"],
                        "state": "finished",
                        "superseded": True,
                    }
                    self.application.suggestion_check_attempt = metadata

            result["_suggestion_check_attempt"] = metadata

        self.write(json.dumps(result))
