"""Test-wide isolation from the developer's real settings file.

This is the FOURTH time in one session that a unit test failed because it read
live local state instead of its own fixture:

1. the suite read the real settings.json and took the API transport path,
   turning one test file into a 275s run of live network calls;
2. tenant_barrier_precheck made a live network call per API test;
3. _ISLAND_PROBE_FN had to keep outright precedence or tests read the real
   routing cache;
4. adding `cowork_voice.default_channel: "teams"` broke two channel-inference
   tests that assert "nothing is inferred" - they were only passing because
   that key happened to be unset when they last ran.

Patching each test file as it breaks treats the symptom. A unit test must not
depend on whether a developer has enabled a feature on their own machine, so
the settings path is pointed at a location that does not exist for the whole
unit suite. `workspace_settings` already fails closed on a missing file, which
is exactly the shipped default we want tests to see.

Any test that wants specific settings mocks `_read_settings` itself (see
tests/test_voice_settings.py), which keeps the intent visible in the test.

Scoped to the unit suite. tests/e2e has its own conftest and drives a real
server, where the real settings file is the point.
"""

import pytest

from src.services import workspace_settings


@pytest.fixture(autouse=True)
def _isolate_workspace_settings(monkeypatch, tmp_path_factory):
    """Point settings at a path that does not exist, for every unit test."""
    missing = tmp_path_factory.mktemp("no-settings") / "settings.json"
    monkeypatch.setattr(workspace_settings, "SETTINGS_PATH", missing)
    yield
