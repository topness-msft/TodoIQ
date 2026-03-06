"""Tornado application entry point for TodoNess."""

import logging
import os
import sys
import tornado.ioloop
import tornado.web

from pathlib import Path

from .db import init_db, get_connection
from .handlers.dashboard import DashboardHandler
from .handlers.task_api import TaskListHandler, TaskDetailHandler, StatsHandler
from .handlers.task_actions import TaskActionHandler, TaskRefreshHandler, TaskSkillHandler
from .handlers.ws import TaskWebSocketHandler, broadcast
from .handlers.sync_api import SyncStatusHandler, RunnerStatusHandler
from .models import get_expired_snoozed, unsnooze_task, get_task
from .services.claude_runner import run_claude

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR.parent / "static"

SYNC_INTERVAL_MS = 30 * 60 * 1000  # 30 minutes
UNSNOOZE_INTERVAL_MS = 60 * 1000  # 60 seconds
PARSE_CHECK_INTERVAL_MS = 30 * 1000  # 30 seconds
WAITING_CHECK_INTERVAL_MS = 4 * 60 * 60 * 1000  # 4 hours


def _periodic_sync():
    """Called every 30 minutes to launch `claude -p /todo-refresh`."""
    result = run_claude("/todo-refresh", label="sync")
    logger.info(f"Periodic sync: {result['message']}")


def _check_waiting():
    """Called every 4 hours to check activity on waiting tasks."""
    result = run_claude("/waiting-check", label="waiting-check")
    logger.info(f"Waiting check: {result['message']}")


def _check_unparsed():
    """Called every 30 seconds to catch orphaned unparsed/queued tasks.

    If a parse subprocess was already running when a new task arrived,
    run_claude silently skipped it. This callback retriggers the parse
    once the previous one finishes, so no task stays stuck.
    """
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM tasks "
            "WHERE parse_status IN ('unparsed', 'queued') "
            "AND status NOT IN ('deleted', 'completed')"
        ).fetchone()
        count = row[0] if row else 0
    finally:
        conn.close()

    if count:
        result = run_claude("/todo-parse", label="parse")
        if result["ok"]:
            logger.info(f"Parse check: triggered parse for {count} task(s)")


def _check_snoozed():
    """Called every 60 seconds to unsnooze expired tasks."""
    expired = get_expired_snoozed()
    for tid in expired:
        task = unsnooze_task(tid)
        if task:
            logger.info(f"Auto-unsnoozed task #{tid}")
            broadcast({"type": "task_updated", "task": task})


def make_app() -> tornado.web.Application:
    """Create and return the Tornado application."""
    app = tornado.web.Application(
        [
            # Dashboard
            (r"/", DashboardHandler),
            # REST API
            (r"/api/tasks", TaskListHandler),
            (r"/api/tasks/(\d+)", TaskDetailHandler),
            (r"/api/tasks/(\d+)/action", TaskActionHandler),
            (r"/api/tasks/(\d+)/refresh", TaskRefreshHandler),
            (r"/api/tasks/(\d+)/skill", TaskSkillHandler),
            (r"/api/stats", StatsHandler),
            (r"/api/sync-status", SyncStatusHandler),
            (r"/api/runner-status", RunnerStatusHandler),
            # WebSocket
            (r"/ws", TaskWebSocketHandler),
        ],
        template_path=str(TEMPLATE_DIR),
        static_path=str(STATIC_DIR),
        debug=False,
    )
    app.auto_sync_enabled = True
    app.sync_callback = None
    return app


def setup_logging(log_file=None):
    """Configure logging. If log_file is set, logs to file; otherwise stderr."""
    handlers = []
    if log_file:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        handlers.append(logging.FileHandler(log_file))
    else:
        handlers.append(logging.StreamHandler())

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=handlers,
    )


def _recover_stuck_parses():
    """Reset tasks stuck in 'queued' or 'parsing' back to 'unparsed'.

    On restart, any subprocess that was mid-parse is gone — these tasks
    would be stuck forever without this recovery step.
    """
    conn = get_connection()
    try:
        cursor = conn.execute(
            "UPDATE tasks SET parse_status = 'unparsed' "
            "WHERE parse_status IN ('queued', 'parsing')"
        )
        conn.commit()
        if cursor.rowcount:
            logger.info(f"Startup recovery: reset {cursor.rowcount} stuck task(s) to unparsed")
    finally:
        conn.close()


def start_server(port=8766):
    """Initialize DB, create app, register periodic callbacks, and start listening.

    Returns (app, ioloop) so the caller can manage the lifecycle.
    Does NOT start the IOLoop — call ioloop.start() yourself, or use main().
    """
    conn = get_connection()
    init_db(conn)
    conn.close()

    _recover_stuck_parses()

    app = make_app()
    app.listen(port)
    logger.info(f"TodoNess running at http://localhost:{port}")

    # Auto-sync every 30 minutes
    sync_callback = tornado.ioloop.PeriodicCallback(_periodic_sync, SYNC_INTERVAL_MS)
    sync_callback.start()
    app.sync_callback = sync_callback
    app.auto_sync_enabled = True
    logger.info("Periodic sync enabled (every 30 min)")

    # Auto-unsnooze check every 60 seconds
    unsnooze_callback = tornado.ioloop.PeriodicCallback(_check_snoozed, UNSNOOZE_INTERVAL_MS)
    unsnooze_callback.start()
    logger.info("Snooze watcher enabled (every 60s)")

    # Parse orphan check every 30 seconds
    parse_callback = tornado.ioloop.PeriodicCallback(_check_unparsed, PARSE_CHECK_INTERVAL_MS)
    parse_callback.start()
    logger.info("Parse watcher enabled (every 30s)")

    # Waiting activity check every 4 hours
    waiting_callback = tornado.ioloop.PeriodicCallback(_check_waiting, WAITING_CHECK_INTERVAL_MS)
    waiting_callback.start()
    logger.info("Waiting activity checker enabled (every 4 hr)")

    return app, tornado.ioloop.IOLoop.current()


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8766
    log_file = os.environ.get("TODONESS_LOG_FILE")
    setup_logging(log_file)
    _app, ioloop = start_server(port)
    ioloop.start()


if __name__ == "__main__":
    main()
