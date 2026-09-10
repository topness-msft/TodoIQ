"""Supervised, read-only Work IQ MCP stdio boundary."""

from __future__ import annotations

import json
import logging
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
from typing import Callable

from .workiq_policy import (
    ACTION_TOOL,
    FETCH_TOOL,
    SENT_ITEMS_URL,
    CalendarAction,
    CalendarOperation,
    CapabilityError,
    build_calendar_operation,
    discover_read_capabilities,
    require_calendar_operation,
)
from .workiq_setup import (
    WorkIQAccountError,
    WorkIQAccountRequiredError,
    get_setup,
    redact,
)


logger = logging.getLogger(__name__)
PROTOCOL_VERSION = "2025-06-18"


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
        self._request_id = 0
        self._active_operation: _Operation | None = None
        self._active_request_id: int | None = None

    def start(self) -> dict:
        with self._start_lock:
            return self._start_serialized()

    def _start_serialized(self) -> dict:
        startup_deadline = time.monotonic() + self._startup_timeout
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
            self._thread = threading.Thread(
                target=self._worker_main,
                args=(startup_deadline,),
                daemon=True,
                name="workiq-mcp-worker",
            )
            self._thread.start()
        remaining = max(0.0, startup_deadline - time.monotonic())
        if not self._ready_event.wait(remaining):
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

    def submit(
        self,
        plan: str = "readiness",
        timeout: float = 45,
        *,
        calendar: CalendarOperation | None = None,
    ) -> str:
        if plan not in {"readiness", "calendar", "sent_items"}:
            raise CapabilityError("Riveter has no approved plan for this read.")
        if plan == "calendar":
            calendar = require_calendar_operation(calendar)
        elif calendar is not None:
            raise CapabilityError("Readiness cannot carry a calendar operation.")
        with self._lock:
            if self._stopping.is_set():
                raise RuntimeStoppingError("Work IQ MCP is stopping.")
            if not self._thread or not self._thread.is_alive():
                raise TransportError("Work IQ MCP is not running.")
            operation = _Operation(
                job_id=str(uuid.uuid4()),
                timeout=timeout,
                plan=plan,
                calendar=calendar,
            )
            self._operations[operation.job_id] = operation
        try:
            self._commands.put_nowait(operation)
        except queue.Full as exc:
            with self._lock:
                self._operations.pop(operation.job_id, None)
            raise QueueFullError("Work IQ read queue is full.") from exc
        return operation.job_id

    def wait(self, job_id: str, timeout: float | None = None) -> dict:
        with self._lock:
            operation = self._operations.get(job_id)
        if operation is None:
            return {
                "job_id": job_id,
                "state": "failed",
                "ok": False,
                "error": {"code": "not_found", "message": "Unknown Work IQ job."},
            }
        operation.event.wait(timeout)
        return operation.public()

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
                if operation.event.is_set() or operation.cancel_requested.is_set():
                    continue
                with self._lock:
                    self._active_operation = operation
                    operation.state = "running"
                    self._state = "busy"
                fatal = self._run_operation(operation)
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
            self._allowed = allowed

    def _run_operation(self, operation: _Operation) -> bool:
        try:
            if operation.plan == "readiness":
                self._run_readiness(operation)
            elif operation.plan == "calendar":
                self._run_calendar(operation)
            else:
                self._run_sent_items(operation)
            return False
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

    def _rpc(self, method: str, params: dict, timeout: float) -> dict:
        with self._lock:
            self._request_id += 1
            request_id = self._request_id
            self._active_request_id = request_id
        self._send({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        })
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
            if runtime_state is not None:
                self._state = runtime_state
                self._error = runtime_error.public() if runtime_error else None
            if authenticated is not None:
                self._authenticated = authenticated
            if self._active_operation is operation:
                self._active_operation = None
                self._active_request_id = None
            operation.state = state
            operation.calendar = None
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
