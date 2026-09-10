"""WebSocket handler for live dashboard updates."""

import json
from urllib.parse import urlsplit

import tornado.web
import tornado.websocket

# Connected clients
_clients: set[tornado.websocket.WebSocketHandler] = set()


class TaskWebSocketHandler(tornado.websocket.WebSocketHandler):
    """WebSocket endpoint at /ws for real-time task updates."""

    _LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}

    @staticmethod
    def _origin_parts(value: str):
        try:
            parsed = urlsplit(value)
            if (
                parsed.scheme not in {"http", "https", "ws", "wss"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                return None
            scheme = {"ws": "http", "wss": "https"}.get(
                parsed.scheme, parsed.scheme
            )
            port = parsed.port or (443 if scheme == "https" else 80)
            return scheme, parsed.hostname.rstrip(".").lower(), port
        except (ValueError, TypeError):
            return None

    def _request_origin(self):
        try:
            parsed = urlsplit(f"//{self.request.host}")
            host = (parsed.hostname or "").rstrip(".").lower()
            if host not in self._LOOPBACK_HOSTS:
                return None
            scheme = {"ws": "http", "wss": "https"}.get(
                self.request.protocol, self.request.protocol
            )
            port = parsed.port or (443 if scheme == "https" else 80)
            return scheme, host, port
        except (ValueError, TypeError):
            return None

    def prepare(self):
        if self._request_origin() is None:
            raise tornado.web.HTTPError(403)

    def check_origin(self, origin):
        request_origin = self._request_origin()
        if request_origin is None:
            return False
        if origin is None:
            return True
        return self._origin_parts(origin) == request_origin

    def open(self):
        _clients.add(self)

    def on_close(self):
        _clients.discard(self)

    def on_message(self, message):
        # Clients don't send messages; this is a push-only channel
        pass


def broadcast(data: dict):
    """Send a JSON message to all connected WebSocket clients."""
    msg = json.dumps(data)
    dead = []
    for client in _clients:
        try:
            client.write_message(msg)
        except tornado.websocket.WebSocketClosedError:
            dead.append(client)
    for client in dead:
        _clients.discard(client)


def broadcast_error(task_id: int, error_message: str):
    """Broadcast a parse_error event for a specific task."""
    broadcast({
        "type": "parse_error",
        "task_id": task_id,
        "error_message": error_message,
    })
