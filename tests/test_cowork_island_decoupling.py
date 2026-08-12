"""The residual cowork_cli coupling, and why it is the biggest risk.

The API transport removes the dependency on the CLI *binary* but NOT on the
Python package: _default_island_probe and _cowork_whoami both import
cowork_cli.auth.manager. If that package moves or breaks, island resolution and
identity fail SILENTLY - the probe logs a warning and returns None, and
tenant_barrier_precheck degrades to "unknown", which also logs and proceeds.

The result is a baffling production failure rather than a clean error, which is
exactly what the architect named as the single biggest risk in this plan.

The island URL does not actually need the package. The CLI writes it as plain
JSON to %APPDATA%/cowork/routing_cache.json, which every API spike read
directly. Preferring that file removes a Python import from the critical path
and leaves the CLI probe as the fallback.
"""

import json
import unittest
from unittest import mock

from src.services import cowork_runner as cr


CACHE = {"entries": [{"result": {"endpoint": "https://island.example.com"}}]}


class TestIslandFromRoutingCache(unittest.TestCase):
    def setUp(self):
        cr.reset_registry()
        self.addCleanup(cr.reset_registry)

    def test_reads_the_endpoint_from_the_routing_cache(self):
        with mock.patch("pathlib.Path.exists", return_value=True), \
             mock.patch("pathlib.Path.read_text", return_value=json.dumps(CACHE)):
            self.assertEqual(
                cr._island_from_routing_cache(), "https://island.example.com"
            )

    def test_a_missing_cache_returns_none_rather_than_raising(self):
        with mock.patch("pathlib.Path.exists", return_value=False):
            self.assertIsNone(cr._island_from_routing_cache())

    def test_malformed_json_returns_none(self):
        with mock.patch("pathlib.Path.exists", return_value=True), \
             mock.patch("pathlib.Path.read_text", return_value="{not json"):
            self.assertIsNone(cr._island_from_routing_cache())

    def test_an_unexpected_shape_returns_none(self):
        with mock.patch("pathlib.Path.exists", return_value=True), \
             mock.patch("pathlib.Path.read_text", return_value='{"entries":[]}'):
            self.assertIsNone(cr._island_from_routing_cache())

    def test_the_cache_is_preferred_over_the_cli_probe(self):
        """The whole point: no cowork_cli import on the happy path."""
        probe_calls = []

        def probe():
            probe_calls.append(1)
            return "https://from-cli.example.com"

        with mock.patch.object(cr, "_default_island_probe", probe), \
             mock.patch.object(cr, "_island_from_routing_cache",
                               lambda: "https://island.example.com"):
            self.assertEqual(
                cr.resolve_cowork_island(), "https://island.example.com"
            )
        self.assertEqual(probe_calls, [])

    def test_falls_back_to_the_cli_probe_when_the_cache_is_unusable(self):
        with mock.patch.object(cr, "_default_island_probe",
                               lambda: "https://from-cli.example.com"), \
             mock.patch.object(cr, "_island_from_routing_cache", lambda: None):
            self.assertEqual(
                cr.resolve_cowork_island(), "https://from-cli.example.com"
            )

    def test_an_injected_probe_overrides_the_cache(self):
        """_ISLAND_PROBE_FN is an explicit override, so it wins outright -
        otherwise a test would silently read the real machine's cache."""
        with mock.patch.object(cr, "_ISLAND_PROBE_FN",
                               lambda: "https://injected.example.com"), \
             mock.patch.object(cr, "_island_from_routing_cache",
                               lambda: "https://island.example.com"):
            self.assertEqual(
                cr.resolve_cowork_island(), "https://injected.example.com"
            )

    def test_both_unavailable_still_returns_none_not_an_exception(self):
        with mock.patch.object(cr, "_default_island_probe", lambda: None), \
             mock.patch.object(cr, "_island_from_routing_cache", lambda: None):
            self.assertIsNone(cr.resolve_cowork_island())


class TestMissingPackageIsLoud(unittest.TestCase):
    """A missing cowork_cli must not read as "everything is fine"."""

    def test_the_precheck_reports_unknown_rather_than_ok(self):
        """It must never claim the barrier is safe when it could not look."""
        def boom():
            raise ImportError("No module named 'cowork_cli'")

        verdict = cr.tenant_barrier_precheck(_whoami=boom)
        self.assertEqual(verdict["status"], "unknown")
        self.assertNotEqual(verdict["status"], "ok")


if __name__ == "__main__":
    unittest.main()
