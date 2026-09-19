"""Supervised, read-only Work IQ MCP stdio boundary."""

from __future__ import annotations

import json
import logging
import re
import unicodedata
import os
import atexit
import queue
import subprocess
import threading
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Callable
from urllib.parse import unquote, urlsplit

from .workiq_policy import (
    ACTION_TOOL,
    ASK_TOOL,
    FETCH_TOOL,
    SENT_ITEMS_URL,
    CalendarAction,
    CalendarOperation,
    AskOperation,
    SourceReadOperation,
    CapabilityError,
    build_calendar_operation,
    build_ask_operation,
    discover_read_capabilities,
    require_calendar_operation,
    require_ask_operation,
    build_source_operation,
    require_source_operation,
    _source_identifier,
    _source_initial_path,
    _source_chat_path,
    _source_email_conversation_path,
)
from .workiq_setup import (
    WorkIQAccountError,
    WorkIQAccountRequiredError,
    get_setup,
    redact,
)


logger = logging.getLogger(__name__)
PROTOCOL_VERSION = "2025-06-18"
ASK_AUTH_COOLDOWN_SECONDS = 60
SOURCE_AUTH_COOLDOWN_SECONDS = 60
SOURCE_EXCERPT_LIMIT = 512
SOURCE_URL_LIMIT = 2048
ASK_PROBE_QUESTION = (
    "Reply exactly RIVETER_PROTOCOL_PROBE. Do not search or access Microsoft 365 data."
)


class WorkIQError(Exception):
    code = "internal"

    def public(self) -> dict:
        return {"code": self.code, "message": redact(self)}


class DependencyError(WorkIQError):
    code = "dependency_missing"


class VersionMismatchError(WorkIQError):
    code = "version_mismatch"


class TransportError(WorkIQError):
    code = "transport"


class ProtocolError(WorkIQError):
    code = "protocol"


class RemoteError(WorkIQError):
    code = "remote"


class SetupUnavailableError(WorkIQError):
    code = "mcp_unavailable"


class InvalidResponseError(WorkIQError):
    code = "invalid_response"


class CapabilityDeniedError(WorkIQError):
    code = "capability_denied"


class AuthRequiredError(WorkIQError):
    code = "auth_required"


class ConsentRequiredError(WorkIQError):
    code = "consent_required"


class EulaRequiredError(WorkIQError):
    code = "eula_required"


class NotReadyError(WorkIQError):
    code = "not_ready"


class ToolError(WorkIQError):
    code = "tool"


class ActionHTTPError(WorkIQError):
    code = "action_http"


class InvalidStructuredContentError(WorkIQError):
    code = "invalid_structured_content"


class SourceUnreadableError(WorkIQError):
    code = "source_unreadable"


class SourcePartialError(SourceUnreadableError):
    code = "source_partial"


class SourceForbiddenError(SourceUnreadableError):
    code = "source_forbidden"


class SourceNotFoundError(SourceUnreadableError):
    code = "source_not_found"


class SourceHTTPError(WorkIQError):
    code = "source_http"


class _SourceText(HTMLParser):
    """Extract inert, bounded evidence text; never retain raw HTML in a result."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.ignored = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.ignored += 1
        elif tag in {"p", "div", "br", "li"} and not self.ignored:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.ignored = max(0, self.ignored - 1)
        elif tag in {"p", "div", "li"} and not self.ignored:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.ignored:
            self.parts.append(data)


class QueueFullError(WorkIQError):
    code = "queue_full"


class TimeoutError(WorkIQError):
    code = "timeout"


class CancelledError(WorkIQError):
    code = "cancelled"


class RuntimeStoppingError(WorkIQError):
    code = "cancelled"


@dataclass
class _Operation:
    job_id: str
    timeout: float
    plan: str = "readiness"
    calendar: CalendarOperation | None = None
    ask: AskOperation | None = None
    source: SourceReadOperation | None = None
    # Shared absolute admission deadline for ask and source reads.
    ask_deadline: float | None = None
    state: str = "queued"
    result: dict | None = None
    event: threading.Event = field(default_factory=threading.Event)
    cancel_requested: threading.Event = field(default_factory=threading.Event)

    def public(self, *, include_data: bool = True) -> dict:
        result = dict(self.result or {})
        if not include_data:
            result.pop("data", None)
        return {
            "job_id": self.job_id,
            "state": self.state,
            **result,
        }


class WorkIQRuntime:
    """Own one Work IQ child and serialize policy-approved MCP read plans."""

    def __init__(
        self,
        *,
        command: Callable[[], list[str]] | None = None,
        process_factory: Callable = subprocess.Popen,
        max_queue: int = 16,
        max_incoming_messages: int = 32,
        max_payload_bytes: int = 256 * 1024,
        stderr_limit_bytes: int = 32 * 1024,
        history_limit: int = 20,
        startup_timeout: float = 240,
        terminate_grace: float = 5,
        required_server_version: str = "1.0.0",
        clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
    ):
        self._command = command or (lambda: get_setup().runtime_command())
        self._process_factory = process_factory
        self._max_incoming_messages = max_incoming_messages
        self._max_payload_bytes = max_payload_bytes
        self._stderr_limit_bytes = stderr_limit_bytes
        self._history_limit = history_limit
        self._startup_timeout = startup_timeout
        self._terminate_grace = terminate_grace
        self._required_server_version = required_server_version
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic_clock = monotonic_clock or time.monotonic

        self._lock = threading.RLock()
        self._start_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._operations: OrderedDict[str, _Operation] = OrderedDict()
        self._commands: queue.Queue[_Operation | None] = queue.Queue(maxsize=max_queue)
        self._incoming: queue.Queue[bytes | BaseException | None] = queue.Queue(
            maxsize=max_incoming_messages
        )
        self._diagnostics: deque[str] = deque()
        self._diagnostic_size = 0
        self._thread: threading.Thread | None = None
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._process: subprocess.Popen | None = None
        self._ready_event = threading.Event()
        self._stopping = threading.Event()
        self._state = "stopped"
        self._error: dict | None = None
        self._allowed: tuple[str, ...] = ()
        self._protocol_version: str | None = None
        self._server_info: dict | None = None
        self._authenticated = False
        self._ask_available = False
        self._ask_ready = False
        self._ask_blocker: dict | None = None
        self._ask_retry_after = 0.0
        self._source_ready = False
        self._source_blocker: dict | None = None
        self._source_retry_after = 0.0
        self._request_id = 0
        self._active_operation: _Operation | None = None
        self._active_request_id: int | None = None

    def start(self) -> dict:
        with self._start_lock:
            return self._start_serialized()

    def _start_serialized(self, *, ask_deadline: float | None = None) -> dict:
        startup_budget = self._startup_timeout
        if ask_deadline is not None:
            startup_budget = min(startup_budget, self._ask_time_left(ask_deadline))
        startup_deadline = time.monotonic() + startup_budget
        with self._lock:
            if self._thread and self._thread.is_alive():
                if self._state != "faulted":
                    return self.snapshot()
            stale = self._thread
        if stale and stale.is_alive():
            self.shutdown()
            if stale.is_alive():
                with self._lock:
                    if not self._error:
                        error = TransportError(
                            "The previous Work IQ runtime is still stopping."
                        )
                        self._error = error.public()
                    self._state = "faulted"
                return self.snapshot()

        with self._lock:
            self._stopping.clear()
            self._ready_event.clear()
            self._incoming = queue.Queue(maxsize=self._max_incoming_messages)
            self._commands = queue.Queue(maxsize=self._commands.maxsize)
            self._state = "starting"
            self._error = None
            self._allowed = ()
            self._authenticated = False
            self._ask_available = False
            self._ask_ready = False
            self._source_ready = False
            self._thread = threading.Thread(
                target=self._worker_main,
                args=(startup_deadline,),
                daemon=True,
                name="workiq-mcp-worker",
            )
            self._thread.start()
            owner = self._thread
        remaining = max(0.0, startup_deadline - time.monotonic())
        if ask_deadline is not None:
            remaining = min(remaining, max(0.0, ask_deadline - self._monotonic_clock()))
        ready = self._ready_event.wait(remaining)
        if ask_deadline is not None and (
            not ready or self._monotonic_clock() >= ask_deadline
        ):
            self._abort_ask_startup(owner)
            raise TimeoutError("Work IQ ask timed out during startup.")
        if not ready:
            timeout_error = TimeoutError("Work IQ MCP startup timed out.")
            with self._lock:
                self._state = "faulted"
                self._error = timeout_error.public()
                self._authenticated = False
                self._stopping.set()
            self._terminate_child()
            thread = self._thread
            if thread and thread is not threading.current_thread():
                thread.join(self._terminate_grace + 1)
        return self.snapshot()

    def _start_for_ask(self, deadline: float) -> None:
        if not self._start_lock.acquire(timeout=self._ask_time_left(deadline)):
            raise TimeoutError("Work IQ ask timed out waiting for startup.")
        try:
            self._ask_time_left(deadline)
            with self._lock:
                existing = self._thread
                running = existing and existing.is_alive()
            if running:
                # A waiter never owns abort, including a legacy caller's startup.
                if not self._ready_event.wait(self._ask_time_left(deadline)):
                    raise TimeoutError("Work IQ ask timed out waiting for startup.")
                self._ask_time_left(deadline)
                with self._lock:
                    if (
                        self._thread is existing
                        and existing.is_alive()
                        and self._process is not None
                        and not self._stopping.is_set()
                    ):
                        return
                    self._raise_public_error(
                        self._error or TransportError("Work IQ MCP is not running.").public()
                    )
            snapshot = self._start_serialized(ask_deadline=deadline)
            if snapshot["state"] not in {"ready", "busy"}:
                self._raise_public_error(snapshot.get("error"))
            self._ask_time_left(deadline)
        finally:
            self._start_lock.release()

    def _abort_ask_startup(self, owner: threading.Thread) -> None:
        with self._lock:
            if self._thread is not owner:
                return
            error = TimeoutError("Work IQ ask timed out during startup.")
            self._state = "faulted"
            self._error = error.public()
            self._authenticated = False
            self._ask_ready = False
            self._source_ready = False
            self._stopping.set()
            self._ready_event.set()
            process = self._process
            incoming = self._incoming
            reader = self._reader_thread
            stderr = self._stderr_thread
        if process is not None:
            self._terminate_child(
                process=process, incoming=incoming,
                reader_thread=reader, stderr_thread=stderr,
            )
        if owner is not threading.current_thread():
            owner.join(self._terminate_grace)

    def submit(
        self,
        plan: str = "readiness",
        timeout: float = 45,
        *,
        calendar: CalendarOperation | None = None,
        ask: AskOperation | None = None,
        source: SourceReadOperation | None = None,
    ) -> str:
        ask_deadline = (
            self._monotonic_clock() + timeout if plan in {"ask", "ask_probe", "source"} else None
        )
        if plan not in {"readiness", "calendar", "sent_items", "ask_probe", "ask", "source"}:
            raise CapabilityError("Riveter has no approved plan for this read.")
        if plan == "calendar":
            calendar = require_calendar_operation(calendar)
        elif calendar is not None:
            raise CapabilityError("Readiness cannot carry a calendar operation.")
        if plan == "ask":
            ask = require_ask_operation(ask)
        elif ask is not None:
            raise CapabilityError("Only an ask plan can carry an ask operation.")
        if plan == "source":
            source = require_source_operation(source)
        elif source is not None:
            raise CapabilityError("Only a source plan can carry a source operation.")
        return self._enqueue_operation(_Operation(
            job_id=str(uuid.uuid4()),
            timeout=timeout,
            plan=plan,
            calendar=calendar,
            ask=ask,
            source=source,
            ask_deadline=ask_deadline,
        ))

    def _enqueue_operation(self, operation: _Operation) -> str:
        with self._lock:
            if self._stopping.is_set():
                raise RuntimeStoppingError("Work IQ MCP is stopping.")
            if not self._thread or not self._thread.is_alive():
                raise TransportError("Work IQ MCP is not running.")
            self._operations[operation.job_id] = operation
            if (
                operation.ask_deadline is not None
                and self._monotonic_clock() >= operation.ask_deadline
            ):
                self._finish(operation, "timed_out", TimeoutError("Work IQ ask timed out in queue."))
                return operation.job_id
        try:
            self._commands.put_nowait(operation)
        except queue.Full as exc:
            with self._lock:
                self._operations.pop(operation.job_id, None)
                operation.ask = None
                operation.source = None
            raise QueueFullError("Work IQ read queue is full.") from exc
        return operation.job_id

    def wait(self, job_id: str, timeout: float | None = None) -> dict:
        return self._wait_operation(job_id, timeout)

    def _wait_operation(
        self, job_id: str, timeout: float | None = None, *, source_data: bool = False,
    ) -> dict:
        with self._lock:
            operation = self._operations.get(job_id)
        if operation is None:
            return {
                "job_id": job_id,
                "state": "failed",
                "ok": False,
                "error": {"code": "not_found", "message": "Unknown Work IQ job."},
            }
        if operation.ask_deadline is None:
            operation.event.wait(timeout)
        else:
            remaining = max(0.0, operation.ask_deadline - self._monotonic_clock())
            deadline_wait = timeout is None or remaining <= timeout
            duration = remaining if timeout is None else min(remaining, max(0.0, timeout))
            completed = operation.event.wait(duration)
            if not completed and (
                deadline_wait or self._monotonic_clock() >= operation.ask_deadline
            ):
                self._expire_ask(operation)
        with self._lock:
            result = operation.public(include_data=operation.plan != "source" or source_data)
            if operation.plan in {"ask", "ask_probe", "source"} and operation.result:
                operation.result.pop("data", None)
            return result

    def _expire_ask(self, operation: _Operation) -> None:
        with self._lock:
            if operation.event.is_set():
                return
            error = TimeoutError("Work IQ ask timed out.")
            if operation.state == "queued":
                self._finish(operation, "timed_out", error)
                return
            operation.cancel_requested.set()
            process = self._process
            incoming = self._incoming
            reader = self._reader_thread
            stderr = self._stderr_thread
            if self._active_operation is operation and self._active_request_id is not None:
                self._send_notification(
                    "notifications/cancelled",
                    {"requestId": self._active_request_id, "reason": "Riveter deadline exceeded"},
                )
            self._finish(
                operation, "timed_out", error,
                runtime_state="faulted", runtime_error=error, authenticated=False,
            )
        if process is not None:
            self._terminate_child(
                allow_grace=True, process=process, incoming=incoming,
                reader_thread=reader, stderr_thread=stderr,
            )

    def probe_ask(self, timeout: float = 45) -> dict:
        """Prove ask readiness without changing legacy calendar readiness."""
        try:
            self._execute_ask_request(None, self._monotonic_clock() + timeout)
            return {"ok": True}
        except WorkIQError as exc:
            return {"ok": False, "error": exc.public()}

    def execute_ask(self, question: str, *, timeout: float) -> dict:
        """Return only transient answer/correlation from a policy-minted ask."""
        deadline = self._monotonic_clock() + timeout
        return self._execute_ask_request(build_ask_operation(question), deadline)

    def read_source(self, locator: dict, *, timeout: float) -> dict:
        """Read one exact resolved task source; only complete pages yield evidence.

        Source auth is proven by this actual fetch, independently of calendar/ask.
        Locators must come from source_locator.resolve on a server task snapshot.
        """
        deadline = self._monotonic_clock() + timeout
        source = build_source_operation(locator)
        with self._lock:
            self._check_source_cooldown()
        # Reuse Stage 1's owner-aware admission/start-lock deadline path.
        self._start_for_ask(deadline)
        job_id = self._enqueue_operation(_Operation(
            job_id=str(uuid.uuid4()), timeout=self._ask_time_left(deadline),
            plan="source", source=source, ask_deadline=deadline,
        ))
        try:
            result = self._wait_operation(job_id, source_data=True)
            if result.get("state") not in {"succeeded", "failed", "timed_out", "cancelled"}:
                self.cancel(job_id)
                raise TimeoutError("Work IQ source read timed out.")
            if not result.get("ok"):
                self._raise_public_error(result.get("error"))
            self._ask_time_left(deadline)
            data = result.get("data")
            if not isinstance(data, dict):
                raise SourceUnreadableError("Work IQ source result was unavailable.")
            return data
        finally:
            with self._lock:
                stored = self._operations.get(job_id)
                if stored:
                    stored.source = None
                    if stored.result:
                        stored.result.pop("data", None)

    def _execute_ask_request(
        self, ask: AskOperation | None, deadline: float
    ) -> dict:
        with self._lock:
            self._check_ask_cooldown()
        self._start_for_ask(deadline)
        job_id = self._enqueue_operation(_Operation(
            job_id=str(uuid.uuid4()),
            timeout=self._ask_time_left(deadline),
            plan="ask" if ask is not None else "ask_probe",
            ask=ask,
            ask_deadline=deadline,
        ))
        result = self.wait(job_id, timeout=None)
        try:
            if result.get("state") not in {"succeeded", "failed", "timed_out", "cancelled"}:
                self.cancel(job_id)
                raise TimeoutError("Work IQ ask timed out.")
            if not result.get("ok"):
                self._raise_public_error(result.get("error"))
            if ask is None:
                return {}
            data = result.get("data")
            if not isinstance(data, dict):
                raise InvalidStructuredContentError("Work IQ ask returned invalid content.")
            return dict(data)
        finally:
            # Completion can race the caller's timeout before cancel observes it.
            with self._lock:
                stored = self._operations.get(job_id)
                if stored:
                    stored.ask = None
                    if stored.result:
                        stored.result.pop("data", None)

    def probe(self, timeout: float = 45) -> dict:
        snapshot = self.start()
        if snapshot["state"] not in {
            "ready",
            "auth_required",
            "consent_required",
            "eula_required",
        }:
            return {
                "ok": False,
                "error": snapshot.get("error")
                or {"code": "mcp_unavailable", "message": "Work IQ MCP is unavailable."},
            }
        try:
            job_id = self.submit("readiness", timeout=timeout)
        except WorkIQError as exc:
            return {"ok": False, "error": exc.public()}
        result = self.wait(job_id, timeout + self._terminate_grace + 1)
        if result.get("state") not in {"succeeded", "failed", "timed_out", "cancelled"}:
            self.cancel(job_id)
            return {
                "ok": False,
                "error": TimeoutError("Work IQ readiness timed out.").public(),
            }
        return {
            "ok": result.get("ok", False),
            **({"error": result["error"]} if result.get("error") else {}),
        }

    def execute_calendar(
        self,
        operation: CalendarOperation,
        *,
        timeout: float,
    ) -> dict:
        """Run one policy-minted calendar read after explicit authenticated readiness."""
        operation = require_calendar_operation(operation)
        with self._lock:
            if (
                not self._authenticated
                or self._state not in {"ready", "busy"}
                or ACTION_TOOL not in self._allowed
            ):
                raise NotReadyError("Work IQ authenticated readiness is required.")
        job_id = self.submit("calendar", timeout=timeout, calendar=operation)
        result = self.wait(job_id, timeout + self._terminate_grace + 1)
        try:
            if result.get("state") not in {
                "succeeded", "failed", "timed_out", "cancelled"
            }:
                self.cancel(job_id)
                raise TimeoutError("Work IQ calendar action timed out.")
            if not result.get("ok"):
                self._raise_public_error(result.get("error"))
            data = result.get("data")
            if not isinstance(data, dict):
                raise InvalidStructuredContentError(
                    "Work IQ calendar action returned invalid structured content."
                )
            return dict(data)
        finally:
            with self._lock:
                stored = self._operations.get(job_id)
                if stored:
                    stored.calendar = None
                    if stored.result:
                        stored.result.pop("data", None)

    def read_sent_items(self, *, timeout: float) -> dict:
        """Read one sealed Sent Items page after explicit authenticated readiness.

        The typed MCP result is
        ``structuredContent.results[0] = {statusCode: 200,
        data: {value: [...], optional @odata.nextLink}}``. The raw page is
        returned only to the synchronous caller and is removed from operation
        history before this method returns or raises.
        """
        with self._lock:
            if (
                not self._authenticated
                or self._state not in {"ready", "busy"}
            ):
                raise NotReadyError("Work IQ authenticated readiness is required.")
            if FETCH_TOOL not in self._allowed:
                raise CapabilityDeniedError(
                    "Work IQ does not advertise the required email read capability."
                )
        job_id = self.submit("sent_items", timeout=timeout)
        result = self.wait(job_id, timeout + self._terminate_grace + 1)
        try:
            if result.get("state") not in {
                "succeeded", "failed", "timed_out", "cancelled"
            }:
                self.cancel(job_id)
                raise TimeoutError("Work IQ Sent Items read timed out.")
            if not result.get("ok"):
                self._raise_public_error(result.get("error"))
            data = result.get("data")
            if not isinstance(data, dict):
                raise InvalidStructuredContentError(
                    "Work IQ Sent Items read returned invalid structured content."
                )
            return dict(data)
        finally:
            with self._lock:
                stored = self._operations.get(job_id)
                if stored and stored.result:
                    stored.result.pop("data", None)

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            operation = self._operations.get(job_id)
            if operation is None or operation.event.is_set():
                return False
            operation.cancel_requested.set()
            if operation.state == "queued":
                self._finish(
                    operation,
                    "cancelled",
                    CancelledError("Work IQ read was cancelled."),
                    **(
                        {
                            "runtime_state": "faulted",
                            "runtime_error": CancelledError(
                                "Work IQ readiness was cancelled."
                            ),
                            "authenticated": False,
                        }
                        if operation.plan == "readiness"
                        else {}
                    ),
                )
                return True
            cancellation = CancelledError("Work IQ read was cancelled.")
            self._finish(
                operation,
                "cancelled",
                cancellation,
                runtime_state="faulted",
                runtime_error=cancellation,
                authenticated=False,
            )
        self._terminate_child()
        return True

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "state": self._state,
                "error": dict(self._error) if self._error else None,
                "protocol_version": self._protocol_version,
                "server": dict(self._server_info) if self._server_info else None,
                "allowed_capabilities": list(self._allowed),
                "authenticated": self._authenticated,
                "active_job_id": (
                    self._active_operation.job_id if self._active_operation else None
                ),
                "queue_depth": self._commands.qsize(),
                "diagnostics": list(self._diagnostics),
                "history": [
                    operation.public(include_data=False)
                    for operation in list(self._operations.values())
                    if operation.event.is_set()
                ][-self._history_limit :],
            }

    def shutdown(self) -> None:
        with self._lock:
            thread = self._thread
            if not thread and self._state == "stopped":
                return
            self._stopping.set()
            self._state = "stopping"
            self._ready_event.set()
            self._authenticated = False
            self._ask_ready = False
            self._source_ready = False
            process = self._process
            commands = self._commands
            incoming = self._incoming
            reader_thread = self._reader_thread
            stderr_thread = self._stderr_thread
            active = self._active_operation
            owned_operations = list(self._operations.values())
            queued = [
                operation
                for operation in owned_operations
                if not operation.event.is_set() and operation is not active
            ]
        for operation in queued:
            self._finish(operation, "cancelled", CancelledError("Work IQ runtime shut down."))
        if active and not active.event.is_set():
            active.cancel_requested.set()
            self._finish(
                active,
                "cancelled",
                CancelledError("Work IQ runtime shut down."),
            )
        try:
            commands.put_nowait(None)
        except queue.Full:
            pass
        if process is not None:
            self._terminate_child(
                process=process,
                incoming=incoming,
                reader_thread=reader_thread,
                stderr_thread=stderr_thread,
            )
        if thread and thread is not threading.current_thread():
            thread.join(self._terminate_grace + 1)
        with self._lock:
            for operation in owned_operations:
                operation.calendar = None
                operation.ask = None
                operation.source = None
                if operation.result:
                    operation.result.pop("data", None)
            thread_stopped = not thread or not thread.is_alive()
            if self._thread is thread:
                if thread_stopped:
                    self._thread = None
                    self._state = "stopped"
                else:
                    self._state = "faulted"
                if self._active_operation is active:
                    self._active_operation = None
                    self._active_request_id = None

    def invalidate_setup_state(self) -> None:
        """Clear cached blockers after a successful out-of-band setup action."""
        self.shutdown()
        with self._lock:
            self._error = None
            self._protocol_version = None
            self._server_info = None
            self._allowed = ()
            self._authenticated = False
            self._ask_available = False
            self._ask_ready = False
            self._ask_blocker = None
            self._ask_retry_after = 0.0
            self._source_ready = False
            self._source_blocker = None
            self._source_retry_after = 0.0

    def _worker_main(self, startup_deadline: float) -> None:
        try:
            self._launch_child()
            self._handshake(startup_deadline)
            with self._lock:
                self._state = "ready"
                self._error = None
                self._ready_event.set()

            while not self._stopping.is_set():
                try:
                    operation = self._commands.get(timeout=0.1)
                except queue.Empty:
                    self._service_idle_messages()
                    continue
                if operation is None:
                    break
                with self._lock:
                    if operation.event.is_set() or operation.cancel_requested.is_set():
                        continue
                    if (
                        operation.ask_deadline is not None
                        and self._monotonic_clock() >= operation.ask_deadline
                    ):
                        self._finish(
                            operation, "timed_out",
                            TimeoutError("Work IQ ask timed out in queue."),
                        )
                        continue
                    legacy_state = (self._state, self._error)
                    self._active_operation = operation
                    operation.state = "running"
                    self._state = "busy"
                fatal = self._run_operation(operation, legacy_state=legacy_state)
                with self._lock:
                    self._active_operation = None
                    self._active_request_id = None
                if fatal:
                    break
        except WorkIQError as exc:
            with self._lock:
                if (self._error or {}).get("code") != "timeout":
                    self._state = "faulted"
                    self._error = exc.public()
                self._authenticated = False
                self._ask_ready = False
                self._source_ready = False
                self._ready_event.set()
        except Exception as exc:
            logger.error(
                "Unexpected Work IQ runtime failure (%s).",
                type(exc).__name__,
            )
            with self._lock:
                if (self._error or {}).get("code") != "timeout":
                    error = WorkIQError("Unexpected Work IQ runtime failure.")
                    self._state = "faulted"
                    self._error = error.public()
                self._authenticated = False
                self._ask_ready = False
                self._source_ready = False
                self._ready_event.set()
        finally:
            with self._lock:
                stopping = self._stopping.is_set()
                outstanding = [
                    operation
                    for operation in self._operations.values()
                    if not operation.event.is_set()
                ]
            exit_error = (
                CancelledError("Work IQ runtime shut down.")
                if stopping
                else TransportError("Work IQ runtime stopped before the request ran.")
            )
            for operation in outstanding:
                self._finish(
                    operation,
                    "cancelled" if stopping else "failed",
                    exit_error,
                )
            self._terminate_child()
            with self._lock:
                self._ready_event.set()
                if self._stopping.is_set() and (self._error or {}).get("code") != "timeout":
                    self._state = "stopped"
                if self._thread is threading.current_thread():
                    self._thread = None

    def _launch_child(self) -> None:
        try:
            argv = self._command()
        except RuntimeError as exc:
            code = str(exc)
            if code == "version_mismatch":
                raise VersionMismatchError("The pinned Work IQ version is not installed.") from exc
            raise DependencyError("The pinned Work IQ runtime is not installed.") from exc
        if not isinstance(argv, list) or not argv or not all(isinstance(v, str) for v in argv):
            raise DependencyError("Work IQ runtime command is invalid.")
        kwargs = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "shell": False,
            "bufsize": 0,
        }
        if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            process = self._process_factory(argv, **kwargs)
        except OSError as exc:
            raise DependencyError("Could not start the pinned Work IQ runtime.") from exc
        with self._lock:
            self._process = process
            stopping = self._stopping.is_set()
        if stopping:
            self._terminate_child()
            raise RuntimeStoppingError("Work IQ MCP startup was cancelled.")
        reader_thread = threading.Thread(
            target=self._read_stdout,
            daemon=True,
            name="workiq-mcp-stdout",
        )
        stderr_thread = threading.Thread(
            target=self._read_stderr,
            daemon=True,
            name="workiq-mcp-stderr",
        )
        reader_thread.start()
        stderr_thread.start()
        with self._lock:
            stopping = self._stopping.is_set()
            if not stopping and self._process is process:
                self._reader_thread = reader_thread
                self._stderr_thread = stderr_thread
        if stopping:
            self._terminate_child(
                process=process,
                incoming=self._incoming,
                reader_thread=reader_thread,
                stderr_thread=stderr_thread,
            )
            raise RuntimeStoppingError("Work IQ MCP startup was cancelled.")

    def _startup_remaining(self, deadline: float) -> float:
        with self._lock:
            startup_timed_out = (
                self._stopping.is_set()
                and (self._error or {}).get("code") == "timeout"
            )
        if startup_timed_out:
            raise TimeoutError("Work IQ MCP startup timed out.")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Work IQ MCP startup timed out.")
        return remaining

    def _handshake(self, deadline: float) -> None:
        initialize_budget = self._startup_remaining(deadline)
        with self._lock:
            if (
                self._stopping.is_set()
                and (self._error or {}).get("code") == "timeout"
            ):
                raise TimeoutError("Work IQ MCP startup timed out.")
            self._state = "initializing"
        initialized = self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "Riveter", "version": "1"},
            },
            initialize_budget,
        )
        protocol = initialized.get("protocolVersion")
        capabilities = initialized.get("capabilities")
        server = initialized.get("serverInfo")
        if not isinstance(protocol, str) or not isinstance(server, dict):
            raise ProtocolError("Work IQ initialize result was malformed.")
        if protocol != PROTOCOL_VERSION:
            raise ProtocolError("Work IQ negotiated an unsupported MCP protocol version.")
        if (
            not isinstance(capabilities, dict)
            or not isinstance(capabilities.get("tools"), dict)
        ):
            raise ProtocolError("Work IQ MCP server did not advertise tool support.")
        if (
            server.get("name") != "WorkIQ"
            or server.get("version") != self._required_server_version
        ):
            raise VersionMismatchError("Work IQ MCP server version does not match Riveter.")
        with self._lock:
            self._protocol_version = protocol
            self._server_info = {
                "name": server["name"],
                "version": server["version"],
            }
        self._send_notification("notifications/initialized", {})

        with self._lock:
            self._state = "discovering"
        capability_error = None
        for attempt in (1, 2):
            listed = self._rpc(
                "tools/list",
                {},
                self._startup_remaining(deadline),
            )
            try:
                allowed = discover_read_capabilities(listed.get("tools"))
                break
            except CapabilityError as exc:
                capability_error = exc
                if attempt == 2:
                    raise CapabilityDeniedError(str(exc)) from exc
        if capability_error is not None:
            logger.info("Work IQ capabilities became ready after one bounded retry.")
        with self._lock:
            self._allowed = tuple(name for name in allowed if name != ASK_TOOL)
            self._ask_available = ASK_TOOL in allowed

    def _run_operation(
        self, operation: _Operation, *, legacy_state: tuple | None = None
    ) -> bool:
        try:
            if operation.plan == "readiness":
                self._run_readiness(operation)
            elif operation.plan == "calendar":
                self._run_calendar(operation)
            elif operation.plan == "sent_items":
                self._run_sent_items(operation)
            elif operation.plan in {"ask_probe", "ask"}:
                self._run_ask_operation(
                    operation, legacy_state or (self._state, self._error)
                )
            elif operation.plan == "source":
                self._run_source_operation(
                    operation, legacy_state or (self._state, self._error)
                )
            else:
                raise CapabilityDeniedError("Riveter has no approved plan for this read.")
            return False
        except CancelledError as exc:
            self._finish(
                operation,
                "cancelled",
                exc,
                runtime_state="faulted",
                runtime_error=exc,
                authenticated=False,
            )
            self._terminate_child()
            return True
        except TimeoutError as exc:
            self._finish(
                operation,
                "timed_out",
                exc,
                runtime_state="faulted",
                runtime_error=exc,
                authenticated=False,
            )
            self._terminate_child(allow_grace=True)
            return True
        except (ProtocolError, TransportError, InvalidResponseError) as exc:
            self._finish(
                operation,
                "failed",
                exc,
                runtime_state="faulted",
                runtime_error=exc,
                authenticated=False,
            )
            self._terminate_child()
            return True
        except (AuthRequiredError, ConsentRequiredError, EulaRequiredError) as exc:
            self._finish(
                operation,
                "failed",
                exc,
                runtime_state=exc.code,
                runtime_error=exc,
                authenticated=False,
            )
            return False
        except (ToolError, ActionHTTPError, InvalidStructuredContentError) as exc:
            if operation.plan == "readiness":
                self._finish(
                    operation,
                    "failed",
                    exc,
                    runtime_state="faulted",
                    runtime_error=exc,
                    authenticated=False,
                )
            else:
                # The MCP exchange completed and the child remains authenticated.
                self._finish(
                    operation,
                    "failed",
                    exc,
                    runtime_state="ready",
                    runtime_error=None,
                )
            return False
        except NotReadyError as exc:
            with self._lock:
                blocker = (self._error or {}).get("code")
                if blocker in {
                    "auth_required",
                    "consent_required",
                    "eula_required",
                }:
                    self._state = blocker
                else:
                    self._state = "faulted"
                    self._error = exc.public()
                self._finish(operation, "failed", exc)
            return False
        except WorkIQError as exc:
            self._finish(
                operation,
                "failed",
                exc,
                runtime_state="faulted",
                runtime_error=exc,
                authenticated=False if operation.plan == "readiness" else None,
            )
            return False
        except Exception as exc:
            error = InvalidResponseError("Work IQ returned an invalid readiness response.")
            self._finish(
                operation,
                "failed",
                error,
                runtime_state="faulted",
                runtime_error=error,
                authenticated=False,
            )
            self._append_diagnostic(
                f"Readiness response validation failed ({type(exc).__name__})."
            )
            self._terminate_child()
            return True

    def _run_ask_operation(self, operation: _Operation, legacy_state: tuple) -> None:
        data = None
        error = None
        try:
            data = self._run_ask(operation)
        except (TimeoutError, CancelledError, ProtocolError, TransportError, InvalidResponseError):
            raise
        except WorkIQError as exc:
            error = exc
            with self._lock:
                self._ask_ready = False
                if isinstance(exc, (AuthRequiredError, ConsentRequiredError, EulaRequiredError)):
                    now = self._monotonic_clock()
                    if self._ask_blocker is None or now >= self._ask_retry_after:
                        self._ask_blocker = exc.public()
                        self._ask_retry_after = now + ASK_AUTH_COOLDOWN_SECONDS
        with self._lock:
            if operation.cancel_requested.is_set() or operation.event.is_set() or self._stopping.is_set():
                raise CancelledError("Work IQ ask was cancelled.")
            # Nonfatal ask outcomes must not erase the pre-busy calendar state.
            self._state, self._error = legacy_state
            self._finish(
                operation,
                "failed" if error else "succeeded",
                error,
                result_data=data,
            )

    def _check_ask_cooldown(self) -> None:
        if self._ask_blocker and self._monotonic_clock() < self._ask_retry_after:
            self._raise_public_error(self._ask_blocker)

    def _ask_remaining(self, operation: _Operation, deadline: float) -> float:
        if operation.cancel_requested.is_set() or operation.event.is_set() or self._stopping.is_set():
            raise CancelledError("Work IQ ask was cancelled.")
        return self._ask_time_left(deadline)

    def _ask_time_left(self, deadline: float) -> float:
        remaining = deadline - self._monotonic_clock()
        if remaining <= 0:
            raise TimeoutError("Work IQ ask timed out.")
        return remaining

    def _run_ask(self, operation: _Operation) -> dict | None:
        deadline = operation.ask_deadline
        if deadline is None:
            raise CapabilityDeniedError("Work IQ ask requires an admission deadline.")
        with self._lock:
            self._check_ask_cooldown()
            if not self._ask_available:
                raise CapabilityDeniedError("Work IQ does not advertise ask.")
            needs_probe = not self._ask_ready or operation.plan == "ask_probe"
        if needs_probe:
            probe = build_ask_operation(ASK_PROBE_QUESTION)
            self._validate_ask_result(self._rpc(
                "tools/call",
                {"name": ASK_TOOL, "arguments": {"question": probe.question}},
                self._ask_remaining(operation, deadline),
                ask_operation=operation,
            ))
            with self._lock:
                self._ask_remaining(operation, deadline)
                self._ask_ready = True
                self._ask_blocker = None
                self._ask_retry_after = 0.0
        if operation.plan == "ask_probe":
            return None
        with self._lock:
            self._ask_remaining(operation, deadline)
            ask = require_ask_operation(operation.ask)
        result = self._rpc(
            "tools/call",
            {"name": ASK_TOOL, "arguments": {"question": ask.question}},
            self._ask_remaining(operation, deadline),
            ask_operation=operation,
        )
        self._ask_remaining(operation, deadline)
        return self._validate_ask_result(result)

    @staticmethod
    def _ask_remote_error(code: object) -> WorkIQError:
        errors = {
            "auth_required": AuthRequiredError("Work IQ authentication is required."),
            "consent_required": ConsentRequiredError("Work IQ administrator consent is required."),
            "eula_required": EulaRequiredError("Work IQ requires EULA acceptance."),
        }
        if isinstance(code, str) and code in errors:
            return errors[code]
        return RemoteError("Work IQ rejected the ask request.")

    def _validate_ask_result(self, result: dict) -> dict:
        content = result.get("content")
        is_error = result.get("isError", False)
        meta = result.get("_meta", {})
        if not isinstance(content, list) or not isinstance(is_error, bool) or not isinstance(meta, dict):
            raise InvalidStructuredContentError("Work IQ ask returned invalid content.")
        if is_error:
            error = self._ask_remote_error(meta.get("code"))
            if not isinstance(error, RemoteError):
                raise error
            raise ToolError("Work IQ ask failed.")
        structured = result.get("structuredContent")
        if (
            len(content) != 1
            or not isinstance(content[0], dict)
            or content[0].get("type") != "text"
            or not isinstance(content[0].get("text"), str)
            or not content[0]["text"].strip()
            or not isinstance(structured, dict)
            or not isinstance(structured.get("conversationId"), str)
            or not structured["conversationId"].strip()
            or ("answer" in structured and structured["answer"] != content[0]["text"])
        ):
            raise InvalidStructuredContentError("Work IQ ask returned invalid content.")
        return {"answer": content[0]["text"], "conversation_id": structured["conversationId"]}

    def _check_source_cooldown(self) -> None:
        if self._source_blocker and self._monotonic_clock() < self._source_retry_after:
            self._raise_public_error(self._source_blocker)

    def _run_source_operation(self, operation: _Operation, legacy_state: tuple) -> None:
        data = None
        error = None
        try:
            data = self._run_source(operation)
        except (TimeoutError, CancelledError, ProtocolError, TransportError, InvalidResponseError):
            raise
        except WorkIQError as exc:
            error = exc
            with self._lock:
                self._source_ready = False
                if isinstance(exc, (AuthRequiredError, ConsentRequiredError, EulaRequiredError)):
                    now = self._monotonic_clock()
                    if self._source_blocker is None or now >= self._source_retry_after:
                        self._source_blocker = exc.public()
                        self._source_retry_after = now + SOURCE_AUTH_COOLDOWN_SECONDS
        with self._lock:
            self._ask_remaining(operation, operation.ask_deadline)
            self._state, self._error = legacy_state
            self._finish(
                operation, "failed" if error else "succeeded", error, result_data=data,
            )
            if error is None:
                self._source_ready = True
                self._source_blocker = None
                self._source_retry_after = 0.0

    def _source_fetch(self, operation: _Operation, path: str) -> dict:
        result = self._rpc(
            "tools/call", {"name": FETCH_TOOL, "arguments": {"entityUrls": [path]}},
            self._ask_remaining(operation, operation.ask_deadline),
            ask_operation=operation,
        )
        self._ask_remaining(operation, operation.ask_deadline)
        data = self._validate_source_envelope(result)
        row = result["structuredContent"]["results"][0]
        if "entityUrl" in row and row["entityUrl"] != path:
            raise SourceUnreadableError("Work IQ source response identity did not match.")
        return data

    def _run_source(self, operation: _Operation) -> dict:
        with self._lock:
            self._ask_remaining(operation, operation.ask_deadline)
            self._check_source_cooldown()
            if FETCH_TOOL not in self._allowed:
                raise CapabilityDeniedError("Work IQ does not advertise source reads.")
            source = require_source_operation(operation.source)
        ids = dict(source.identifiers)
        conversation = ids.get("conversation_id")
        data = self._source_fetch(operation, _source_initial_path(source))
        if source.kind == "email":
            self._validate_source_entity(data, ids["message_id"])
            conversation = self._source_id(data.get("conversationId"))
            self._validate_source_optional_fields(data, email=True)
            path = _source_email_conversation_path(conversation)
            data = self._source_fetch(operation, path)
        elif source.kind == "meeting" and not conversation:
            self._validate_source_entity(data, ids["event_id"])
            self._validate_source_event_fields(data)
            meeting = data.get("onlineMeeting")
            if not isinstance(meeting, dict):
                raise SourceUnreadableError("Work IQ event has no usable meeting chat.")
            conversation = self._source_meeting_thread(meeting.get("joinUrl"))
            data = self._source_fetch(operation, _source_chat_path(conversation))
        return self._project_source_page(source, conversation, data)

    def _validate_source_envelope(self, result: dict) -> dict:
        content = result.get("content")
        is_error = result.get("isError", False)
        meta = result.get("_meta", {})
        if (
            not isinstance(content, list) or not isinstance(is_error, bool)
            or not isinstance(meta, dict)
            or any(not isinstance(item, dict) or item.get("type") != "text"
                   or not isinstance(item.get("text"), str) for item in content)
        ):
            raise SourceUnreadableError("Work IQ source response was malformed.")
        if is_error:
            error = self._ask_remote_error(meta.get("code"))
            if not isinstance(error, RemoteError):
                raise error
            raise ToolError("Work IQ source read failed.")
        structured = result.get("structuredContent")
        rows = structured.get("results") if isinstance(structured, dict) else None
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise SourceUnreadableError("Work IQ source response was malformed.")
        status = rows[0].get("statusCode")
        if type(status) is not int:
            raise SourceUnreadableError("Work IQ source response was malformed.")
        if status == 401:
            raise AuthRequiredError("Work IQ authentication is required.")
        if status == 403:
            raise SourceForbiddenError("Work IQ access to this source was denied.")
        if status == 404:
            raise SourceNotFoundError("Work IQ could not find this source.")
        if status != 200:
            raise SourceHTTPError("Work IQ source read returned a non-success status.")
        data = rows[0].get("data")
        if not isinstance(data, dict):
            raise SourceUnreadableError("Work IQ source response was malformed.")
        if "headers" in rows[0]:
            headers = rows[0]["headers"]
            if not isinstance(headers, dict) or any(
                not isinstance(key, str) or len(key) > 256
                or not isinstance(value, str) or len(value) > SOURCE_URL_LIMIT
                or any(ord(char) < 32 or ord(char) == 127 for char in key + value)
                for key, value in headers.items()
            ):
                raise SourceUnreadableError("Work IQ source headers were malformed.")
            if any(key.lower() == "link" for key in headers):
                raise SourcePartialError("Work IQ source returned a partial page.")
        return data

    @staticmethod
    def _source_id(value: object) -> str:
        try:
            return _source_identifier(value)
        except CapabilityError:
            raise SourceUnreadableError("Work IQ source identifier was invalid.") from None

    @staticmethod
    def _source_timestamp(value: object) -> str:
        if (
            not isinstance(value, str) or len(value) > 64
            or re.fullmatch(
                r"\d{4}-\d\d-\d\dT[0-2]\d:[0-5]\d:[0-5]\d(?:\.\d{1,7})?(?:Z|[+-][0-2]\d:[0-5]\d)",
                value,
            ) is None
        ):
            raise SourceUnreadableError("Work IQ source timestamp was invalid.")
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return stamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        except (ValueError, OverflowError):
            raise SourceUnreadableError("Work IQ source timestamp was invalid.") from None

    @staticmethod
    def _source_text(value: object, *, limit: int, html: bool = False) -> str:
        if not isinstance(value, str) or len(value) > 65536:
            raise SourceUnreadableError("Work IQ source text was invalid.")
        if html:
            parser = _SourceText()
            parser.feed(value)
            parser.close()
            value = "".join(parser.parts)
        value = "".join(char for char in value if char.isspace() or not unicodedata.category(char).startswith("C"))
        return " ".join(value.split())[:limit]

    @staticmethod
    def _source_url(value: object, *, hosts: set[str]):
        if (
            not isinstance(value, str) or not value or len(value) > SOURCE_URL_LIMIT
            or value != value.strip()
            or any(char.isspace() or unicodedata.category(char).startswith("C") for char in value)
            or "\\" in value
        ):
            raise SourceUnreadableError("Work IQ source link was invalid.")
        try:
            parsed = urlsplit(value)
            if (
                parsed.scheme != "https" or parsed.hostname not in hosts
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in {None, 443}
            ):
                raise ValueError
        except ValueError:
            raise SourceUnreadableError("Work IQ source link was invalid.") from None
        return parsed

    def _source_meeting_thread(self, value: object) -> str:
        parsed = self._source_url(value, hosts={"teams.microsoft.com"})
        match = re.fullmatch(r"/l/meetup-join/([^/]+)/0", parsed.path)
        if not match or parsed.fragment:
            raise SourceUnreadableError("Work IQ event has no usable meeting chat.")
        thread = unquote(match.group(1))
        if re.fullmatch(r"19:meeting_[A-Za-z0-9_-]+@thread\.v2", thread) is None:
            raise SourceUnreadableError("Work IQ event has no usable meeting chat.")
        return self._source_id(thread)

    def _source_complete(self, data: dict) -> None:
        if "@odata.nextLink" in data:
            self._source_url(data["@odata.nextLink"], hosts={"graph.microsoft.com"})
            raise SourcePartialError("Work IQ source returned a partial page.")

    def _validate_source_entity(self, data: dict, expected_id: str) -> None:
        self._source_complete(data)
        if "value" in data or self._source_id(data.get("id")) != expected_id:
            raise SourceUnreadableError("Work IQ source bootstrap identity did not match.")
        if data.get("subject") is not None:
            self._source_text(data["subject"], limit=SOURCE_EXCERPT_LIMIT)

    def _validate_source_event_fields(self, data: dict) -> None:
        for key in ("start", "end"):
            if key not in data:
                continue
            value = data[key]
            if (
                not isinstance(value, dict)
                or not isinstance(value.get("dateTime"), str)
                or len(value["dateTime"]) > 64
                or not isinstance(value.get("timeZone"), str)
                or not value["timeZone"].strip() or len(value["timeZone"]) > 256
                or any(unicodedata.category(char).startswith("C") for char in value["timeZone"])
                or re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,7})?", value["dateTime"]) is None
            ):
                raise SourceUnreadableError("Work IQ source event time was invalid.")
            try:
                datetime.fromisoformat(value["dateTime"])
            except ValueError:
                raise SourceUnreadableError("Work IQ source event time was invalid.") from None
        if "organizer" in data:
            self._source_sender(data["organizer"], email=True)

    def _source_sender(self, value: object, *, email: bool) -> dict:
        sender = {"id": None, "display_name": None, "address": None, "address_kind": None}
        if value is None:
            return sender
        if not isinstance(value, dict):
            raise SourceUnreadableError("Work IQ source sender was invalid.")
        if email:
            person = value.get("emailAddress")
            name_key = "name"
        else:
            identities = [value[key] for key in ("user", "application", "device") if value.get(key) is not None]
            if len(identities) != 1:
                raise SourceUnreadableError("Work IQ source sender was invalid.")
            person = identities[0]
            name_key = "displayName"
        if not isinstance(person, dict):
            raise SourceUnreadableError("Work IQ source sender was invalid.")
        if person.get("id") is not None:
            sender["id"] = self._source_id(person["id"])
        if person.get(name_key) is not None:
            sender["display_name"] = self._source_text(person[name_key], limit=256)
        if person.get("address") is not None:
            address = person["address"]
            if (
                not isinstance(address, str) or len(address) > 320
                or any(unicodedata.category(char).startswith("C") for char in address)
            ):
                raise SourceUnreadableError("Work IQ source sender was invalid.")
            if address.startswith("/"):
                legacy_dn = re.fullmatch(
                    r"/o=([^/\\=<>]+)/ou=([^/\\=<>]+)/cn=Recipients/cn=([^/\\=<>]+)",
                    address, re.IGNORECASE,
                )
                if not legacy_dn or any(value != value.strip() for value in legacy_dn.groups()):
                    raise SourceUnreadableError("Work IQ source sender was invalid.")
                # A legacy Exchange DN is not an SMTP address or person authority.
                sender["address_kind"] = "exchange_dn"
            else:
                if re.fullmatch(r"[^@\s<>]+@[^@\s<>]+", address) is None:
                    raise SourceUnreadableError("Work IQ source sender was invalid.")
                sender["address"] = address
                sender["address_kind"] = "smtp"
        return sender

    def _validate_source_optional_fields(self, item: dict, *, email: bool) -> None:
        if item.get("subject") is not None:
            self._source_text(item["subject"], limit=SOURCE_EXCERPT_LIMIT)
        if item.get("internetMessageId") is not None:
            self._source_id(item["internetMessageId"])
        for key in ("createdDateTime", "receivedDateTime", "sentDateTime", "lastModifiedDateTime", "deletedDateTime"):
            if item.get(key) is not None:
                self._source_timestamp(item[key])
        if "from" in item:
            self._source_sender(item["from"], email=email)
        if "bodyPreview" in item:
            self._source_text(item["bodyPreview"], limit=SOURCE_EXCERPT_LIMIT)
        link_key = "webLink" if email else "webUrl"
        if item.get(link_key) is not None:
            self._source_url(item[link_key], hosts=(
                {"outlook.office.com", "outlook.office365.com", "outlook.live.com"}
                if email else {"teams.microsoft.com"}
            ))

    def _project_source_page(
        self, source: SourceReadOperation, conversation: str | None, data: dict,
    ) -> dict:
        self._source_complete(data)
        values = data.get("value")
        email = source.kind == "email"
        if not isinstance(values, list) or len(values) > (25 if email else 50) or "id" in data:
            raise SourceUnreadableError("Work IQ source collection was invalid.")
        ids = dict(source.identifiers)
        items = []
        seen = set()
        for item in values:
            if not isinstance(item, dict):
                raise SourceUnreadableError("Work IQ source item was invalid.")
            item_id = self._source_id(item.get("id"))
            if item_id in seen:
                raise SourceUnreadableError("Work IQ source contained duplicate items.")
            seen.add(item_id)
            foreign_fields = (
                ("chatId", "channelIdentity", "replyToId") if email
                else ("conversationId",) if source.kind == "teams_channel"
                else ("conversationId", "channelIdentity")
            )
            if any(item.get(key) is not None for key in foreign_fields):
                raise SourceUnreadableError("Work IQ source message identity did not match.")
            if email:
                if self._source_id(item.get("conversationId")) != conversation:
                    raise SourceUnreadableError("Work IQ source message identity did not match.")
            elif source.kind == "teams_channel":
                if item.get("chatId") is not None:
                    raise SourceUnreadableError("Work IQ source message identity did not match.")
                if item.get("replyToId") is not None and item["replyToId"] != ids["message_id"]:
                    raise SourceUnreadableError("Work IQ source message identity did not match.")
                if item.get("channelIdentity") is not None:
                    channel = item["channelIdentity"]
                    if not isinstance(channel, dict) or channel.get("teamId") != ids["team_id"] or channel.get("channelId") != ids["channel_id"]:
                        raise SourceUnreadableError("Work IQ source message identity did not match.")
            elif item.get("chatId") is not None and item["chatId"] != conversation:
                raise SourceUnreadableError("Work IQ source message identity did not match.")
            self._validate_source_optional_fields(item, email=email)
            occurred = self._source_timestamp(item.get("receivedDateTime" if email else "createdDateTime"))
            if email:
                excerpt = self._source_text(item.get("bodyPreview"), limit=SOURCE_EXCERPT_LIMIT)
            else:
                body = item.get("body")
                if not isinstance(body, dict) or body.get("contentType") not in {"html", "text"}:
                    raise SourceUnreadableError("Work IQ source body was invalid.")
                excerpt = self._source_text(body.get("content"), limit=SOURCE_EXCERPT_LIMIT, html=body["contentType"] == "html")
            items.append({
                "source_item_id": item_id, "occurred_at": occurred,
                "sender": self._source_sender(item.get("from"), email=email),
                "excerpt": excerpt, "web_url": item.get("webLink" if email else "webUrl"),
            })
        if conversation is not None:
            ids["conversation_id"] = conversation
        items.sort(key=lambda item: (datetime.fromisoformat(item["occurred_at"].replace("Z", "+00:00")), item["source_item_id"]))
        return {
            "source_kind": source.kind, "locator_source": source.locator_source,
            "source_identity": ids, "conversation_id": conversation,
            "complete": True, "items": items,
        }

    def _run_readiness(self, operation: _Operation) -> None:
        if ACTION_TOOL not in self._allowed:
            raise CapabilityDeniedError(
                "Work IQ does not advertise calendar actions."
            )
        deadline = time.monotonic() + operation.timeout
        try:
            account = get_setup().configured_account(
                timeout=max(0.1, deadline - time.monotonic())
            )
        except WorkIQAccountRequiredError as exc:
            raise AuthRequiredError("Work IQ authentication is required.") from exc
        except WorkIQAccountError as exc:
            raise SetupUnavailableError(
                "Work IQ account configuration is unavailable."
            ) from exc
        start = self._clock().astimezone(timezone.utc).replace(
            second=0,
            microsecond=0,
        )
        calendar = build_calendar_operation(
            CalendarAction.GET_SCHEDULE,
            {
                "schedules": [account],
                "startTime": {
                    "dateTime": start.replace(tzinfo=None).isoformat(),
                    "timeZone": "UTC",
                },
                "endTime": {
                    "dateTime": (
                        start + timedelta(minutes=30)
                    ).replace(tzinfo=None).isoformat(),
                    "timeZone": "UTC",
                },
            },
        )
        result = self._rpc(
            "tools/call",
            {
                "name": ACTION_TOOL,
                "arguments": {
                    "actionUrl": calendar.path,
                    "jsonBody": calendar.body,
                },
            },
            max(0.1, deadline - time.monotonic()),
        )
        data = self._validate_calendar_result(calendar, result)
        schedules = data.get("value")
        if (
            not isinstance(schedules, list)
            or len(schedules) != 1
            or not isinstance(schedules[0], Mapping)
            or str(schedules[0].get("scheduleId") or "").strip().lower()
            != account.lower()
            or (
                "error" in schedules[0]
                and schedules[0]["error"] is not None
            )
        ):
            raise InvalidStructuredContentError(
                "Work IQ readiness did not return the configured account."
            )
        self._finish(
            operation,
            "succeeded",
            runtime_state="ready",
            authenticated=True,
        )

    def _run_calendar(self, operation: _Operation) -> None:
        calendar = require_calendar_operation(operation.calendar)
        with self._lock:
            if not self._authenticated or ACTION_TOOL not in self._allowed:
                raise NotReadyError("Work IQ authenticated readiness is required.")
        result = self._rpc(
            "tools/call",
            {
                "name": ACTION_TOOL,
                "arguments": {
                    "actionUrl": calendar.path,
                    "jsonBody": calendar.body,
                },
            },
            operation.timeout,
        )
        data = self._validate_calendar_result(calendar, result)
        self._finish(
            operation,
            "succeeded",
            result_data=data,
            runtime_state="ready",
        )

    def _run_sent_items(self, operation: _Operation) -> None:
        with self._lock:
            if not self._authenticated:
                raise NotReadyError("Work IQ authenticated readiness is required.")
            if FETCH_TOOL not in self._allowed:
                raise CapabilityDeniedError(
                    "Work IQ does not advertise the required email read capability."
                )
        result = self._rpc(
            "tools/call",
            {
                "name": FETCH_TOOL,
                "arguments": {"entityUrls": [SENT_ITEMS_URL]},
            },
            operation.timeout,
        )
        data = self._validate_sent_items_result(result)
        self._finish(
            operation,
            "succeeded",
            result_data=data,
            runtime_state="ready",
        )

    def _rpc(
        self, method: str, params: dict, timeout: float,
        *, ask_operation: _Operation | None = None,
    ) -> dict:
        with self._lock:
            active = ask_operation or self._active_operation
            self._request_id += 1
            request_id = self._request_id
            message = {
                "jsonrpc": "2.0", "id": request_id, "method": method, "params": params,
            }
            if ask_operation is not None:
                if (
                    ask_operation.cancel_requested.is_set()
                    or ask_operation.event.is_set()
                    or self._stopping.is_set()
                ):
                    raise CancelledError("Work IQ ask was cancelled.")
                timeout = min(
                    timeout, self._ask_remaining(ask_operation, ask_operation.ask_deadline)
                )
                self._active_request_id = request_id
                self._send(message)
            else:
                self._active_request_id = request_id
        if ask_operation is None:
            self._send(message)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._send_notification(
                    "notifications/cancelled",
                    {"requestId": request_id, "reason": "Riveter deadline exceeded"},
                )
                raise TimeoutError(f"Work IQ {method} timed out.")
            try:
                raw = self._incoming.get(timeout=remaining)
            except queue.Empty as exc:
                self._send_notification(
                    "notifications/cancelled",
                    {"requestId": request_id, "reason": "Riveter deadline exceeded"},
                )
                raise TimeoutError(f"Work IQ {method} timed out.") from exc
            if raw is None:
                raise TransportError("Work IQ MCP closed its stdout.")
            message = self._parse_message(raw)
            if "id" in message and isinstance(message.get("method"), str):
                self._handle_server_request(message)
                continue
            if "id" not in message:
                if isinstance(message.get("method"), str):
                    continue
                raise ProtocolError("Work IQ MCP emitted an invalid notification.")
            if message["id"] != request_id:
                raise ProtocolError("Work IQ MCP response correlation failed.")
            if "error" in message:
                if active and active.plan in {"ask", "ask_probe", "source"}:
                    error = message["error"]
                    raise self._ask_remote_error(
                        error.get("code") if isinstance(error, dict) else None
                    )
                raise self._classify_rpc_error(message["error"])
            result = message.get("result")
            if not isinstance(result, dict):
                raise ProtocolError("Work IQ MCP response did not contain an object result.")
            return result

    def _service_idle_messages(self) -> None:
        while True:
            try:
                raw = self._incoming.get_nowait()
            except queue.Empty:
                return
            message = self._parse_message(raw)
            if "id" in message and isinstance(message.get("method"), str):
                self._handle_server_request(message)
                continue
            if "id" not in message and isinstance(message.get("method"), str):
                continue
            raise ProtocolError("Work IQ MCP emitted an unexpected response while idle.")

    def _parse_message(self, raw) -> dict:
        if raw is None:
            raise TransportError("Work IQ MCP closed its stdout.")
        if isinstance(raw, BaseException):
            raise TransportError("Work IQ MCP stdout failed.") from raw
        if len(raw) > self._max_payload_bytes:
            raise InvalidResponseError("Work IQ response exceeded Riveter's payload limit.")
        try:
            message = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolError("Work IQ MCP emitted malformed JSON.") from exc
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise ProtocolError("Work IQ MCP emitted an invalid JSON-RPC message.")
        return message

    def _handle_server_request(self, message: dict) -> None:
        request_id = message["id"]
        method = message["method"]
        if method == "ping":
            self._send({"jsonrpc": "2.0", "id": request_id, "result": {}})
            return
        self._send({
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": "Method not found"},
        })

    def _send(self, message: dict) -> None:
        payload = json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(payload) > self._max_payload_bytes:
            raise InvalidResponseError("Work IQ request exceeded Riveter's payload limit.")
        with self._write_lock:
            process = self._process
            if not process or process.poll() is not None or not process.stdin:
                raise TransportError("Work IQ MCP process is not available.")
            try:
                process.stdin.write(payload)
                process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise TransportError("Could not write to Work IQ MCP.") from exc

    def _send_notification(self, method: str, params: dict) -> None:
        try:
            self._send({"jsonrpc": "2.0", "method": method, "params": params})
        except WorkIQError:
            pass

    def _read_stdout(self) -> None:
        process = self._process
        incoming = self._incoming
        try:
            if not process or not process.stdout:
                incoming.put(TransportError("Work IQ stdout is unavailable."))
                return
            while True:
                raw = process.stdout.readline(self._max_payload_bytes + 2)
                if not raw:
                    self._queue_terminal(incoming, None)
                    return
                try:
                    incoming.put_nowait(raw)
                except queue.Full:
                    self._queue_terminal(
                        incoming,
                        TransportError(
                            "Work IQ MCP overwhelmed its response queue."
                        ),
                    )
                    try:
                        process.terminate()
                    except OSError:
                        pass
                    return
                raw = None
        except (OSError, ValueError) as exc:
            self._queue_terminal(incoming, exc)

    @staticmethod
    def _queue_terminal(incoming: queue.Queue, marker: object) -> None:
        for _attempt in range(2):
            try:
                incoming.put_nowait(marker)
                return
            except queue.Full:
                try:
                    incoming.get_nowait()
                except queue.Empty:
                    continue

    def _read_stderr(self) -> None:
        process = self._process
        try:
            if not process or not process.stderr:
                return
            while True:
                raw = process.stderr.readline(4096)
                if not raw:
                    return
                self._append_diagnostic(raw.decode("utf-8", errors="replace"))
        except (OSError, ValueError):
            return

    def _append_diagnostic(self, text: object) -> None:
        if not str(text or "").strip():
            return
        # Child stderr can contain personal response data without a stable
        # envelope. Retain only the event class, never its contents.
        value = "Work IQ emitted redacted diagnostic output."
        with self._lock:
            self._diagnostics.append(value)
            self._diagnostic_size += len(value.encode("utf-8"))
            while self._diagnostics and self._diagnostic_size > self._stderr_limit_bytes:
                removed = self._diagnostics.popleft()
                self._diagnostic_size -= len(removed.encode("utf-8"))

    def _classify_rpc_error(self, error: object) -> WorkIQError:
        if isinstance(error, dict):
            code = str(error.get("code", "")).lower()
            message = str(error.get("message", "Work IQ request failed."))
        else:
            code = ""
            message = "Work IQ request failed."
        return self._error_from_code(code, message)

    @staticmethod
    def _validate_tool_result(result: dict) -> tuple[bool, dict, list[str]]:
        content = result.get("content")
        is_error = result.get("isError", False)
        meta = result.get("_meta") or {}
        if (
            not isinstance(content, list)
            or not isinstance(is_error, bool)
            or not isinstance(meta, dict)
        ):
            raise InvalidResponseError("Work IQ returned invalid readiness content.")
        texts = [
            item.get("text")
            for item in content
            if isinstance(item, dict)
            and item.get("type") == "text"
            and isinstance(item.get("text"), str)
        ]
        if len(texts) != len(content):
            raise InvalidResponseError("Work IQ returned non-text readiness content.")
        return is_error, meta, texts

    def _validate_calendar_result(
        self,
        operation: CalendarOperation,
        result: dict,
    ) -> dict:
        content = result.get("content")
        is_error = result.get("isError", False)
        meta = result.get("_meta") or {}
        if (
            not isinstance(content, list)
            or not isinstance(is_error, bool)
            or not isinstance(meta, dict)
        ):
            raise InvalidStructuredContentError(
                "Work IQ calendar action returned invalid structured content."
            )
        texts = [
            item.get("text")
            for item in content
            if isinstance(item, dict)
            and item.get("type") == "text"
            and isinstance(item.get("text"), str)
        ]
        if is_error:
            remote = self._classify_remote_error(meta, texts)
            if isinstance(remote, (EulaRequiredError, AuthRequiredError, ConsentRequiredError)):
                raise remote
            raise ToolError("Work IQ calendar action failed.")
        structured = result.get("structuredContent")
        if not isinstance(structured, Mapping):
            raise InvalidStructuredContentError(
                "Work IQ calendar action returned invalid structured content."
            )
        status = structured.get("statusCode")
        if not isinstance(status, int) or isinstance(status, bool):
            raise InvalidStructuredContentError(
                "Work IQ calendar action returned invalid structured content."
            )
        if status != 200:
            raise ActionHTTPError("Work IQ calendar action returned a non-success status.")
        data = structured.get("data")
        if not isinstance(data, Mapping):
            raise InvalidStructuredContentError(
                "Work IQ calendar action returned invalid structured content."
            )
        expected = (
            "meetingTimeSuggestions"
            if operation.path == CalendarAction.FIND_MEETING_TIMES.value
            else "value"
        )
        if not isinstance(data.get(expected), list):
            raise InvalidStructuredContentError(
                "Work IQ calendar action returned invalid structured content."
            )
        return dict(data)

    def _validate_sent_items_result(self, result: dict) -> dict:
        content = result.get("content")
        is_error = result.get("isError", False)
        meta = result.get("_meta") or {}
        if (
            not isinstance(content, list)
            or not isinstance(is_error, bool)
            or not isinstance(meta, dict)
        ):
            raise InvalidStructuredContentError(
                "Work IQ Sent Items read returned invalid structured content."
            )
        texts = [
            item.get("text")
            for item in content
            if isinstance(item, dict)
            and item.get("type") == "text"
            and isinstance(item.get("text"), str)
        ]
        if len(texts) != len(content):
            raise InvalidStructuredContentError(
                "Work IQ Sent Items read returned invalid structured content."
            )
        if is_error:
            remote = self._classify_remote_error(meta, texts)
            if isinstance(
                remote,
                (EulaRequiredError, AuthRequiredError, ConsentRequiredError),
            ):
                raise remote
            raise ToolError("Work IQ Sent Items read failed.")
        structured = result.get("structuredContent")
        results = (
            structured.get("results")
            if isinstance(structured, Mapping)
            else None
        )
        if (
            not isinstance(results, list)
            or len(results) != 1
            or not isinstance(results[0], Mapping)
        ):
            raise InvalidStructuredContentError(
                "Work IQ Sent Items read returned invalid structured content."
            )
        status = results[0].get("statusCode")
        if not isinstance(status, int) or isinstance(status, bool):
            raise InvalidStructuredContentError(
                "Work IQ Sent Items read returned invalid structured content."
            )
        if status != 200:
            raise ActionHTTPError(
                "Work IQ Sent Items read returned a non-success status."
            )
        data = results[0].get("data")
        if not isinstance(data, Mapping) or not isinstance(data.get("value"), list):
            raise InvalidStructuredContentError(
                "Work IQ Sent Items read returned invalid structured content."
            )
        return dict(data)

    def _classify_remote_error(self, meta: dict, texts: list[str]) -> WorkIQError:
        code = ""
        if isinstance(meta, dict):
            code = str(meta.get("code", "")).lower()
        text = " ".join(texts)
        return self._error_from_code(code, text or "Work IQ request failed.")

    @staticmethod
    def _error_from_code(code: str, message: str) -> WorkIQError:
        normalized = f"{code} {message}".lower()
        if code == "eula_required" or "end user license" in normalized:
            return EulaRequiredError("Work IQ requires EULA acceptance.")
        if code == "auth_required" or "sign in is required" in normalized:
            return AuthRequiredError("Work IQ authentication is required.")
        if code == "consent_required" or "consent is required" in normalized:
            return ConsentRequiredError("Work IQ administrator consent is required.")
        return RemoteError("Work IQ rejected the readiness request.")

    @staticmethod
    def _raise_public_error(error: object) -> None:
        public = error if isinstance(error, dict) else {}
        code = public.get("code")
        message = str(public.get("message") or "Work IQ calendar action failed.")
        error_types = {
            "dependency_missing": DependencyError,
            "version_mismatch": VersionMismatchError,
            "transport": TransportError,
            "protocol": ProtocolError,
            "remote": RemoteError,
            "mcp_unavailable": SetupUnavailableError,
            "invalid_response": InvalidResponseError,
            "capability_denied": CapabilityDeniedError,
            "auth_required": AuthRequiredError,
            "consent_required": ConsentRequiredError,
            "eula_required": EulaRequiredError,
            "not_ready": NotReadyError,
            "tool": ToolError,
            "action_http": ActionHTTPError,
            "invalid_structured_content": InvalidStructuredContentError,
            "source_unreadable": SourceUnreadableError,
            "source_partial": SourcePartialError,
            "source_forbidden": SourceForbiddenError,
            "source_not_found": SourceNotFoundError,
            "source_http": SourceHTTPError,
            "timeout": TimeoutError,
            "cancelled": CancelledError,
        }
        raise error_types.get(code, WorkIQError)(message)

    def _finish(
        self,
        operation: _Operation,
        state: str,
        error: WorkIQError | None = None,
        result_data: dict | None = None,
        *,
        runtime_state: str | None = None,
        runtime_error: WorkIQError | None = None,
        authenticated: bool | None = None,
    ) -> None:
        with self._lock:
            if operation.event.is_set():
                return
            if state == "succeeded" and operation.ask_deadline is not None:
                self._ask_remaining(operation, operation.ask_deadline)
            if runtime_state is not None:
                self._state = runtime_state
                self._error = runtime_error.public() if runtime_error else None
            if authenticated is not None:
                self._authenticated = authenticated
                if not authenticated:
                    self._ask_ready = False
                    self._source_ready = False
            if self._active_operation is operation:
                self._active_operation = None
                self._active_request_id = None
            operation.state = state
            operation.calendar = None
            operation.ask = None
            operation.source = None
            operation.result = {
                "ok": error is None,
                **({"error": error.public()} if error else {}),
                **({"data": result_data} if result_data is not None else {}),
            }
            operation.event.set()
            while len(self._operations) > self._history_limit:
                oldest_id, oldest = next(iter(self._operations.items()))
                if not oldest.event.is_set():
                    break
                self._operations.pop(oldest_id)

    def _set_error(self, error: WorkIQError, state: str) -> None:
        with self._lock:
            self._state = state
            self._error = error.public()

    def _terminate_child(
        self,
        allow_grace: bool = False,
        *,
        process=None,
        incoming=None,
        reader_thread=None,
        stderr_thread=None,
    ) -> None:
        with self._lock:
            target_process = process or self._process
            if target_process is self._process:
                self._ask_ready = False
                self._source_ready = False
                self._authenticated = False
            target_incoming = incoming or self._incoming
            target_reader = (
                reader_thread if process is not None else self._reader_thread
            )
            target_stderr = (
                stderr_thread if process is not None else self._stderr_thread
            )
        if not target_process:
            return
        try:
            if target_process.poll() is None:
                if allow_grace:
                    try:
                        target_process.wait(timeout=self._terminate_grace)
                    except subprocess.TimeoutExpired:
                        pass
                if target_process.poll() is None:
                    target_process.terminate()
                    try:
                        target_process.wait(timeout=self._terminate_grace)
                    except subprocess.TimeoutExpired:
                        target_process.kill()
                        target_process.wait(timeout=self._terminate_grace)
        except (OSError, subprocess.SubprocessError):
            pass
        for stream in (
            target_process.stdin,
            target_process.stdout,
            target_process.stderr,
        ):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass
        for thread in (target_reader, target_stderr):
            if (
                thread
                and thread is not threading.current_thread()
                and thread.ident is not None
            ):
                thread.join(self._terminate_grace)
        with self._lock:
            if (
                target_process.poll() is not None
                and self._process is target_process
            ):
                self._process = None
                if self._incoming is target_incoming:
                    self._incoming = queue.Queue(
                        maxsize=self._max_incoming_messages
                    )
            if not target_reader or not target_reader.is_alive():
                if self._reader_thread is target_reader:
                    self._reader_thread = None
            if not target_stderr or not target_stderr.is_alive():
                if self._stderr_thread is target_stderr:
                    self._stderr_thread = None


_default_runtime = WorkIQRuntime()


def get_runtime() -> WorkIQRuntime:
    return _default_runtime


def shutdown_runtime() -> None:
    _default_runtime.shutdown()


atexit.register(shutdown_runtime)
