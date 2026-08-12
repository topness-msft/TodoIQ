"""E2E test configuration for TodoNess dashboard."""

import os
import sys
import subprocess
import time
import urllib.request
import urllib.error
import tempfile
import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
BASE_URL = 'http://127.0.0.1:18766'


def pytest_collection_modifyitems(items):
    """Mark everything under tests/e2e/ so pytest.ini can deselect it.

    This hook is global, not per-directory, so the path check is required —
    without it every test in the run gets marked.

    These tests must not share a process with the Tornado AsyncHTTPTestCase
    suite; see the comment in pytest.ini.
    """
    e2e_dir = os.path.dirname(__file__)
    for item in items:
        if str(item.fspath).startswith(e2e_dir):
            item.add_marker(pytest.mark.e2e)


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
    # Use a temporary database
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()

    env = os.environ.copy()
    env['PYTHONPATH'] = PROJECT_ROOT

    # Patch DB_PATH via environment and start server
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
import tornado.ioloop

conn = get_connection()
init_db(conn)
conn.close()

app = make_app()
app.listen(18766)
print("E2E server running on 18766", flush=True)
tornado.ioloop.IOLoop.current().start()
'''],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert _wait_for_server(BASE_URL), 'TodoNess server failed to start'
    yield server

    server.terminate()
    try:
        server.wait(timeout=5)
    except subprocess.TimeoutExpired:
        server.kill()
    os.unlink(tmp_db.name)


@pytest.fixture(scope='session')
def browser_context_args(browser_context_args):
    return {
        **browser_context_args,
        'record_video_dir': os.path.join(PROJECT_ROOT, 'test-runs', 'playwright-videos'),
        'record_video_size': {'width': 1280, 'height': 720},
        'viewport': {'width': 1280, 'height': 720},
    }


@pytest.fixture(scope='session')
def base_url(tornado_server):
    return BASE_URL


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
