"""TodoIQ page handler — serves the alternate TodoIQ UI."""

import os
import tornado.web


class TodoIQHandler(tornado.web.RequestHandler):
    """GET /todo — serve the TodoIQ UI with real API wiring."""

    def get(self):
        # Serve the mock HTML directly, with API adapter injected
        static_dir = self.application.settings.get("static_path", "static")
        mock_path = os.path.join(static_dir, "mock-todo.html")

        with open(mock_path, "r", encoding="utf-8") as f:
            html = f.read()

        # Inject the API adapter script with cache-busting hash
        import hashlib
        adapter_path = os.path.join(static_dir, "js", "todoiq-api.js")
        try:
            with open(adapter_path, "rb") as af:
                h = hashlib.md5(af.read()).hexdigest()[:8]
        except FileNotFoundError:
            h = "0"
        adapter_tag = f'<script src="/static/js/todoiq-api.js?v={h}"></script>'
        html = html.replace("</body>", adapter_tag + "\n</body>")

        self.set_header("Content-Type", "text/html; charset=utf-8")
        self.set_header("Cache-Control", "no-cache")
        self.write(html)
