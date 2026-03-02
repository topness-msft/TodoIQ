"""TodoNess system tray launcher.

Runs the Tornado server in a background thread and shows a system tray icon.
Use pythonw.exe to run this file (no console window).
"""

import sys
import os
import threading
import logging
import signal
import webbrowser
import ctypes
import atexit
from logging.handlers import RotatingFileHandler
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
LOG_FILE = DATA_DIR / "todoness.log"
PID_FILE = DATA_DIR / "todoness.pid"

# Add project root so `from src.app import ...` works
sys.path.insert(0, str(PROJECT_ROOT))

# ---------------------------------------------------------------------------
# Logging (file-only, rotating)
# ---------------------------------------------------------------------------

DATA_DIR.mkdir(parents=True, exist_ok=True)

log_handler = RotatingFileHandler(
    str(LOG_FILE), maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
log_handler.setFormatter(
    logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
)
logging.basicConfig(level=logging.INFO, handlers=[log_handler])
logger = logging.getLogger("todoness_tray")

# ---------------------------------------------------------------------------
# PID management
# ---------------------------------------------------------------------------


def _is_process_alive(pid: int) -> bool:
    """Check if a process with the given PID is running (Windows)."""
    try:
        import ctypes.wintypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    except Exception:
        return False


def check_already_running() -> bool:
    """Return True if another instance is already running."""
    if not PID_FILE.exists():
        return False
    try:
        old_pid = int(PID_FILE.read_text().strip())
    except (ValueError, OSError):
        return False
    if _is_process_alive(old_pid):
        return True
    # Stale PID file — remove it
    try:
        PID_FILE.unlink()
    except OSError:
        pass
    return False


def write_pid():
    PID_FILE.write_text(str(os.getpid()))


def cleanup_pid():
    try:
        PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Dependency check
# ---------------------------------------------------------------------------


def _missing_deps_dialog(msg: str):
    """Show a Windows message box (no dependencies needed)."""
    ctypes.windll.user32.MessageBoxW(0, msg, "TodoNess", 0x10)  # MB_ICONERROR


try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    _missing_deps_dialog(
        "TodoNess requires pystray and Pillow.\n\n"
        "Run the following command and try again:\n\n"
        "    pip install pystray Pillow"
    )
    sys.exit(1)

# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------

# Shared reference so the main thread can stop the IOLoop
_ioloop = None
_ioloop_ready = threading.Event()


def server_thread():
    """Start the Tornado server in a dedicated thread with its own event loop."""
    global _ioloop
    import asyncio
    asyncio.set_event_loop(asyncio.new_event_loop())

    from src.app import start_server

    # Logging already configured at module level (RotatingFileHandler)
    app, ioloop = start_server(port=8766)
    _ioloop = ioloop
    _ioloop_ready.set()
    logger.info("Tornado IOLoop starting")
    ioloop.start()
    logger.info("Tornado IOLoop stopped")


# ---------------------------------------------------------------------------
# Tray icon helpers
# ---------------------------------------------------------------------------


def _create_icon_image() -> "Image.Image":
    """Load the TodoNess logo from static/img/icon.png, cropped to a circle."""
    logo_path = PROJECT_ROOT / "static" / "img" / "icon.png"
    if logo_path.exists():
        try:
            size = 64
            img = Image.open(str(logo_path)).resize((size, size), Image.LANCZOS).convert("RGBA")
            # Apply circular mask — keep only the circle, transparent corners
            mask = Image.new("L", (size, size), 0)
            ImageDraw.Draw(mask).ellipse([0, 0, size - 1, size - 1], fill=255)
            img.putalpha(mask)
            return img
        except Exception:
            pass

    # Fallback: draw a simple blue circle with white checkmark
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([2, 2, size - 3, size - 3], fill="#3B82F6")
    draw.line([(16, 34), (26, 46), (48, 20)], fill="white", width=5)
    return img


def on_open_dashboard(icon, item):
    webbrowser.open("http://localhost:8766")


def on_sync_now(icon, item):
    if _ioloop is None:
        return
    from src.services.claude_runner import run_claude

    def _do_sync():
        result = run_claude("/todo-refresh", label="sync")
        logger.info(f"Manual tray sync: {result['message']}")

    _ioloop.add_callback(_do_sync)


def on_stop(icon, item):
    logger.info("Stop & Exit requested from tray menu")
    if _ioloop is not None:
        _ioloop.add_callback(_ioloop.stop)
    cleanup_pid()
    icon.stop()


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------

_tray_icon = None  # set in main()


def _signal_handler(signum, frame):
    logger.info(f"Received signal {signum}, shutting down")
    if _ioloop is not None:
        _ioloop.add_callback(_ioloop.stop)
    cleanup_pid()
    if _tray_icon is not None:
        _tray_icon.stop()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    global _tray_icon

    # Single-instance check
    if check_already_running():
        ctypes.windll.user32.MessageBoxW(
            0, "TodoNess is already running.", "TodoNess", 0x40  # MB_ICONINFORMATION
        )
        sys.exit(0)

    write_pid()
    atexit.register(cleanup_pid)

    logger.info("TodoNess tray launcher starting")

    # Start Tornado in a background thread
    t = threading.Thread(target=server_thread, daemon=True)
    t.start()

    # Wait for the IOLoop to be ready (up to 15 seconds)
    if not _ioloop_ready.wait(timeout=15):
        logger.error("Tornado IOLoop did not start in time")
        cleanup_pid()
        sys.exit(1)

    logger.info("Tornado server ready")

    # Signal handlers
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    # Build tray icon
    menu = pystray.Menu(
        pystray.MenuItem("Open Dashboard", on_open_dashboard, default=True),
        pystray.MenuItem("Sync Now", on_sync_now),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Stop && Exit", on_stop),
    )

    _tray_icon = pystray.Icon("TodoNess", _create_icon_image(), "TodoNess", menu)

    logger.info("System tray icon starting")
    _tray_icon.run()

    # If we reach here, the icon was stopped
    logger.info("Tray icon stopped, exiting")


if __name__ == "__main__":
    main()
