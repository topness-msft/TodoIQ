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

import os
import re
import shlex
import socket
import subprocess
from pathlib import Path


def _pid_alive(pid: int) -> bool:
    """Is a process with this PID currently running?

    NOT ``os.kill(pid, 0)``. That is the portable POSIX idiom, but on Windows
    ``os.kill`` ignores the signal and calls ``TerminateProcess``, so the
    "probe" actually kills the target. Using it here killed the interpreter
    outright when the PID under test was our own.

    On Windows we ask the kernel directly with ``OpenProcess``. On POSIX the
    signal-0 idiom is correct and used.
    """
    if pid <= 0:
        return False

    if os.name == "nt":
        import ctypes

        # PROCESS_QUERY_LIMITED_INFORMATION: enough to ask "does this exist",
        # and obtainable for processes we do not own.
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            # ERROR_ACCESS_DENIED means it exists but is not ours; anything
            # else (typically ERROR_INVALID_PARAMETER) means it is gone.
            return ctypes.windll.kernel32.GetLastError() == 5
        try:
            exit_code = ctypes.c_ulong()
            if ctypes.windll.kernel32.GetExitCodeProcess(
                handle, ctypes.byref(exit_code)
            ):
                # 259 == STILL_ACTIVE. A handle can outlive the process.
                return exit_code.value == 259
            return True
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        # Unknown state. Assume alive: deleting a live instance's lock is worse
        # than leaving a stale file behind.
        return True
    return True


def is_stale_pidfile(path, _alive=None) -> bool:
    """True when a PID file names a process that is no longer running.

    A deploy that stops the tray with ``Stop-Process -Force`` leaves this file
    behind, because the tray never gets to clean up. The next tray then sees a
    PID file, concludes another instance owns the port, and exits silently.
    Three consecutive deploys hit exactly that, and each looked successful.

    Only a PID that is genuinely not running counts as stale. PIDs can be
    recycled, so if *any* process holds it we leave the file alone. Unreadable
    is likewise not stale: when we cannot tell, we do not delete someone's lock.
    """
    alive = _alive or _pid_alive
    try:
        p = Path(path)
        if not p.exists():
            return False
        raw = p.read_text(encoding="utf-8").strip()
    except Exception:
        return False

    if not raw:
        return True
    try:
        pid = int(raw)
    except ValueError:
        # An unparseable lock cannot be protecting anything.
        return True
    return not alive(pid)


def clear_stale_pidfile(path, _alive=None) -> bool:
    """Remove a stale PID file. Returns True if one was actually removed.

    Fails open, like everything else here: this runs on a deploy path and must
    never be the reason a deploy aborts.
    """
    if not is_stale_pidfile(path, _alive=_alive):
        return False
    try:
        os.remove(path)
        return True
    except Exception:
        return False

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
