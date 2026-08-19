"""Process-wide runtime safety switches."""

import os


def demo_mode() -> bool:
    return os.environ.get("RIVETER_DEMO_MODE", "").strip() == "1"


def _demo_flag(name: str) -> bool:
    return os.environ.get(name, "").strip() == "1"


def todo_parse_enabled() -> bool:
    return not demo_mode() or _demo_flag("RIVETER_DEMO_ALLOW_TODO_PARSE")


def cowork_session_enabled() -> bool:
    return not demo_mode() or _demo_flag("RIVETER_DEMO_ALLOW_COWORK_SESSION")


def cowork_execute_enabled() -> bool:
    return not demo_mode() or _demo_flag("RIVETER_DEMO_ALLOW_COWORK_EXECUTE")


def copilot_command_enabled(command: str, label: str) -> bool:
    if not demo_mode():
        return True
    return todo_parse_enabled() and command.strip() == "/todo-parse" and label == "parse"


def external_integrations_enabled() -> bool:
    return not demo_mode()


DEMO_DISABLED_MESSAGE = (
    "External Microsoft 365 and Cowork actions are disabled in Riveter demo mode."
)
