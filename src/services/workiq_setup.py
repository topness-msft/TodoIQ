"""Pinned Work IQ CLI discovery and explicit setup actions."""

from __future__ import annotations

import json
import platform
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Callable


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = PROJECT_ROOT / "scripts" / "workiq-runtime.json"
DEFAULT_RUNTIME_ROOT = PROJECT_ROOT / "data" / "runtime" / "workiq"
EULA_URL = "https://github.com/microsoft/work-iq"
_SECRET_RE = re.compile(
    r"(?i)(bearer\s+|access[_-]?token\s*[=:]\s*|refresh[_-]?token\s*[=:]\s*|"
    r"cookie\s*[=:]\s*)([^\s,;]+)"
)


class WorkIQAccountError(RuntimeError):
    """The pinned runtime's account configuration could not be read safely."""


class WorkIQAccountRequiredError(WorkIQAccountError):
    """The pinned runtime has no configured default account."""


def redact(text: object, limit: int = 32 * 1024) -> str:
    value = str(text or "")
    value = _SECRET_RE.sub(lambda match: match.group(1) + "[REDACTED]", value)
    return value[-limit:]


def load_runtime_manifest(path: Path = MANIFEST_PATH) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "package",
        "packageVersion",
        "serverName",
        "serverVersion",
        "platformEntries",
        "mcpArguments",
        "eulaArguments",
    }
    if not isinstance(data, dict) or not required.issubset(data):
        raise ValueError("Invalid Work IQ runtime manifest.")
    return data


class WorkIQSetup:
    """Side-effect-free inspection plus separately invoked EULA acceptance."""

    def __init__(
        self,
        runtime_root: Path = DEFAULT_RUNTIME_ROOT,
        node_path: Path | str | None = None,
        executor: Callable = subprocess.run,
        manifest_path: Path = MANIFEST_PATH,
    ):
        self.runtime_root = Path(runtime_root)
        discovered = node_path if node_path is not None else shutil.which("node")
        self.node_path = Path(discovered).resolve() if discovered else None
        self.executor = executor
        self.manifest = load_runtime_manifest(manifest_path)
        self._eula_lock = threading.Lock()

    @property
    def package_root(self) -> Path:
        return self.runtime_root / "node_modules" / "@microsoft" / "workiq"

    @property
    def package_json(self) -> Path:
        return self.package_root / "package.json"

    @property
    def entry_point(self) -> Path:
        machine = platform.machine().lower()
        key = "win32-arm64" if machine in {"arm64", "aarch64"} else "win32-x64"
        relative = self.manifest["platformEntries"].get(key)
        return self.runtime_root / relative if relative else self.runtime_root / "unsupported"

    def inspect(self) -> dict:
        base = {
            "required_version": self.manifest["packageVersion"],
            "server_version": self.manifest["serverVersion"],
            "eula_url": EULA_URL,
            "runtime_root": str(self.runtime_root),
            "install_command": r"powershell -ExecutionPolicy Bypass -File scripts\setup_workiq.ps1",
        }
        if (
            self.node_path is None
            or not self.node_path.is_file()
            or not self.package_json.is_file()
            or not self.entry_point.is_file()
        ):
            return {**base, "state": "missing_cli", "installed_version": None}
        try:
            package = json.loads(self.package_json.read_text(encoding="utf-8"))
            installed = package.get("version")
        except (OSError, json.JSONDecodeError):
            installed = None
        if installed != self.manifest["packageVersion"]:
            return {
                **base,
                "state": "version_mismatch",
                "installed_version": installed,
            }
        return {
            **base,
            "state": "mcp_unavailable",
            "installed_version": installed,
        }

    def runtime_command(self) -> list[str]:
        status = self.inspect()
        if status["state"] != "mcp_unavailable":
            raise RuntimeError(status["state"])
        return [
            str(self.entry_point.resolve()),
            *self.manifest["mcpArguments"],
        ]

    def configured_account(self, timeout: float = 30) -> str:
        status = self.inspect()
        if status["state"] != "mcp_unavailable":
            raise WorkIQAccountError(
                "The pinned Work IQ runtime is not available."
            )
        argv = [str(self.entry_point.resolve()), "config", "show"]
        kwargs = {
            "capture_output": True,
            "text": True,
            "timeout": max(0.1, min(30, timeout)),
            "shell": False,
            "cwd": str(PROJECT_ROOT),
        }
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            result = self.executor(argv, **kwargs)
        except (OSError, subprocess.SubprocessError) as exc:
            raise WorkIQAccountError(
                "Work IQ account configuration is unavailable."
            ) from exc
        if result.returncode != 0:
            raise WorkIQAccountError(
                "Work IQ account configuration is unavailable."
            )
        accounts = []
        malformed = False
        for raw_line in str(result.stdout or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if "=" not in line:
                malformed = True
                continue
            key, value = line.split("=", 1)
            if key.strip() == "defaultAccount":
                accounts.append(value.strip())
        if malformed or len(accounts) > 1:
            raise WorkIQAccountError(
                "Work IQ account configuration is invalid."
            )
        if not accounts or not accounts[0]:
            raise WorkIQAccountRequiredError(
                "Work IQ sign-in is required."
            )
        if (
            "=" in accounts[0]
            or re.fullmatch(r"[^\s@=]+@[^\s@=]+", accounts[0]) is None
        ):
            raise WorkIQAccountError(
                "Work IQ account configuration is invalid."
            )
        return accounts[0]

    def accept_eula(self) -> dict:
        if not self._eula_lock.acquire(blocking=False):
            return {
                "ok": False,
                "error": {"code": "already_running", "message": "EULA acceptance is already running."},
            }
        try:
            status = self.inspect()
            if status["state"] != "mcp_unavailable":
                return {
                    "ok": False,
                    "error": {
                        "code": status["state"],
                        "message": "The pinned Work IQ runtime is not available.",
                    },
                }
            argv = [
                str(self.entry_point.resolve()),
                *self.manifest["eulaArguments"],
            ]
            kwargs = {
                "capture_output": True,
                "text": True,
                "timeout": 30,
                "shell": False,
                "cwd": str(PROJECT_ROOT),
            }
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            try:
                result = self.executor(argv, **kwargs)
            except (OSError, subprocess.SubprocessError) as exc:
                return {
                    "ok": False,
                    "error": {"code": "mcp_unavailable", "message": redact(exc)},
                }
            if result.returncode != 0:
                return {
                    "ok": False,
                    "error": {
                        "code": "eula_required",
                        "message": redact(result.stderr or result.stdout or "EULA acceptance failed."),
                    },
                }
            return {"ok": True, "message": "Work IQ EULA accepted. Check readiness again."}
        finally:
            self._eula_lock.release()


_default_setup = WorkIQSetup()


def get_setup() -> WorkIQSetup:
    return _default_setup
