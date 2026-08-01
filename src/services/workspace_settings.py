"""Local task-workspace settings.

The configured root may itself be a OneDrive reparse point. That is allowed.
Future child-folder creation must separately reject junctions that escape the
resolved root; ``Path.is_symlink()`` alone does not detect every NTFS junction.
"""

import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SETTINGS_PATH = PROJECT_ROOT / "data" / "settings.json"


def validate_workspace_root(value: str | None) -> Path | None:
    """Return a resolved existing local directory, or None when unsafe."""
    if not value or value.startswith("\\\\"):
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
