"""Process-wide runtime safety switches."""

import os


def demo_mode() -> bool:
    return os.environ.get("RIVETER_DEMO_MODE", "").strip() == "1"


def external_integrations_enabled() -> bool:
    return not demo_mode()


DEMO_DISABLED_MESSAGE = (
    "External Microsoft 365 and Cowork actions are disabled in Riveter demo mode."
)
