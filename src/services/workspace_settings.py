"""Local task-workspace settings.

The configured root may itself be a OneDrive reparse point. That is allowed.
Future child-folder creation must separately reject junctions that escape the
resolved root; ``Path.is_symlink()`` alone does not detect every NTFS junction.
"""

import json
import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SETTINGS_PATH = Path(
    os.environ.get("TODONESS_SETTINGS_PATH", PROJECT_ROOT / "data" / "settings.json")
).resolve()


def validate_workspace_root(value: str | None) -> Path | None:
    """Return a resolved existing local directory, or None when unsafe."""
    if not value or value.startswith("\\\\") or value.startswith("//"):
        return None
    path = Path(value)
    if not path.is_absolute() or not path.exists() or not path.is_dir():
        return None
    try:
        return path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None


def get_workspace_settings() -> dict:
    """Load the gitignored user setting; invalid configuration fails closed."""
    if not SETTINGS_PATH.exists():
        return {"enabled": False}
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"enabled": False}

    configured = data.get("task_workspaces")
    if not isinstance(configured, dict):
        return {"enabled": False}
    root = validate_workspace_root(configured.get("root"))
    if root is None:
        return {"enabled": False}
    return {"enabled": bool(configured.get("enabled")), "root": str(root)}


def _read_settings() -> dict:
    """Whole settings document, or an empty one when absent or malformed."""
    if not SETTINGS_PATH.exists():
        return {}
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def missing_settings_warning() -> str | None:
    """Say when the settings file is absent, rather than quietly defaulting.

    Every reader here falls back to a default when the document is missing,
    which is the right behaviour but a silent one. The file was lost in a
    checkout migration and nothing said so: meeting preferences reverted to
    unset and the Cowork transport flag reverted to off, and the first
    symptom was meeting times coming back on the hour days later. A missing
    file is a fact, so state it.
    """
    if SETTINGS_PATH.exists():
        return None
    return (
        f"No settings file at {SETTINGS_PATH}. Meeting preferences and the "
        "Cowork API transport are running on defaults, not on anything you "
        "configured."
    )


def api_transport_enabled() -> bool:
    """Is the Cowork run transport allowed to use the runtime HTTP API?

    Default FALSE, and unreadable or malformed configuration also reads False,
    so the proven `cowork` subprocess path stays in charge unless the flag is
    deliberately turned on. TodoIQ is a daily driver; the API path has a bake
    window measured in weeks, and the subprocess path must keep working
    untouched throughout it.

    Scope: this gates ONLY the run transport (``start_preview``). Additive
    reads such as GET /v1/cost are not flagged, because if they fail the user
    simply sees no badge, whereas a transport failure breaks an existing
    feature. The rule is: flag what replaces, ship what adds.
    """
    return bool(_read_settings().get("cowork_api_transport") is True)
