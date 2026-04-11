"""TodoIQ page handler — serves the TodoIQ dashboard."""

import os
import tornado.web


class TodoIQHandler(tornado.web.RequestHandler):
    """GET /todo — serve the TodoIQ dashboard (no template processing needed)."""

    def get(self):
        template_dir = self.application.settings.get("template_path", "templates")
        path = os.path.join(template_dir, "todoiq.html")
        with open(path, "r", encoding="utf-8") as f:
            html = f.read()
        self.set_header("Content-Type", "text/html; charset=utf-8")
        self.write(html)
