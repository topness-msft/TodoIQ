"""Shared subprocess manager for `claude -p` commands.

Spawns and tracks labeled subprocesses.  Different labels run in parallel;
the same label won't double-spawn.
"""

import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
LOG_DIR = PROJECT_ROOT / "data" / "logs"

# label -> subprocess.Popen
_processes: dict[str, subprocess.Popen] = {}
# label -> open file handle (so we can close it when process finishes)
_log_files: dict[str, object] = {}


def _cleanup(label: str) -> None:
    """Close log file handle for a finished process."""
    fh = _log_files.pop(label, None)
    if fh:
        try:
            fh.close()
        except Exception:
            pass


def is_running(label: str) -> bool:
    """Check if a labeled process is still running."""
    proc = _processes.get(label)
    if proc is None:
        return False
    if proc.poll() is not None:
        _cleanup(label)
        del _processes[label]
        return False
    return True


def run_claude(command: str, label: str) -> dict:
    """Launch `claude -p "<command>"` if *label* is not already running.

    Returns {"ok": True/False, "message": ...}.
    """
    if is_running(label):
        return {"ok": False, "message": f"'{label}' already running."}

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    safe_label = label.replace(":", "_").replace("/", "_")
    log_path = LOG_DIR / f"{safe_label}.log"

    try:
        fh = open(str(log_path), "w")
        proc = subprocess.Popen(
            [
                "claude", "-p", command,
                "--no-session-persistence",
                "--allowedTools",
                "mcp__workiq__ask_work_iq,Bash,Read,Write,Glob,Grep",
            ],
            cwd=str(PROJECT_ROOT),
            env=env,
            stdout=fh,
            stderr=fh,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        _processes[label] = proc
        _log_files[label] = fh
        logger.info(f"[{label}] started: PID {proc.pid}")
        return {"ok": True, "message": f"'{label}' started (PID {proc.pid})."}
    except FileNotFoundError:
        logger.warning("claude CLI not found on PATH")
        return {"ok": False, "message": "claude CLI not found on PATH."}
    except Exception as e:
        logger.error(f"[{label}] launch failed: {e}")
        return {"ok": False, "message": str(e)}


def get_status() -> dict:
    """Return dict of all tracked labels and whether they're running."""
    # Prune finished processes
    labels = list(_processes.keys())
    for label in labels:
        is_running(label)  # side-effect: removes finished

    return {label: True for label in _processes}
