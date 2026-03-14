"""Shared subprocess manager for `copilot -p` commands.

Spawns and tracks labeled subprocesses.  Different labels run in parallel;
the same label won't double-spawn.
"""

import collections
import logging
import os
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
LOG_DIR = PROJECT_ROOT / "data" / "logs"

SUBPROCESS_TIMEOUT = 300  # 5 minutes (default)

# label -> subprocess.Popen
_processes: dict[str, subprocess.Popen] = {}
# label -> open file handle (so we can close it when process finishes)
_log_files: dict[str, object] = {}
# label -> monotonic start time
_start_times: dict[str, float] = {}
# label -> per-label timeout override (seconds)
_timeouts: dict[str, float] = {}
# Recently finished process info: label -> {"exit_code": int, "error": str|None}
_exit_info: collections.OrderedDict[str, dict] = collections.OrderedDict()
_EXIT_INFO_MAX = 20


def _cleanup(label: str) -> None:
    """Close log file handle for a finished process."""
    _start_times.pop(label, None)
    _timeouts.pop(label, None)
    fh = _log_files.pop(label, None)
    if fh:
        try:
            fh.close()
        except Exception as e:
            logger.debug(f"Failed to close log file for '{label}': {e}")


def _read_log_tail(label: str, max_chars: int = 500) -> str:
    """Read the last max_chars of a subprocess log file."""
    safe_label = label.replace(":", "_").replace("/", "_")
    log_path = LOG_DIR / f"{safe_label}.log"
    if not log_path.exists():
        return "(no log file)"
    try:
        content = log_path.read_text(encoding="utf-8", errors="replace").strip()
        if len(content) > max_chars:
            return "..." + content[-max_chars:]
        return content
    except Exception:
        return "(failed to read log)"


def _set_task_error(label: str, error_message: str) -> None:
    """Set parse_status='error' and error_message on the task associated with label.

    For 'parse' labels: finds tasks in 'parsing' status and marks them as error.
    For 'skill:{name}:{id}' labels: writes error_message but does NOT change parse_status.
    """
    db_path = PROJECT_ROOT / "data" / "claudetodo.db"
    if not db_path.exists():
        return

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = sqlite3.connect(str(db_path))
    try:
        if label == "parse":
            # Mark all currently-parsing or queued tasks as error
            conn.execute(
                "UPDATE tasks SET parse_status = 'error', error_message = ?, updated_at = ? "
                "WHERE parse_status IN ('parsing', 'queued')",
                (error_message, now),
            )
            conn.commit()
            logger.info(f"[{label}] set parse_status='error' on parsing tasks")
        elif label.startswith("skill:"):
            parts = label.split(":")
            if len(parts) == 3:
                try:
                    task_id = int(parts[2])
                    conn.execute(
                        "UPDATE tasks SET error_message = ?, updated_at = ? WHERE id = ?",
                        (error_message, now, task_id),
                    )
                    conn.commit()
                    logger.info(f"[{label}] set error_message on task #{task_id}")
                except ValueError:
                    pass
    except Exception as e:
        logger.error(f"[{label}] _set_task_error failed: {e}")
    finally:
        conn.close()


def _record_exit(label: str, exit_code: int, error: str | None) -> None:
    """Cache exit info for a recently finished process."""
    _exit_info[label] = {"exit_code": exit_code, "error": error}
    # Keep only the last N entries
    while len(_exit_info) > _EXIT_INFO_MAX:
        _exit_info.popitem(last=False)


def _skill_persist(label: str) -> None:
    """Extract skill output from log file and write to DB.

    This is the PRIMARY persistence path — skill commands just output text,
    and this function captures it from the log after the process exits.

    Extraction priority:
    1. Content between <<<SKILL_OUTPUT>>> / <<<END_SKILL_OUTPUT>>> markers
    2. Full log content as-is (fallback when Claude skips markers)
    """
    if not label.startswith("skill:"):
        return

    parts = label.split(":")
    if len(parts) != 3:
        return

    skill_name, task_id_str = parts[1], parts[2]
    try:
        task_id = int(task_id_str)
    except ValueError:
        return

    db_path = PROJECT_ROOT / "data" / "claudetodo.db"
    if not db_path.exists():
        return

    # Read log file first (before opening DB) so we can bail early
    safe_label = label.replace(":", "_").replace("/", "_")
    log_path = LOG_DIR / f"{safe_label}.log"
    if not log_path.exists():
        logger.warning(f"[{label}] persist: log file not found")
        return

    try:
        log_content = log_path.read_text(encoding="utf-8", errors="replace").strip()
    except Exception as e:
        logger.error(f"[{label}] persist: failed to read log: {e}")
        return

    if not log_content:
        logger.warning(f"[{label}] persist: log file is empty")
        return

    # Try marker extraction first
    match = re.search(
        r"<<<SKILL_OUTPUT>>>\s*\n(.*?)\n\s*<<<END_SKILL_OUTPUT>>>",
        log_content,
        re.DOTALL,
    )
    if match:
        extracted = match.group(1).strip()
        source = "markers"
    else:
        extracted = log_content
        source = "full log"

    if not extracted:
        logger.warning(f"[{label}] persist: extracted content is empty")
        return

    # Route cowork-prompt output to its own column
    target_column = "cowork_prompt" if skill_name == "cowork-prompt" else "skill_output"

    conn = sqlite3.connect(str(db_path))
    try:
        # Check if target column was already written (e.g. Claude did execute DB code)
        row = conn.execute(
            f"SELECT {target_column} FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row and row[0] and target_column == "skill_output":
            logger.info(f"[{label}] persist: {target_column} already in DB, skipping")
            return

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            f"UPDATE tasks SET {target_column} = ?, suggestion_refreshed_at = ?, updated_at = ? WHERE id = ?",
            (extracted, now, now, task_id),
        )
        conn.commit()
        logger.info(
            f"[{label}] persist: saved {target_column} from {source} ({len(extracted)} chars)"
        )
    except Exception as e:
        logger.error(f"[{label}] persist failed: {e}")
    finally:
        conn.close()


def is_running(label: str) -> bool:
    """Check if a labeled process is still running.

    Side effects on process exit (natural or timeout):
    - Extracts skill output for skill labels
    - Records exit info in cache
    - Sets parse_status='error' on timeout or non-zero exit
    """
    proc = _processes.get(label)
    if proc is None:
        return False

    # Check for timeout while still running
    start = _start_times.get(label)
    timeout = _timeouts.get(label, SUBPROCESS_TIMEOUT)
    if proc.poll() is None:
        if start and (time.monotonic() - start) > timeout:
            logger.warning(f"[{label}] timed out after {timeout}s, killing")
            proc.kill()
            proc.wait()
            error_msg = f"Process timed out after {timeout // 60:.0f} minutes"
            _set_task_error(label, error_msg)
            _record_exit(label, -1, error_msg)
            _cleanup(label)
            del _processes[label]
            return False
        return True

    # Process finished naturally
    exit_code = proc.returncode
    _skill_persist(label)

    if exit_code != 0:
        log_tail = _read_log_tail(label)
        error_msg = f"Process exited with code {exit_code}: {log_tail}"
        _set_task_error(label, error_msg)
        _record_exit(label, exit_code, error_msg)
        logger.warning(f"[{label}] exited with code {exit_code}")
    else:
        _record_exit(label, 0, None)

    _cleanup(label)
    del _processes[label]
    return False


def run_copilot(command: str, label: str, timeout: float | None = None) -> dict:
    """Launch `copilot -p "<command>"` if *label* is not already running.

    Args:
        timeout: Per-process timeout in seconds. Defaults to SUBPROCESS_TIMEOUT (300s).

    Returns {"ok": True/False, "message": ...}.
    """
    if is_running(label):
        return {"ok": False, "message": f"'{label}' already running."}

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    safe_label = label.replace(":", "_").replace("/", "_")
    log_path = LOG_DIR / f"{safe_label}.log"

    try:
        fh = open(str(log_path), "w")
        proc = subprocess.Popen(
            [
                "copilot", "-p", command,
                "--allow-tool=workiq",
                "--allow-tool=shell",
                "--allow-tool=read",
                "--allow-tool=write",
                "--allow-tool=glob",
                "--allow-tool=grep",
            ],
            cwd=str(PROJECT_ROOT),
            stdout=fh,
            stderr=fh,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        _processes[label] = proc
        _log_files[label] = fh
        _start_times[label] = time.monotonic()
        if timeout is not None:
            _timeouts[label] = timeout
        logger.info(f"[{label}] started: PID {proc.pid} (timeout={timeout or SUBPROCESS_TIMEOUT}s)")
        return {"ok": True, "message": f"'{label}' started (PID {proc.pid})."}
    except FileNotFoundError:
        logger.warning("copilot CLI not found on PATH")
        return {"ok": False, "message": "copilot CLI not found on PATH."}
    except Exception as e:
        logger.error(f"[{label}] launch failed: {e}")
        return {"ok": False, "message": str(e)}


def get_exit_info(label: str | None = None) -> dict | None:
    """Return exit info for a recently finished process.

    If label is None, return all cached exit info.
    If label is given, return that entry or None.
    """
    if label is None:
        return dict(_exit_info)
    return _exit_info.get(label)


def get_status() -> dict:
    """Return dict of all tracked labels and whether they're running."""
    # Prune finished processes
    labels = list(_processes.keys())
    for label in labels:
        is_running(label)  # side-effect: removes finished

    return {label: True for label in _processes}
