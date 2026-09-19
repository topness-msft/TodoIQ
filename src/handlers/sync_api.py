"""Sync triggers and merged legacy CLI / two direct check workflow statuses."""

import json
import logging
import tornado.web

from ..models import get_last_sync, get_task
from ..services.claude_runner import run_copilot, is_running, get_status, get_exit_info
from ..services.runtime_mode import DEMO_DISABLED_MESSAGE, demo_mode
from ..services.suggestion_checks import QueueFull, SuggestionCheckQueue
from ..services import checks

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
SUGGESTION_CHECK_BUSY_MESSAGE = (
    "A suggestion check is already running. Try again when it finishes."
)
SUGGESTION_CHECK_QUEUE_FULL_MESSAGE = (
    "The suggestion check queue is full. Try again after a queued check finishes."
)


def _suggestion_queue(application) -> SuggestionCheckQueue:
    queue = getattr(application, "suggestion_check_queue", None)
    if queue is None:
        queue = SuggestionCheckQueue()
        application.suggestion_check_queue = queue
    return queue


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
        queue = _suggestion_queue(self.application)
        self.write(json.dumps({
            "last_sync": dict(last_sync) if last_sync else None,
            "sync_running": is_sync_running(),
            "auto_sync_enabled": getattr(self.application, "auto_sync_enabled", True),
            "suggestion_check_running": bool(checks.get_checks().status().get("suggestion-check")),
            "auto_suggestion_check_enabled": getattr(self.application, "auto_suggestion_check_enabled", True),
            "suggestion_checks": queue.snapshot(),
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
        # answer to "retry this one". The direct waiting worker keeps the same
        # single-flight label for targeted and global runs.
        if body.get("waiting_check"):
            raw_id = body.get("task_id")
            if raw_id is None:
                result = checks.get_waiting_checks().launch()
            else:
                # Preserve the existing numeric-string input contract.
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
                result = checks.get_waiting_checks().launch(task_id)
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

            if targeted:
                try:
                    job = _suggestion_queue(self.application).enqueue(task_id)
                except QueueFull:
                    self.set_status(429)
                    self.write(json.dumps({
                        "ok": False,
                        "message": SUGGESTION_CHECK_QUEUE_FULL_MESSAGE,
                    }))
                    return
                self.set_status(202)
                self.write(json.dumps({
                    "ok": True,
                    "suggestion_check_job": job,
                }))
                return

            queue = _suggestion_queue(self.application)
            if queue.has_work() or checks.get_checks().status().get("suggestion-check"):
                self.set_status(409)
                self.write(json.dumps({
                    "ok": False,
                    "message": SUGGESTION_CHECK_BUSY_MESSAGE,
                }))
                return

            result = checks.get_checks().launch()

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
            self.write(json.dumps(result))
            return

        # Manual sync trigger (existing behavior)
        result = run_sync()
        if not result["ok"] and "already running" not in result["message"].lower():
            self.set_status(500)
        self.write(json.dumps(result))


class RunnerStatusHandler(tornado.web.RequestHandler):
    """GET /api/runner-status — legacy labels plus both direct check runs."""

    def set_default_headers(self):
        self.set_header("Content-Type", "application/json")

    def get(self):
        running = get_status()
        completed = get_exit_info()
        # Flat format for backward compat: {label: true, ...}
        # Plus "completed" key with exit info for error tracking
        result = checks.merged_status(running)
        result["_completed"] = checks.merged_completions(completed)
        result["_suggestion_check_queue"] = _suggestion_queue(
            self.application
        ).snapshot()

        self.write(json.dumps(result))
