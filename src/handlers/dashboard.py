"""Dashboard page handler — serves the main HTML UI."""

from pathlib import Path

import tornado.web

from ..models import get_stats, get_last_sync
from ..services.runtime_mode import demo_mode

STATIC_DIR = Path(__file__).resolve().parents[2] / "static"


class DashboardHandler(tornado.web.RequestHandler):
    """GET / — render the TodoNess dashboard."""

    def get(self):
        stats = get_stats()
        last_sync = get_last_sync()
        self.render(
            "dashboard.html",
            stats=stats,
            last_sync=last_sync,
            demo_mode=demo_mode(),
            asset_version=max(
                int((STATIC_DIR / "css" / "style.css").stat().st_mtime_ns),
                int((STATIC_DIR / "js" / "dashboard.js").stat().st_mtime_ns),
            ),
        )
