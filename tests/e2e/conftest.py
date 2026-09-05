"""E2E test configuration for TodoNess dashboard."""

import os
import sys
import sqlite3
import subprocess
import time
import urllib.request
import urllib.error
import tempfile
import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
BASE_URL = 'http://127.0.0.1:18766'

# Set when the session-scoped server starts, so the per-test reset below can
# reach the same database the server is serving.
_DB_PATH = None

# Deleted before every test. Children first: task_actions/task_person carry
# foreign keys onto tasks.
_SEEDED_TABLES = ('task_actions', 'task_context', 'task_person', 'tasks')

# Opt-in browser video recording; see browser_context_args below.
RECORD_VIDEO = os.environ.get('RIVETER_E2E_VIDEO') == '1'


def pytest_collection_modifyitems(items):
    """Mark everything under tests/e2e/ so pytest.ini can deselect it.

    This hook is global, not per-directory, so the path check is required —
    without it every test in the run gets marked.

    These tests must not share a process with the Tornado AsyncHTTPTestCase
    suite; see the comment in pytest.ini.

    A per-test timeout is attached here rather than in pytest.ini so it applies
    to browser tests only. A wedged Playwright call otherwise stalls the whole
    run with no indication of which test is stuck; failing at 90s names it.
    """
    e2e_dir = os.path.dirname(__file__)
    for item in items:
        if str(item.fspath).startswith(e2e_dir):
            item.add_marker(pytest.mark.e2e)
            if item.get_closest_marker('timeout') is None:
                item.add_marker(pytest.mark.timeout(90, method='thread'))


def _wait_for_server(url, timeout=15):
    """Wait for the server to become ready."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = urllib.request.urlopen(url + '/api/stats')
            if resp.status == 200:
                return True
        except (urllib.error.URLError, ConnectionError):
            time.sleep(0.3)
    return False


@pytest.fixture(scope='session')
def tornado_server():
    """Start a fresh TodoNess server with a temp database."""
    global _DB_PATH
    # Use a temporary database
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    _DB_PATH = tmp_db.name

    env = os.environ.copy()
    env['PYTHONPATH'] = PROJECT_ROOT
    # The project's cp1252 lesson applies to the child too: force UTF-8 so a
    # non-ASCII log line cannot take the server down.
    env['PYTHONIOENCODING'] = 'utf-8'

    # Server output goes to a FILE, never to a pipe.
    #
    # This used to be stdout=PIPE, stderr=PIPE with nothing draining either
    # pipe. Tornado logs handler tracebacks and warnings to stderr, so once the
    # server had written more than the OS pipe buffer (a few KB) it blocked
    # forever inside write() — and because the block is in the single-threaded
    # IOLoop, the process kept the listening socket open while answering
    # nothing. The suite saw a server that accepted connections and never
    # replied: every test from that point on failed on a 30s selector timeout,
    # which read as "~38 broken tests" rather than one wedged process. Files
    # passed on their own because a short run never filled the buffer.
    #
    # A file can always be written, so the deadlock cannot recur, and the log
    # is kept for diagnosis.
    log_path = os.path.join(PROJECT_ROOT, 'test-runs', 'e2e-server.log')
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    log_handle = open(log_path, 'w', encoding='utf-8', errors='replace')

    server = subprocess.Popen(
        [sys.executable, '-c', f'''
import sys, os
sys.path.insert(0, r"{PROJECT_ROOT}")
os.environ["CLAUDETODO_DB"] = r"{tmp_db.name}"

# Patch db module before anything imports it
import src.db as db_module
from pathlib import Path
db_module.DB_PATH = Path(r"{tmp_db.name}")

from src.app import make_app
from src.db import get_connection, init_db
import src.handlers.workiq_api as workiq_api
from src.services.workiq_setup import EULA_URL
import tornado.ioloop
import tornado.web

class E2EWorkIQSetup:
    def inspect(self):
        return {{
            "state": "mcp_unavailable",
            "required_version": "1.0.0",
            "installed_version": "1.0.0",
            "eula_url": EULA_URL,
            "install_command": r"powershell -ExecutionPolicy Bypass -File scripts\\setup_workiq.ps1",
        }}

    def accept_eula(self):
        return {{"ok": True, "message": "Work IQ EULA accepted. Check readiness again."}}

class E2EWorkIQRuntime:
    def __init__(self):
        self.state = "eula_required"

    def snapshot(self):
        ready = self.state == "ready"
        return {{
            "state": self.state,
            "error": (
                {{"code": "eula_required", "message": "Work IQ requires EULA acceptance."}}
                if self.state == "eula_required" else None
            ),
            "protocol_version": "2025-06-18" if ready else None,
            "server": {{"name": "WorkIQ", "version": "1.0.0"}} if ready else None,
            "allowed_capabilities": ["ask_work_iq", "do_action"] if ready else [],
            "authenticated": ready,
            "active_job_id": None,
            "queue_depth": 0,
        }}

    def shutdown(self):
        self.state = "stopped"

    def invalidate_setup_state(self):
        self.state = "stopped"

    def probe(self):
        self.state = "ready"
        return {{"ok": True}}

e2e_workiq_setup = E2EWorkIQSetup()
e2e_workiq_runtime = E2EWorkIQRuntime()
workiq_api.get_setup = lambda: e2e_workiq_setup
workiq_api.get_runtime = lambda: e2e_workiq_runtime

class GracefulShutdownHandler(tornado.web.RequestHandler):
    def post(self):
        from src.services.workiq_runtime import shutdown_runtime
        shutdown_runtime()
        self.write({{"ok": True}})
        tornado.ioloop.IOLoop.current().add_callback(
            tornado.ioloop.IOLoop.current().stop
        )

conn = get_connection()
init_db(conn)
conn.close()

app = make_app()
app.add_handlers(r".*$", [(r"/__test/shutdown", GracefulShutdownHandler)])
app.listen(18766)
print("E2E server running on 18766", flush=True)
tornado.ioloop.IOLoop.current().start()
'''],
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )

    assert _wait_for_server(BASE_URL), (
        f'TodoNess server failed to start; see {log_path}'
    )
    yield server

    try:
        request = urllib.request.Request(
            BASE_URL + '/__test/shutdown', data=b'', method='POST'
        )
        urllib.request.urlopen(request, timeout=5).read()
        server.wait(timeout=5)
    except (urllib.error.URLError, subprocess.TimeoutExpired):
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
    log_handle.close()
    os.unlink(tmp_db.name)


@pytest.fixture(scope='session')
def browser_context_args(browser_context_args):
    """Browser context defaults.

    Video recording is OPT-IN. Recording every context left 17,062 files and
    2.7 GB under test-runs/playwright-videos with nothing ever cleaning them
    up, and writing each new recording into a directory that large degraded the
    full run until it wedged partway through — while the same files passed
    quickly on their own. Set RIVETER_E2E_VIDEO=1 when a recording is actually
    wanted (debugging a flake, capturing a demo).
    """
    args = {
        **browser_context_args,
        'viewport': {'width': 1280, 'height': 720},
    }
    if RECORD_VIDEO:
        args['record_video_dir'] = os.path.join(
            PROJECT_ROOT, 'test-runs', 'playwright-videos'
        )
        args['record_video_size'] = {'width': 1280, 'height': 720}
    return args


@pytest.fixture(scope='session')
def base_url(tornado_server):
    return BASE_URL


@pytest.fixture(autouse=True)
def reset_seeded_tasks(tornado_server):
    """Give every test an empty task table.

    The server keeps ONE database for the whole session, and a test that fails
    part-way never reaches its own `finally: _delete(...)`. Leaked rows used to
    accumulate silently until they crossed a cap: list_tasks serves 200 rows
    ordered by (priority ASC, created_at DESC) (src/models.py:241), so past that
    point a freshly seeded task was simply not in the payload the dashboard
    fetched. The symptom was whole files timing out at 30s on
    `wait_for_function("tasks.find(t => t.id === N)")` in the full run while
    passing on their own - test_snooze_days (27), test_person_picklist (3) and
    test_riveter_visuals (4) all did exactly that.

    Clearing here makes each test depend only on what it seeds itself, rather
    than on how many tests happened to run before it.
    """
    _clear_tasks()
    yield


def _clear_tasks():
    if not _DB_PATH:
        return
    conn = sqlite3.connect(_DB_PATH, timeout=10)
    try:
        conn.execute('PRAGMA foreign_keys = OFF')
        for table in _SEEDED_TABLES:
            try:
                conn.execute(f'DELETE FROM {table}')
            except sqlite3.OperationalError:
                # Table not present in this schema revision; nothing to clear.
                pass
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def context(browser, browser_context_args):
    """Custom context with click indicator for video recordings."""
    ctx = browser.new_context(**browser_context_args)
    ctx.add_init_script("""
        document.addEventListener('click', function(e) {
            var ring = document.createElement('div');
            ring.style.cssText = 'position:fixed;width:30px;height:30px;border:3px solid red;' +
                'border-radius:50%;z-index:2147483647;pointer-events:none;' +
                'transform:translate(-50%,-50%);transition:opacity 0.6s,transform 0.6s;';
            ring.style.left = e.clientX + 'px';
            ring.style.top = e.clientY + 'px';
            document.documentElement.appendChild(ring);
            requestAnimationFrame(function() {
                ring.style.transform = 'translate(-50%,-50%) scale(2)';
                ring.style.opacity = '0';
            });
            setTimeout(function() { ring.remove(); }, 700);
        }, true);
    """)
    yield ctx
    ctx.close()
