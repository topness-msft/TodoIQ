"""What a preview cost, measured rather than estimated.

THE HISTORY
-----------
`cw-cost-display` sat blocked for weeks on "there is no cost signal". That was
checked against the CLI: no `cowork cost` subcommand, no `/cost` slash command,
and nothing cost-shaped anywhere in the `--json` payload. All true, and all the
wrong place to look.

Repo access turned up `GET /v1/cost` in the runtime
(`aether_runtime/src/orchestrator/api/v1/cost.py`), a read proxy over Neptune's
costing API. Verified live:

    {"user": {"consumed": 14272.022146, "limit": -1.0},
     "policy": {"policyName": "All Users Policy", ...},
     "asOfDate": "...", "resetOn": "2026-09-01T00:00:00Z"}

It is month-to-date, not per-call. But it is a monotonic counter with no drift
when nothing is running (three reads, zero movement), and it updates
immediately: a trivial 12.6s turn with no tools moved it by 30.23. So the cost
of one preview is the difference across it.

No transport migration is needed for this. `SessionManager.sync_get` reaches any
runtime endpoint, which is the same targeted use of the library as the tenant
precheck already in production.

HONESTY CONSTRAINTS
-------------------
The counter is per USER, not per run. Two previews overlapping means neither
delta is attributable, so we report nothing rather than a wrong number.

The endpoint has a documented kill switch (an ECS flight can disable it, giving
404) and can return 503 when Neptune is throttled. Cost is decoration; a preview
must never fail because of it.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.services.cowork_runner as cr  # noqa: E402


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


class TestSnapshot(unittest.TestCase):
    def test_reads_the_user_consumed_value(self):
        got = cr.cost_snapshot(
            _get=lambda p: _Resp({"user": {"consumed": 14272.02, "limit": -1.0}})
        )
        self.assertAlmostEqual(got, 14272.02)

    def test_it_asks_the_versioned_path(self):
        """Bare /cost 404s; only /v1/cost is routed."""
        seen = []

        def _get(path):
            seen.append(path)
            return _Resp({"user": {"consumed": 1.0}})

        cr.cost_snapshot(_get=_get)
        self.assertEqual(seen, ["/v1/cost"])

    def test_a_missing_user_block_is_not_a_crash(self):
        self.assertIsNone(cr.cost_snapshot(_get=lambda p: _Resp({"policy": {}})))

    def test_a_disabled_endpoint_returns_none(self):
        """404 is documented: an ECS rule can kill-switch this per tenant."""
        def _get(path):
            raise RuntimeError("404 Not Found")

        self.assertIsNone(cr.cost_snapshot(_get=_get))

    def test_a_throttled_upstream_returns_none(self):
        def _get(path):
            raise RuntimeError("503 Service Unavailable")

        self.assertIsNone(cr.cost_snapshot(_get=_get))

    def test_it_never_raises(self):
        """Cost is decoration. It must not be able to fail a preview."""
        for boom in (ValueError, KeyError, TimeoutError, RuntimeError):
            def _get(path, exc=boom):
                raise exc("nope")

            self.assertIsNone(cr.cost_snapshot(_get=_get))

    def test_a_non_numeric_value_is_rejected(self):
        self.assertIsNone(
            cr.cost_snapshot(_get=lambda p: _Resp({"user": {"consumed": "lots"}}))
        )


class TestDelta(unittest.TestCase):
    def test_a_normal_delta(self):
        self.assertAlmostEqual(cr.cost_delta(14272.02, 14302.25), 30.23, places=2)

    def test_a_missing_endpoint_yields_no_number(self):
        self.assertIsNone(cr.cost_delta(None, 14302.25))
        self.assertIsNone(cr.cost_delta(14272.02, None))
        self.assertIsNone(cr.cost_delta(None, None))

    def test_a_counter_that_went_backwards_is_rejected(self):
        """Monthly reset (resetOn) or a Neptune correction. Not our spend."""
        self.assertIsNone(cr.cost_delta(14302.25, 14272.02))

    def test_zero_is_a_real_answer_not_a_missing_one(self):
        """A cached turn can legitimately cost nothing."""
        self.assertEqual(cr.cost_delta(100.0, 100.0), 0.0)

    def test_an_implausible_jump_is_rejected(self):
        """A month's spend cannot land on one preview. Something else moved the
        counter, most likely another client on the same account."""
        self.assertIsNone(cr.cost_delta(100.0, 100.0 + cr._COST_SANITY_CEILING + 1))


class TestConcurrencyHonesty(unittest.TestCase):
    """The counter is per USER. Overlapping previews cannot be told apart, so we
    decline to attribute rather than show a wrong number."""

    def setUp(self):
        cr.reset_registry()

    def tearDown(self):
        cr.reset_registry()

    def test_a_solo_run_is_attributable(self):
        self.assertTrue(cr.cost_is_attributable(concurrent_runs=1))

    def test_an_overlapping_run_is_not(self):
        self.assertFalse(cr.cost_is_attributable(concurrent_runs=2))

    def test_zero_is_treated_as_solo(self):
        """Defensive: the caller counts itself, but never trust that."""
        self.assertTrue(cr.cost_is_attributable(concurrent_runs=0))


class TestFormatting(unittest.TestCase):
    """What a user actually reads."""

    def test_a_normal_cost(self):
        self.assertEqual(cr.format_cost(30.231125), "30.2 credits")

    def test_a_small_cost_keeps_a_decimal(self):
        self.assertEqual(cr.format_cost(0.4), "0.4 credits")

    def test_a_large_cost_drops_the_decimal(self):
        self.assertEqual(cr.format_cost(1234.56), "1,235 credits")

    def test_one_credit_is_singular(self):
        self.assertEqual(cr.format_cost(1.0), "1.0 credits")

    def test_zero_is_stated_plainly(self):
        self.assertEqual(cr.format_cost(0.0), "no credits")

    def test_nothing_to_show_is_empty(self):
        self.assertEqual(cr.format_cost(None), "")


if __name__ == "__main__":
    unittest.main()
