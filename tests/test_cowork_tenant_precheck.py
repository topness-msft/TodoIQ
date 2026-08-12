"""Check the barrier's precondition BEFORE running, not after.

The canary shipped in 4e6387b is reactive: it inspects a finished run and says
whether interception was observed. By then any write has already happened.

Reading the server source gave us the precondition directly:

    # aether_runtime/src/orchestrator/api/v1/tool_callback.py
    if tenant_id not in EVAL_ALLOWED_TENANTS:
        raise HTTPException(status_code=404, detail="Not found")

    # aether_runtime/src/orchestrator/domain/eval/auth.py
    EVAL_ALLOWED_TENANTS = SYNTHETIC_EVAL_TENANTS | frozenset({
        "72f988bf-86f1-41af-91ab-2d7cd011db47",  # Microsoft (dogfood)
        ...
    })

And the CLI, imported as a library, will tell us which tenant we are on:

    AuthManager(get_settings()).whoami().tenant_id
    -> '72f988bf-86f1-41af-91ab-2d7cd011db47'

So we can compare the two before spending 60 seconds on a run that might write
for real. That is the difference between "a write may have happened" and "we
never started".

Deliberately advisory, never blocking. The allowlist is read from server source
we do not control and cannot query, so treating a mismatch as fatal would strand
the user the first time upstream adds a tenant we have not copied. It warns; the
reactive canary still backstops the actual run.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services.cowork_runner import (  # noqa: E402
    EVAL_ALLOWED_TENANTS,
    tenant_barrier_precheck,
    warm_barrier_precheck,
)
import src.services.cowork_runner as _runner  # noqa: E402

MSFT = "72f988bf-86f1-41af-91ab-2d7cd011db47"


class TestKnownAllowlist(unittest.TestCase):
    """Transcribed from aether_runtime/src/orchestrator/domain/eval/auth.py."""

    def test_microsoft_tenant_is_listed(self):
        self.assertIn(MSFT, EVAL_ALLOWED_TENANTS)

    def test_synthetic_eval_tenants_are_included(self):
        # coworkevals + DeepWorkAgent SEVAL + InceptionBench
        self.assertIn("258e9af2-1c09-4fbd-9b9c-a1f08bda4697", EVAL_ALLOWED_TENANTS)

    def test_the_msa_consumer_pseudo_tenant_is_NOT_listed(self):
        """Upstream #18550: this is the tenant where the gate drops the config
        and real Graph writes execute. If it ever appears here, our copy of the
        allowlist has been edited wrongly."""
        self.assertNotIn("9188040d-6c67-4c5b-b112-36a304b66dad", EVAL_ALLOWED_TENANTS)


class TestPrecheckVerdict(unittest.TestCase):
    def _who(self, tenant):
        who = mock.Mock()
        who.tenant_id = tenant
        who.username = "phtopnes@microsoft.com"
        return who

    def test_allowlisted_tenant_is_ok(self):
        r = tenant_barrier_precheck(_whoami=lambda: self._who(MSFT))
        self.assertEqual(r["status"], "ok")

    def test_unlisted_tenant_warns(self):
        r = tenant_barrier_precheck(
            _whoami=lambda: self._who("9188040d-6c67-4c5b-b112-36a304b66dad")
        )
        self.assertEqual(r["status"], "AT_RISK")

    def test_at_risk_reason_names_the_tenant_and_the_consequence(self):
        r = tenant_barrier_precheck(_whoami=lambda: self._who("deadbeef"))
        self.assertIn("deadbeef", r["reason"])
        self.assertIn("18550", r["reason"])

    def test_unknown_when_identity_is_unavailable(self):
        """Never claim safety we could not verify."""
        def boom():
            raise RuntimeError("not signed in")

        r = tenant_barrier_precheck(_whoami=boom)
        self.assertEqual(r["status"], "unknown")

    def test_unknown_when_tenant_is_blank(self):
        r = tenant_barrier_precheck(_whoami=lambda: self._who(""))
        self.assertEqual(r["status"], "unknown")

    def test_verdict_always_carries_status_and_reason(self):
        for who in (lambda: self._who(MSFT), lambda: self._who("x")):
            r = tenant_barrier_precheck(_whoami=who)
            self.assertIn(r["status"], {"ok", "AT_RISK", "unknown"})
            self.assertTrue(r["reason"])


class TestIsAdvisoryNotBlocking(unittest.TestCase):
    """A mismatch must not strand the user. Our allowlist copy can go stale the
    moment upstream adds a tenant, and we cannot query the real one."""

    def test_precheck_never_raises(self):
        def boom():
            raise ValueError("anything")

        tenant_barrier_precheck(_whoami=boom)  # must not raise

    def test_at_risk_is_not_an_error_field(self):
        r = tenant_barrier_precheck(_whoami=lambda: None)
        self.assertNotIn("error", r)


class TestAgainstTheRealSignedInIdentity(unittest.TestCase):
    """Uses the actual CLI token store. Skips when not signed in, so CI without
    Cowork auth stays green."""

    def test_real_identity_resolves_to_an_allowlisted_tenant(self):
        try:
            from cowork_cli.auth.manager import AuthManager
            from cowork_cli.config.settings import get_settings

            auth = AuthManager(get_settings())
            if not auth.is_authenticated():
                self.skipTest("Cowork not authenticated")
            tenant = auth.whoami().tenant_id
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"Cowork CLI unavailable: {exc}")

        self.assertIn(
            tenant,
            EVAL_ALLOWED_TENANTS,
            "The signed-in tenant is no longer on our copy of "
            "EVAL_ALLOWED_TENANTS. Either upstream changed the gate, or this "
            "machine signed in elsewhere. The write barrier may be inert.",
        )


if __name__ == "__main__":
    unittest.main()


class TestCachingKeepsItOffTheRequestPath(unittest.TestCase):
    """Cold cost is ~5.5s (CLI import + MSAL silent refresh), warm is ~7ms.
    start_preview runs on a request path, so it must never pay the cold cost."""

    def setUp(self):
        self._saved = dict(_runner._precheck_cache)
        _runner._precheck_cache["verdict"] = None
        _runner._precheck_cache["at"] = 0.0

    def tearDown(self):
        _runner._precheck_cache.update(self._saved)

    def test_cached_call_does_not_reresolve_identity(self):
        calls = []

        def who():
            calls.append(1)
            m = mock.Mock()
            m.tenant_id = MSFT
            return m

        tenant_barrier_precheck(_whoami=who, use_cache=True)
        tenant_barrier_precheck(_whoami=who, use_cache=True)
        tenant_barrier_precheck(_whoami=who, use_cache=True)
        self.assertEqual(len(calls), 1, "identity resolved more than once")

    def test_uncached_call_always_reresolves(self):
        """The live path stays honest; only request-path callers opt in."""
        calls = []

        def who():
            calls.append(1)
            m = mock.Mock()
            m.tenant_id = MSFT
            return m

        tenant_barrier_precheck(_whoami=who)
        tenant_barrier_precheck(_whoami=who)
        self.assertEqual(len(calls), 2)

    def test_warm_populates_the_cache(self):
        warm_barrier_precheck()
        self.assertIsNotNone(_runner._precheck_cache["verdict"])

    def test_warm_never_raises_even_if_auth_is_broken(self):
        with mock.patch.object(
            _runner, "_cowork_whoami", side_effect=RuntimeError("boom")
        ):
            warm_barrier_precheck()

    def test_an_unknown_verdict_is_not_cached_as_truth(self):
        """A transient auth failure must not pin 'unknown' for 10 minutes."""
        def boom():
            raise RuntimeError("transient")

        tenant_barrier_precheck(_whoami=boom, use_cache=True)
        self.assertIsNone(_runner._precheck_cache["verdict"])
