"""Tornado application entry point for TodoNess."""

import logging
import sys
import tornado.ioloop
import tornado.web

from pathlib import Path

from .db import init_db, get_connection
from .handlers.dashboard import DashboardHandler
from .handlers.task_api import TaskListHandler, TaskDetailHandler, StatsHandler
from .handlers.task_actions import TaskActionHandler, TaskRefreshHandler
from .handlers.ws import TaskWebSocketHandler
from .handlers.sync_api import SyncStatusHandler, run_sync

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR.parent / "static"

SYNC_INTERVAL_MS = 30 * 60 * 1000  # 30 minutes


def _periodic_sync():
    """Called every 30 minutes to launch `claude -p /todo-refresh`."""
    result = run_sync()
    logger.info(f"Periodic sync: {result['message']}")


def make_app() -> tornado.web.Application:
    """Create and return the Tornado application."""
    return tornado.web.Application(
        [
            # Dashboard
            (r"/", DashboardHandler),
            # REST API
            (r"/api/tasks", TaskListHandler),
            (r"/api/tasks/(\d+)", TaskDetailHandler),
            (r"/api/tasks/(\d+)/action", TaskActionHandler),
            (r"/api/tasks/(\d+)/refresh", TaskRefreshHandler),
            (r"/api/stats", StatsHandler),
            (r"/api/sync-status", SyncStatusHandler),
            # WebSocket
            (r"/ws", TaskWebSocketHandler),
        ],
        template_path=str(TEMPLATE_DIR),
        static_path=str(STATIC_DIR),
        debug=True,
    )


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8766
    conn = get_connection()
    init_db(conn)
    conn.close()

    app = make_app()
    app.listen(port)
    print(f"TodoNess running at http://localhost:{port}")

    # Auto-sync every 30 minutes
    sync_callback = tornado.ioloop.PeriodicCallback(_periodic_sync, SYNC_INTERVAL_MS)
    sync_callback.start()
    logger.info("Periodic sync enabled (every 30 min)")

    tornado.ioloop.IOLoop.current().start()


if __name__ == "__main__":
    main()
