"""Process-lifetime FIFO for targeted suggestion re-checks."""

from collections import OrderedDict, deque
from datetime import datetime, timezone
import logging
import uuid

from ..db import get_connection
from ..models import get_last_sync, get_task
from . import waiting_activity
from .claude_runner import get_exit_info, get_status, run_copilot


logger = logging.getLogger(__name__)

LABEL = "suggestion-check"
TIMEOUT_SECONDS = 420
VALID_RESULTS = {"likely_resolved", "still_pending", "unclear"}


class QueueFull(Exception):
    """Raised when the bounded waiting queue cannot accept another task."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _timestamp(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # Historical activity timestamps omit the UTC offset.
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _activity(task):
    signal = task.get("waiting_signal") if isinstance(task, dict) else None
    value = signal.get("activity") if isinstance(signal, dict) else None
    return value if isinstance(value, dict) else None


def _latest_full_scan():
    return get_last_sync("full_scan")


def _failed_suggestion_task_ids(connection_factory=get_connection):
    """Return every current failed suggestion-check row in stable DB order."""
    conn = connection_factory()
    try:
        rows = conn.execute(
            "SELECT id, waiting_activity FROM tasks "
            "WHERE status = 'suggested' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    selected = []
    for row in rows:
        task_id = row["id"] if hasattr(row, "keys") else row[0]
        raw = row["waiting_activity"] if hasattr(row, "keys") else row[1]
        activity = waiting_activity.normalise(raw)
        if (
            activity
            and activity.get("producer") == LABEL
            and activity.get("check_state") == waiting_activity.CHECK_FAILED
        ):
            selected.append(task_id)
    return selected


def _completion_token(completed):
    if not isinstance(completed, dict):
        return None
    run_id = completed.get("run_id")
    if isinstance(run_id, str) and run_id:
        return ("run_id", run_id)
    return (
        "unsafe",
        completed.get("started_at"),
        completed.get("finished_at"),
        completed.get("exit_code"),
        bool(completed.get("error")),
    )


def _valid_uuid(value):
    if not isinstance(value, str):
        return False
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError):
        return False
    return True


def _utc_second(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(None):
        return None
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def _marker_id(marker):
    if not isinstance(marker, dict):
        return None
    value = marker.get("id")
    return value if type(value) is int and value > 0 else None


def _is_failed_suggestion(task):
    activity = _activity(task)
    return bool(
        task
        and task.get("status") == "suggested"
        and activity
        and activity.get("producer") == LABEL
        and activity.get("check_state") == waiting_activity.CHECK_FAILED
    )


class SuggestionCheckQueue:
    """Own targeted suggestion-check ordering and exact-run completion."""

    def __init__(
        self,
        *,
        launcher=run_copilot,
        status_reader=get_status,
        completion_reader=get_exit_info,
        task_reader=get_task,
        full_scan_reader=_latest_full_scan,
        failed_task_reader=_failed_suggestion_task_ids,
        max_pending=100,
        max_terminal=100,
        clock=_utc_now,
    ):
        self._launcher = launcher
        self._status_reader = status_reader
        self._completion_reader = completion_reader
        self._task_reader = task_reader
        self._full_scan_reader = full_scan_reader
        self._failed_task_reader = failed_task_reader
        self._max_pending = max_pending
        self._max_terminal = max_terminal
        self._clock = clock
        self._pending = deque()
        self._active_job_id = None
        self._jobs = {}
        self._by_task = {}
        self._terminal = OrderedDict()
        self._last_error = None
        self._post_sync_initialized = False
        self._post_sync_enabled = False
        self._post_sync_auto_enabled = lambda: False
        self._post_sync_completion_watermark = None
        self._post_sync_full_scan_watermark = 0
        self.last_post_sync_recheck = None

    @property
    def post_sync_initialized(self):
        return self._post_sync_initialized

    def initialize_post_sync(self, auto_enabled_reader):
        """Snapshot startup history before any sync can launch."""
        if self._post_sync_initialized:
            return
        self._post_sync_auto_enabled = auto_enabled_reader
        self._post_sync_initialized = True

        try:
            completed = self._completion_reader("sync")
            marker = self._full_scan_reader()
        except Exception:
            logger.exception("Post-sync suggestion retry could not initialize")
            self._last_error = "Could not initialize post-sync suggestion retries."
            self.last_post_sync_recheck = self._post_sync_report(
                error=self._last_error
            )
            return

        marker_id = _marker_id(marker)
        if marker is not None and marker_id is None:
            logger.error("Post-sync suggestion retry found an unsafe startup marker")
            self._last_error = "Could not initialize post-sync suggestion retries."
            self.last_post_sync_recheck = self._post_sync_report(
                error=self._last_error
            )
            return

        self._post_sync_completion_watermark = _completion_token(completed)
        self._post_sync_full_scan_watermark = marker_id or 0
        self._post_sync_enabled = True

    def has_work(self) -> bool:
        return self._active_job_id is not None or bool(self._pending)

    def enqueue(self, task_id: int) -> dict:
        return self._enqueue(task_id)

    def _enqueue(self, task_id: int, *, post_sync_run_id=None) -> dict:
        existing_id = self._by_task.get(task_id)
        if existing_id:
            return self._public_job(existing_id)
        if len(self._pending) >= self._max_pending:
            raise QueueFull()

        job_id = str(uuid.uuid4())
        self._jobs[job_id] = {
            "job_id": job_id,
            "task_id": task_id,
            "state": "queued",
            "run_id": None,
            "queued_at": self._clock(),
            "started_at": None,
            "finished_at": None,
            "error": None,
            "prior_checked_at": None,
            "_post_sync_run_id": post_sync_run_id,
        }
        self._pending.append(job_id)
        self._by_task[task_id] = job_id
        return self._public_job(job_id)

    def job_for_task(self, task_id: int):
        current = self._by_task.get(task_id)
        if current:
            return self._public_job(current)
        for job_id in reversed(self._terminal):
            if self._jobs[job_id]["task_id"] == task_id:
                return self._public_job(job_id)
        return None

    def snapshot(self) -> dict:
        return {
            "process_lifetime": True,
            "pending_count": len(self._pending),
            "active": (
                self._public_job(self._active_job_id)
                if self._active_job_id else None
            ),
            "pending": [self._public_job(job_id) for job_id in self._pending],
            "terminal": [
                self._public_job(job_id) for job_id in self._terminal
            ],
            "last_error": self._last_error,
            "last_post_sync_recheck": (
                dict(self.last_post_sync_recheck)
                if self.last_post_sync_recheck else None
            ),
        }

    def pump_once(self) -> None:
        """Finalize the exact active run, then start at most one queued job."""
        try:
            running = self._status_reader() or {}
        except Exception:
            logger.exception("Suggestion check queue could not read runner status")
            self._last_error = "Could not read suggestion check status."
            return

        self._last_error = None
        active_run = (running.get("_runs") or {}).get(LABEL)

        if self._active_job_id:
            job = self._jobs[self._active_job_id]
            if active_run and active_run.get("run_id") == job["run_id"]:
                self._observe_post_sync()
                return

            try:
                completed = self._completion_reader(LABEL)
            except Exception:
                logger.exception("Suggestion check queue could not read completion")
                self._last_error = "Could not read suggestion check completion."
                return

            if completed and completed.get("run_id") == job["run_id"]:
                self._finalize(job, completed)
            else:
                self._finish(
                    job,
                    "failed",
                    "The suggestion check result could not be matched to its run.",
                )
            self._active_job_id = None

        self._observe_post_sync()

        if active_run:
            return

        while self._pending:
            job_id = self._pending.popleft()
            job = self._jobs[job_id]
            try:
                task = self._task_reader(job["task_id"])
            except Exception:
                logger.exception("Suggestion check queue could not revalidate task")
                self._finish(
                    job,
                    "failed",
                    "Could not validate this suggestion before checking it.",
                )
                return

            if not task or task.get("status") != "suggested":
                self._finish(
                    job,
                    "skipped",
                    "This task is no longer suggested, so it was skipped.",
                )
                if job["_post_sync_run_id"]:
                    self._record_post_sync_skip(job)
                continue
            if job["_post_sync_run_id"] and not _is_failed_suggestion(task):
                self._finish(
                    job,
                    "skipped",
                    "This suggestion no longer needs an automatic retry.",
                )
                self._record_post_sync_skip(job)
                continue

            previous = _activity(task)
            job["prior_checked_at"] = (
                previous.get("checked_at") if previous else None
            )
            try:
                result = self._launcher(
                    f"/suggestion-check {job['task_id']}",
                    label=LABEL,
                    timeout=TIMEOUT_SECONDS,
                )
            except Exception:
                logger.exception("Suggestion check queue launch raised")
                result = {"ok": False, "message": "launch failed"}

            if not result.get("ok"):
                message = str(result.get("message") or "").lower()
                if "already running" in message:
                    job["state"] = "queued"
                    self._pending.appendleft(job_id)
                    return
                self._finish(
                    job,
                    "failed",
                    "Could not start the suggestion check.",
                )
                return

            job["state"] = "running"
            job["run_id"] = result.get("run_id")
            job["started_at"] = result.get("started_at")
            if not job["run_id"] or not job["started_at"]:
                self._finish(
                    job,
                    "failed",
                    "The suggestion check started without safe run metadata.",
                )
                return
            self._active_job_id = job_id
            return

    def _observe_post_sync(self):
        if not self._post_sync_initialized or not self._post_sync_enabled:
            return

        try:
            completed = self._completion_reader("sync")
        except Exception:
            logger.exception("Post-sync suggestion retry could not read completion")
            self._last_error = "Could not read the latest sync completion."
            self.last_post_sync_recheck = self._post_sync_report(
                error=self._last_error
            )
            return

        token = _completion_token(completed)
        if token is None or token == self._post_sync_completion_watermark:
            return

        self._post_sync_completion_watermark = token
        run_id = completed.get("run_id") if isinstance(completed, dict) else None
        finished_at = (
            completed.get("finished_at") if isinstance(completed, dict) else None
        )

        try:
            marker = self._full_scan_reader()
        except Exception:
            logger.exception("Post-sync suggestion retry could not read full-scan marker")
            self._last_error = "Could not read the latest full-scan marker."
            self.last_post_sync_recheck = self._post_sync_report(
                run_id=run_id,
                finished_at=finished_at,
                error=self._last_error,
            )
            return

        prior_marker_id = self._post_sync_full_scan_watermark
        marker_id = _marker_id(marker)
        if marker_id is not None:
            self._post_sync_full_scan_watermark = max(
                self._post_sync_full_scan_watermark, marker_id
            )

        error = self._completion_marker_error(
            completed, marker, marker_id, prior_marker_id
        )
        if error:
            if error.startswith("Could not"):
                self._last_error = error
            self.last_post_sync_recheck = self._post_sync_report(
                run_id=run_id,
                finished_at=finished_at,
                error=error if error.startswith("Could not") else None,
            )
            return

        try:
            enabled = bool(self._post_sync_auto_enabled())
        except Exception:
            logger.exception("Post-sync suggestion retry could not read auto-check setting")
            self._last_error = "Could not read the automatic suggestion-check setting."
            self.last_post_sync_recheck = self._post_sync_report(
                run_id=run_id,
                finished_at=finished_at,
                error=self._last_error,
            )
            return

        if not enabled:
            self.last_post_sync_recheck = self._post_sync_report(
                run_id=run_id,
                finished_at=finished_at,
            )
            return

        try:
            task_ids = self._failed_task_reader()
        except Exception:
            logger.exception("Post-sync suggestion retry could not discover failed checks")
            self._last_error = "Could not discover failed suggestion checks."
            self.last_post_sync_recheck = self._post_sync_report(
                run_id=run_id,
                finished_at=finished_at,
                error=self._last_error,
            )
            return

        counts = {"accepted": 0, "deduped": 0, "overflow": 0}
        for task_id in task_ids:
            existing = task_id in self._by_task
            try:
                self._enqueue(task_id, post_sync_run_id=run_id)
            except QueueFull:
                counts["overflow"] += 1
                continue
            counts["deduped" if existing else "accepted"] += 1

        self.last_post_sync_recheck = self._post_sync_report(
            run_id=run_id,
            finished_at=finished_at,
            **counts,
        )
        if counts["overflow"]:
            logger.warning(
                "Post-sync suggestion retry queue overflowed for %d task(s)",
                counts["overflow"],
            )

    def _completion_marker_error(
        self, completed, marker, marker_id, prior_marker_id
    ):
        if not isinstance(completed, dict) or not _valid_uuid(completed.get("run_id")):
            return "Could not safely identify the completed sync."
        if completed.get("exit_code") != 0 or completed.get("error"):
            return "Sync did not complete successfully."
        if marker is None:
            return "Could not match the completed sync to a full-scan marker."
        if marker.get("sync_type") != "full_scan" or marker_id is None:
            return "Could not safely read the completed sync marker."
        if marker_id <= prior_marker_id:
            return "Sync did not produce a new full-scan marker."

        started = _utc_second(completed.get("started_at"))
        finished = _utc_second(completed.get("finished_at"))
        synced = _utc_second(marker.get("synced_at"))
        if started is None or finished is None or synced is None:
            return "Could not safely correlate the completed sync time."
        if finished < started or synced < started or synced > finished:
            return "Sync marker was outside the completed run window."
        return None

    def _post_sync_report(
        self,
        *,
        run_id=None,
        finished_at=None,
        accepted=0,
        deduped=0,
        overflow=0,
        skipped=0,
        error=None,
    ):
        return {
            "run_id": run_id,
            "sync_finished_at": finished_at,
            "processed_at": self._clock(),
            "accepted": accepted,
            "deduped": deduped,
            "overflow": overflow,
            "skipped": skipped,
            "error": error,
        }

    def _record_post_sync_skip(self, job):
        report = self.last_post_sync_recheck
        if (
            report
            and report.get("run_id") == job.get("_post_sync_run_id")
        ):
            report["skipped"] += 1

    def _finalize(self, job, completed):
        finished_at = completed.get("finished_at")
        if completed.get("exit_code") != 0 or completed.get("error"):
            code = completed.get("exit_code")
            message = (
                "The suggestion check timed out."
                if code == -1
                else f"The suggestion check process failed (code {code})."
            )
            self._finish(job, "failed", message, finished_at)
            return

        try:
            task = self._task_reader(job["task_id"])
        except Exception:
            logger.exception("Suggestion check queue could not read result task")
            self._finish(
                job,
                "failed",
                "Could not read the checked suggestion.",
                finished_at,
            )
            return

        if not task or task.get("status") != "suggested":
            self._finish(
                job,
                "skipped",
                "This task is no longer suggested, so it was skipped.",
                finished_at,
            )
            return

        result = _activity(task)
        if not self._valid_result(job, result, finished_at):
            self._finish(
                job,
                "failed",
                "The check finished without a new valid result for this suggestion.",
                finished_at,
            )
            return
        if result.get("check_state") == "failed":
            self._finish(
                job,
                "failed",
                "WorkIQ could not complete this suggestion check.",
                finished_at,
            )
            return
        self._finish(job, "succeeded", None, finished_at)

    def _valid_result(self, job, result, finished_at):
        if not result or result.get("producer") != LABEL:
            return False
        if result.get("check_state") not in {"ok", "failed"}:
            return False
        if (
            result.get("check_state") == "ok"
            and result.get("status") not in VALID_RESULTS
        ):
            return False

        checked = _timestamp(result.get("checked_at"))
        started = _timestamp(job.get("started_at"))
        finished = _timestamp(finished_at)
        prior = _timestamp(job.get("prior_checked_at"))
        if checked is None or started is None or finished is None:
            return False
        if checked < started or checked > finished:
            return False
        if prior is not None and checked <= prior:
            return False
        return True

    def _finish(self, job, state, error, finished_at=None):
        job["state"] = state
        job["finished_at"] = finished_at or self._clock()
        job["error"] = error
        self._by_task.pop(job["task_id"], None)
        self._terminal[job["job_id"]] = None
        while len(self._terminal) > self._max_terminal:
            old_job_id, _ = self._terminal.popitem(last=False)
            self._jobs.pop(old_job_id, None)

    def _public_job(self, job_id):
        job = self._jobs[job_id]
        position = None
        if job["state"] == "queued":
            try:
                position = list(self._pending).index(job_id) + 1
            except ValueError:
                position = None
        elif job["state"] == "running":
            position = 0
        return {
            "job_id": job["job_id"],
            "task_id": job["task_id"],
            "state": job["state"],
            "queue_position": position,
            "run_id": job["run_id"],
            "queued_at": job["queued_at"],
            "started_at": job["started_at"],
            "finished_at": job["finished_at"],
            "error": job["error"],
        }
