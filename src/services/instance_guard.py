"""Guards against running a second TodoNess tray from another checkout.

Background: the tray derives ``PROJECT_ROOT`` from ``__file__``, so its
database, log and PID file all live beside whichever copy of the script was
launched. The single-instance PID check is therefore scoped to one checkout,
and two checkouts of this repo cannot see each other. Both bind port 8766, so
whichever starts first wins and the user sees the URL they expect serving
different code against a different database.

That happened on 2026-08-04 and cost a database fork. These helpers make the
situation visible before the server starts, rather than silently.

Everything here fails open: a guard must never be the reason TodoNess will not
start.
"""

from __future__ import annotations

import re
import shlex
import socket
import subprocess
from pathlib import Path

TASK_NAME = "TodoNess"
_SCRIPT_RE = re.compile(r"[^\s\"']+\.pyw?", re.IGNORECASE)


def parse_registered_script(arguments: str | None) -> Path | None:
    """Pull the tray script path out of a scheduled-task argument string.

    The action normally looks like ``"<pythonw.exe>" "<...>\\todoness_tray.pyw"``,
    but the interpreter is sometimes in ``Execute`` instead, so both shapes are
    accepted. Returns the last ``.pyw``/``.py`` token, which is the script.
    """
    if not arguments or not arguments.strip():
        return None
    try:
        tokens = shlex.split(arguments, posix=False)
    except ValueError:
        tokens = arguments.split()

    scripts = []
    for token in tokens:
        cleaned = token.strip("\"'")
        if cleaned.lower().endswith((".pyw", ".py")):
            scripts.append(cleaned)
    if not scripts:
        # Fall back to a regex for unquoted paths containing spaces.
        found = _SCRIPT_RE.findall(arguments)
        if not found:
            return None
        scripts = found
    return Path(scripts[-1])


def registered_tray_script(run: object = None) -> Path | None:
    """Path of the tray script the Windows scheduled task launches, if any."""
    runner = run or _query_scheduled_task
    try:
        arguments = runner(TASK_NAME)
    except Exception:
        return None
    return parse_registered_script(arguments)


def _query_scheduled_task(task_name: str) -> str | None:
    """Return the scheduled task's Arguments field, or None."""
    result = subprocess.run(
        [
            "powershell", "-NoProfile", "-Command",
            f"(Get-ScheduledTask -TaskName '{task_name}' -ErrorAction Stop)"
            ".Actions[0].Arguments",
        ],
        capture_output=True, text=True, timeout=20,
    )
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip() or None


def _same_path(a: Path, b: Path) -> bool:
    """Windows paths compare case-insensitively."""
    try:
        return str(a.resolve()).lower() == str(b.resolve()).lower()
    except OSError:
        return str(a).lower() == str(b).lower()


def checkout_mismatch(this_script: Path, registered: Path | None) -> str | None:
    """Warn when launching a tray from a checkout that is not the registered one.

    Returns None when there is nothing to say: paths match, or no startup task
    is installed (a user who never ran the installer should not be nagged).
    """
    if registered is None or _same_path(Path(this_script), Path(registered)):
        return None
    return (
        "You are starting TodoNess from a different checkout than the one that "
        "is registered to run at logon.\n\n"
        f"You launched:\n    {this_script}\n\n"
        f"The registered instance is:\n    {registered}\n\n"
        "Both copies serve port 8766 but use their OWN database, so this copy "
        "will show different tasks and any changes you make will be saved to "
        "the wrong database.\n\n"
        "To start the registered instance instead, run:\n"
        "    schtasks /run /tn TodoNess"
    )


def _tcp_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def port_owner_message(port: int, probe: object = None) -> str | None:
    """Warn when the port is already served, whoever owns it.

    The PID-file check only sees instances from the same checkout. This catches
    the case it cannot: another checkout already holding the port.
    """
    check = probe or _tcp_in_use
    try:
        if not check(port):
            return None
    except Exception:
        return None  # fail open
    return (
        f"Something is already serving port {port}, so TodoNess is most likely "
        "already running - possibly from a different checkout of this repo.\n\n"
        f"Open http://localhost:{port} to see which instance it is.\n\n"
        "To start the registered instance, first stop the running one, then:\n"
        "    schtasks /run /tn TodoNess"
    )
