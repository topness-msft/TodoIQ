"""Tornado application entry point for TodoNess."""

import logging
import os
import sqlite3
import sys
import threading
import time
import tornado.ioloop
import tornado.web

from datetime import datetime, timezone
from pathlib import Path

from .db import init_db, get_connection
from .handlers.dashboard import DashboardHandler
from .handlers.todoiq import TodoIQHandler
from .handlers.task_api import TaskListHandler, TaskDetailHandler, StatsHandler
from .handlers.task_actions import TaskActionHandler, TaskRefreshHandler, TaskSkillHandler
from .handlers.ws import TaskWebSocketHandler, broadcast
from .handlers.sync_api import SyncStatusHandler, RunnerStatusHandler
from .handlers.workiq_api import (
    WorkIQEulaHandler,
    WorkIQReadinessHandler,
    WorkIQStatusHandler,
)
from .handlers.cowork import (
    CoworkDestinationHandler,
    CoworkAnswerHandler,
    CoworkExecuteHandler,
    CoworkHandler,
    CoworkRefineHandler,
    CoworkRetryHandler,
)
from .models import (
    get_expired_snoozed, unsnooze_task, get_task, recover_stuck_previews,
    recover_parse_requests, select_parse_ids,
)
from .services.parsing import get_parse_service
from .services.refresh import get_refresh_service
from .services.cowork_runner import resolve_cowork_island, warm_barrier_precheck
from .services.suggestion_checks import SuggestionCheckQueue
from .services import checks
from .services.workspace_settings import (
    get_workspace_settings,
    missing_settings_warning,
)
from .services.workiq_runtime import shutdown_runtime

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR.parent / "static"

SYNC_INTERVAL_MS = 30 * 60 * 1000  # 30 minutes
UNSNOOZE_INTERVAL_MS = 60 * 1000  # 60 seconds
PARSE_CHECK_INTERVAL_MS = 30 * 1000  # 30 seconds
WAITING_CHECK_INTERVAL_MS = 4 * 60 * 60 * 1000  # 4 hours
SUGGESTION_CHECK_INTERVAL_MS = 3 * 60 * 60 * 1000  # 3 hours
SUGGESTION_QUEUE_PUMP_INTERVAL_MS = 500
BACKUP_INTERVAL_MS = 6 * 60 * 60 * 1000  # 6 hours
BACKUP_KEEP_DAYS = 7


def _periodic_sync():
    """Called every 30 minutes to launch the single direct refresh owner."""
    result = get_refresh_service().launch()
    logger.info(f"Periodic sync: {result['message']}")


def _check_waiting():
    """Called every 4 hours to check activity on waiting tasks."""
    result = checks.get_waiting_checks().launch(skip_empty=True)
    logger.info(f"Waiting check: {result['message']}")


def _check_suggestions(queue=None):
    """Called every 3 hours to check if suggested tasks are already resolved."""
    if queue is not None and queue.has_work():
        logger.info("Suggestion check: deferred behind targeted queue")
        return

    result = checks.get_checks().launch(skip_empty=True)
    logger.info(f"Suggestion check: {result['message']}")


def _backup_db():
    """Called every 6 hours to back up the database. Keeps last 7 days."""
    from .db import DB_PATH
    backup_dir = DB_PATH.parent / "backups"
    backup_dir.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"claudetodo_{stamp}.db"
    try:
        src = sqlite3.connect(str(DB_PATH))
        dst = sqlite3.connect(str(backup_path))
        src.backup(dst)
        dst.close()
        src.close()
        logger.info(f"DB backup saved: {backup_path.name}")
        # Prune old backups
        cutoff = time.time() - (BACKUP_KEEP_DAYS * 86400)
        for f in sorted(backup_dir.glob("claudetodo_*.db")):
            if f.stat().st_mtime < cutoff:
                f.unlink()
                logger.info(f"Pruned old backup: {f.name}")
    except Exception as e:
        logger.error(f"DB backup failed: {e}")


def _check_unparsed():
    """Drain an exact pending snapshot; errors require explicit retry."""
    selected = select_parse_ids()
    if selected:
        result = get_parse_service().launch(selected)
        if result["ok"]:
            logger.info("Parse check: triggered parse for %d task(s)", len(selected))


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
            (r"/todo", TodoIQHandler),
            # REST API
            (r"/api/tasks", TaskListHandler),
            (r"/api/tasks/(\d+)", TaskDetailHandler),
            (r"/api/tasks/(\d+)/action", TaskActionHandler),
            (r"/api/tasks/(\d+)/refresh", TaskRefreshHandler),
            (r"/api/tasks/(\d+)/skill", TaskSkillHandler),
            (r"/api/tasks/(\d+)/cowork", CoworkHandler),
            (r"/api/tasks/(\d+)/cowork/refine", CoworkRefineHandler),
            (r"/api/tasks/(\d+)/cowork/answer", CoworkAnswerHandler),
            (r"/api/tasks/(\d+)/cowork/execute", CoworkExecuteHandler),
            (r"/api/tasks/(\d+)/cowork/retry", CoworkRetryHandler),
            (r"/api/tasks/(\d+)/cowork/destination", CoworkDestinationHandler),
            (r"/api/stats", StatsHandler),
            (r"/api/sync-status", SyncStatusHandler),
            (r"/api/runner-status", RunnerStatusHandler),
            (r"/api/workiq/status", WorkIQStatusHandler),
            (r"/api/workiq/readiness", WorkIQReadinessHandler),
            (r"/api/workiq/eula", WorkIQEulaHandler),
            # WebSocket
            (r"/ws", TaskWebSocketHandler),
        ],
        template_path=str(TEMPLATE_DIR),
        static_path=str(STATIC_DIR),
        debug=False,
    )
    app.auto_sync_enabled = True
    app.sync_callback = None
    app.auto_suggestion_check_enabled = True
    app.suggestion_check_callback = None
    app.suggestion_check_queue = SuggestionCheckQueue()
    app.suggestion_check_queue_callback = None
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
    """Retain known intent and expose interrupted claims as retryable errors."""
    recovered = recover_parse_requests()
    if recovered:
        logger.info("Startup recovery: marked %d interrupted parse(s) for retry", recovered)


def start_server(port=8766):
    """Initialize DB, create app, register periodic callbacks, and start listening.

    Returns (app, ioloop) so the caller can manage the lifecycle.
    Does NOT start the IOLoop — call ioloop.start() yourself, or use main().
    """
    conn = get_connection()
    init_db(conn)
    conn.close()

    _recover_stuck_parses()

    # A preview left mid-flight by a restart would otherwise sit in 'previewing'
    # forever, and that task could never be previewed again.
    stranded = recover_stuck_previews()
    if stranded:
        logger.info(f"Startup recovery: failed {stranded} interrupted Cowork preview(s)")

    # Every settings reader falls back to a default when the document is
    # absent, which is correct but silent. Say it once at startup so a lost
    # settings.json is diagnosable instead of showing up days later as
    # meeting times on the hour.
    settings_warning = missing_settings_warning()
    if settings_warning:
        logger.warning(settings_warning)

    app = make_app()
    app.suggestion_check_queue.initialize_post_sync(
        lambda: app.auto_suggestion_check_enabled
    )
    app.listen(port, address="127.0.0.1")
    logger.info(f"TodoNess running at http://localhost:{port}")

    workspace = get_workspace_settings()
    if workspace.get("root"):
        logger.info(
            "Task workspace configured%s: %s",
            "" if workspace.get("enabled") else " (disabled)",
            workspace["root"],
        )

    # CMP resolution may make a network call. Warm it away from Tornado's
    # IOLoop; request handlers only read the eventually-consistent cache.
    threading.Thread(
        target=resolve_cowork_island,
        daemon=True,
        name="cowork-island-warmup",
    ).start()

    # The write-barrier precheck costs ~5.5s cold (CLI import plus an MSAL
    # silent refresh) and ~7ms warm. Warm it here so the first preview does not
    # pay that on the request path.
    threading.Thread(
        target=warm_barrier_precheck,
        daemon=True,
        name="cowork-barrier-precheck-warmup",
    ).start()

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

    # Targeted suggestion checks are server-owned and continue when no browser
    # is open. The queue itself is passive in make_app(); only a real server
    # installs this IOLoop pump.
    suggestion_queue_cb = tornado.ioloop.PeriodicCallback(
        app.suggestion_check_queue.pump_once,
        SUGGESTION_QUEUE_PUMP_INTERVAL_MS,
    )
    suggestion_queue_cb.start()
    app.suggestion_check_queue_callback = suggestion_queue_cb
    logger.info("Targeted suggestion queue enabled (FIFO, one at a time)")

    # Suggestion check every 3 hours
    suggestion_check_cb = tornado.ioloop.PeriodicCallback(
        lambda: _check_suggestions(app.suggestion_check_queue),
        SUGGESTION_CHECK_INTERVAL_MS,
    )
    suggestion_check_cb.start()
    app.suggestion_check_callback = suggestion_check_cb
    app.auto_suggestion_check_enabled = True
    logger.info("Suggestion checker enabled (every 3 hr)")

    # DB backup every 6 hours
    backup_callback = tornado.ioloop.PeriodicCallback(_backup_db, BACKUP_INTERVAL_MS)
    backup_callback.start()
    _backup_db()  # Run once at startup
    logger.info("DB backup enabled (every 6 hr, keep 7 days)")

    return app, tornado.ioloop.IOLoop.current()


def _stop_suggestion_queue(app):
    callback = getattr(app, "suggestion_check_queue_callback", None)
    if callback:
        callback.stop()


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8766
    log_file = os.environ.get("TODONESS_LOG_FILE")
    setup_logging(log_file)
    _app, ioloop = start_server(port)
    try:
        ioloop.start()
    finally:
        _stop_suggestion_queue(_app)
        shutdown_runtime()


if __name__ == "__main__":
    main()
