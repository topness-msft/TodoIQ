"""Process-owned standalone requests; no CLI, parse lifecycle or polling writes."""

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import threading
import time
import uuid

from ..db import get_connection
from . import generation
from .runtime_mode import DEMO_DISABLED_MESSAGE, demo_mode
from .workiq_runtime import CancelledError, TimeoutError as WorkIQTimeoutError, get_runtime


VALID_SKILLS = frozenset(skill.value for skill in generation.Skill)
FAILURE_MESSAGE = "Skill output was not updated. Review the task and retry."
MAX_RUNNING = 32
MAX_COMPLETED = 100


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class _Request:
    run_id: str
    task_id: int
    skill: generation.Skill
    captured: generation.CapturedInput
    started_at: str
    deadline: float
    cancel: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)

    @property
    def label(self):
        return f"skill:{self.skill.value}:{self.task_id}"

    @property
    def target(self):
        return self.task_id, self.captured.target

    def metadata(self):
        return {"run_id": self.run_id, "label": self.label, "started_at": self.started_at}


class SkillService:
    """Small bounded registry. One ordering lock covers every target's admission/CAS."""

    def __init__(self, *, runtime_provider=None, monotonic=None):
        self._runtime_provider = runtime_provider or get_runtime
        self._monotonic = monotonic or time.monotonic
        self._lock = threading.RLock()
        self._active = {}
        self._latest = {}
        self._completed = OrderedDict()

    def launch(self, task_id, skill, *, timeout=420):
        if demo_mode():
            return {"ok": False, "state": "disabled", "message": DEMO_DISABLED_MESSAGE}
        try:
            skill = generation.Skill(skill)
            if type(task_id) is not int or task_id <= 0 or not 0 < timeout <= 3600:
                raise ValueError("Invalid request")
        except (ValueError, TypeError):
            return {"ok": False, "message": "Invalid skill request."}
        label = f"skill:{skill.value}:{task_id}"
        with self._lock:
            if label in self._active:
                return {"ok": False, "message": f"'{label}' already running."}
            if len(self._active) >= MAX_RUNNING:
                return {"ok": False, "message": "Too many skills are running. Retry shortly."}
            deadline = self._monotonic() + timeout
            conn = get_connection()
            try:
                row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
                if row is None:
                    return {"ok": False, "message": "Task not found."}
                captured = generation.capture(dict(row), skill)
            except Exception:
                return {"ok": False, "message": FAILURE_MESSAGE}
            finally:
                conn.close()
            request = _Request(str(uuid.uuid4()), task_id, skill, captured, _now(), deadline)
            self._active[label] = request
            self._latest[request.target] = request.run_id
            thread = threading.Thread(target=self._execute, args=(request,),
                                      name="standalone-skill", daemon=True)
            try:
                thread.start()
            except Exception:
                self._finish(request, "failed")
                return {"ok": False, "message": FAILURE_MESSAGE}
            return {"ok": True, "message": f"'{label}' started.", **request.metadata()}

    def _remaining(self, request):
        if request.cancel.is_set():
            raise CancelledError("Skill cancelled")
        remaining = request.deadline - self._monotonic()
        if remaining <= 0:
            raise WorkIQTimeoutError("Skill deadline expired")
        return remaining

    def _execute(self, request):
        state = "failed"
        try:
            # The runtime is request-owned as a reference, not shut down on cancellation.
            # Its transport may be shared with unrelated read-only workflows.
            runtime = None
            def provider():
                nonlocal runtime
                if runtime is None:
                    runtime = self._runtime_provider()
                return runtime
            context = generation.GenerationContext(provider, lambda: self._remaining(request))
            output = generation.generate(request.captured, context)
            with self._lock:
                self._remaining(request)
                if self._latest.get(request.target) != request.run_id:
                    state = "superseded"
                else:
                    state = "succeeded" if self._persist(request, output) else "stale"
        except CancelledError:
            state = "cancelled"
        except WorkIQTimeoutError:
            state = "timed_out"
        except Exception:
            # Never retain/log exception text, provider answers, prompts or task data.
            state = "failed"
        finally:
            self._finish(request, state)

    def _persist(self, request, output):
        """Caller holds ordering lock from latest-token check through SQL commit."""
        captured = request.captured
        if json.dumps(generation.settings_for(request.skill), sort_keys=True) != captured.settings_json:
            return False
        self._remaining(request)
        conn = get_connection()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                self._remaining(request)
                stamp = _now()
                cursor = conn.execute(
                    f"UPDATE tasks SET {captured.target}=?, suggestion_refreshed_at=?, updated_at=? "
                    "WHERE id=? AND " + " AND ".join(f"{key} IS ?" for key, _ in captured.fields),
                    (output, stamp, stamp, request.task_id, *(value for _, value in captured.fields)),
                )
                self._remaining(request)
            return cursor.rowcount == 1
        finally:
            conn.close()

    def _finish(self, request, state):
        with self._lock:
            if self._active.get(request.label) is not request:
                return
            self._completed[request.run_id] = {
                **request.metadata(), "state": state, "finished_at": _now(),
                "persisted": state == "succeeded", "exit_code": 0 if state == "succeeded" else 1,
                "error": None if state == "succeeded" else FAILURE_MESSAGE,
            }
            self._active.pop(request.label)
            # Keep the newest (including failed) token until all older work has left.
            if not any(other.target == request.target for other in self._active.values()):
                self._latest.pop(request.target, None)
            while len(self._completed) > MAX_COMPLETED:
                self._completed.popitem(last=False)
            request.done.set()

    def cancel(self, run_id):
        with self._lock:
            for request in self._active.values():
                if request.run_id == run_id:
                    request.cancel.set()
                    return True
            return False

    def join(self, run_id, timeout=None):
        with self._lock:
            request = next((r for r in self._active.values() if r.run_id == run_id), None)
        if request:
            request.done.wait(timeout)

    def status(self):
        with self._lock:
            return {**{label: True for label in self._active},
                    "_runs": {label: {**r.metadata(), "state": "running"} for label, r in self._active.items()}}

    def completion(self, run_id):
        with self._lock:
            value = self._completed.get(run_id)
            return dict(value) if value else None

    def completions(self):
        with self._lock:
            result = {}
            for value in self._completed.values():
                # An older different-skill run cannot replace this label's new record.
                previous = result.get(value["label"])
                if previous is None or previous["started_at"] < value["started_at"]:
                    result[value["label"]] = dict(value)
            return result


_service = SkillService()


def get_skill_service():
    return _service


def _skill_label(label):
    return isinstance(label, str) and label.startswith("skill:")


def merged_status(legacy):
    result = {key: value for key, value in legacy.items() if not _skill_label(key) and key != "_runs"}
    runs = {key: value for key, value in legacy.get("_runs", {}).items() if not _skill_label(key)}
    direct = get_skill_service().status()
    result.update({key: value for key, value in direct.items() if key != "_runs"})
    result["_runs"] = {**runs, **direct["_runs"]}
    return result


def merged_completions(legacy):
    return {**{key: value for key, value in legacy.items() if not _skill_label(key)},
            **get_skill_service().completions()}
