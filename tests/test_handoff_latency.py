"""Handoff status must never block the task-detail request.

Phil: "switching between tasks in the left nav can be very slow."

Measured on the running server: the first /api/tasks/<id>/cowork call after
the 30s TTL expires took 6.9s and 7.2s on two different tasks, while every
call inside the TTL took ~20-50ms. The stall follows the cache clock, not the
task, so it is the handoff refetch and not rendering.

`GET /v1/tasks` pages up to 6 times over the network. handoff_status is
DECORATION on a card that is already complete without it, so paying for it
synchronously in the request path is the wrong trade: it makes a fast local
dashboard intermittently feel broken.

Contract: serve whatever is cached, even when stale, and refresh out of band.
"""

import threading
import time
import unittest

from src.services import cowork_runner as cr

CONV = "t:u:11111111-1111-1111-1111-111111111111"


def _task_list(state="completed"):
    return {"tasks": [{"taskId": CONV, "state": state}], "nextOffset": None}


class _SlowGet:
    """A /v1/tasks reader that takes a measurable amount of time."""

    def __init__(self, delay=0.4, state="completed"):
        self.delay = delay
        self.state = state
        self.calls = 0
        self.started = threading.Event()

    def __call__(self, path):
        self.calls += 1
        self.started.set()
        time.sleep(self.delay)
        payload = _task_list(self.state)

        class _R:
            def json(self_inner):
                return payload

        return _R()


class HandoffLatencyTest(unittest.TestCase):
    def setUp(self):
        cr.reset_handoff_cache()
        self.addCleanup(cr.reset_handoff_cache)

    def _warm(self, get, state="completed"):
        """Populate the cache the way a previous request would have."""
        with cr._handoff_lock:
            cr._handoff_cache["tasks"] = {CONV: {"taskId": CONV, "state": state}}
            cr._handoff_cache["at"] = time.monotonic()

    def test_a_stale_cache_is_served_without_waiting_for_the_network(self):
        get = _SlowGet(delay=0.5)
        self._warm(get)
        # Age the cache past the TTL.
        with cr._handoff_lock:
            cr._handoff_cache["at"] = time.monotonic() - (cr._HANDOFF_TTL + 5)

        start = time.monotonic()
        result = cr.handoff_status(CONV, _get=get)
        elapsed = time.monotonic() - start

        self.assertIsNotNone(result, "stale data is still worth showing")
        self.assertLess(
            elapsed, 0.2,
            "a stale cache must be served immediately, not after a refetch",
        )

    def test_the_stale_read_still_triggers_a_refresh(self):
        get = _SlowGet(delay=0.2, state="needs_user_input")
        self._warm(get, state="completed")
        with cr._handoff_lock:
            cr._handoff_cache["at"] = time.monotonic() - (cr._HANDOFF_TTL + 5)

        cr.handoff_status(CONV, _get=get)
        self.assertTrue(
            get.started.wait(timeout=5), "a stale read must kick off a refresh"
        )
        cr.wait_for_handoff_refresh(timeout=5)

        self.assertEqual(
            cr.handoff_status(CONV, _get=get)["state"], "needs_user_input",
            "the background refresh must actually land in the cache",
        )

    def test_concurrent_stale_reads_do_not_stampede(self):
        get = _SlowGet(delay=0.4)
        self._warm(get)
        with cr._handoff_lock:
            cr._handoff_cache["at"] = time.monotonic() - (cr._HANDOFF_TTL + 5)

        threads = [
            threading.Thread(target=cr.handoff_status, args=(CONV,), kwargs={"_get": get})
            for _ in range(6)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        cr.wait_for_handoff_refresh(timeout=5)

        self.assertEqual(get.calls, 1, "six stale reads must share one refresh")

    def test_a_cold_cache_does_not_block_either(self):
        """Nothing to serve is not a reason to make the user wait."""
        get = _SlowGet(delay=0.5)

        start = time.monotonic()
        result = cr.handoff_status(CONV, _get=get)
        elapsed = time.monotonic() - start

        self.assertIsNone(result, "no data yet means no badge")
        self.assertLess(elapsed, 0.2, "a cold cache must not block the request")

        cr.wait_for_handoff_refresh(timeout=5)
        self.assertIsNotNone(
            cr.handoff_status(CONV, _get=get),
            "the warmed cache should answer the next call",
        )

    def test_a_failing_refresh_leaves_the_previous_answer_intact(self):
        def boom(path):
            raise RuntimeError("throttled")

        self._warm(boom)
        with cr._handoff_lock:
            cr._handoff_cache["at"] = time.monotonic() - (cr._HANDOFF_TTL + 5)

        self.assertIsNotNone(cr.handoff_status(CONV, _get=boom))
        cr.wait_for_handoff_refresh(timeout=5)
        self.assertIsNotNone(
            cr.handoff_status(CONV, _get=boom),
            "a failed refresh must not wipe usable data",
        )


if __name__ == "__main__":
    unittest.main()
