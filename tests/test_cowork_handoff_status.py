"""Handoff status: what happened after "Open in Cowork".

Today handing a draft to the Cowork web app is fire-and-forget - TodoIQ never
learns whether Phil actually did anything with it. `GET /v1/tasks` is keyed by
the SAME composite conversation id our deep link already uses, so the card can
report back.

Verified read-only against production on 2026-08-10: 237 tasks visible across 5
pages, and 17 of our 18 stored conversation ids matched, carrying our real task
titles. Three states exist:

    running            Cowork is still working
    needs_user_input   Cowork is WAITING ON PHIL
    completed          finished

`needs_user_input` is the valuable one: it is how an approval prompt surfaces,
and it means TodoIQ can say "Cowork needs you" without owning an execute route
or ever sending anything itself.

This is purely ADDITIVE. It adds information to a card that works fine without
it, so it ships unflagged - failure degrades to exactly today's behaviour. That
is the same call already made for the /v1/cost badge.
"""

import unittest
from unittest import mock

from src.services.cowork_runner import (
    handoff_status,
    reset_handoff_cache,
)


def _task(cid, state="completed", title="A task", last=1786400663554):
    return {"taskId": cid, "state": state, "title": title, "lastActivity": last}


CID = "72f988bf:08b7be88:cw-42cef5df"


def _get(tasks, next_offset=None):
    """Fake the paged GET /v1/tasks response."""
    body = {"tasks": tasks}
    if next_offset:
        body["nextOffset"] = next_offset
    return mock.Mock(json=mock.Mock(return_value=body))


class HandoffTestBase(unittest.TestCase):
    def setUp(self):
        reset_handoff_cache()
        self.addCleanup(reset_handoff_cache)


class TestReadsState(HandoffTestBase):
    def test_returns_the_state_for_a_known_conversation(self):
        got = handoff_status(CID, _get=lambda p: _get([_task(CID, "running")]))
        self.assertEqual(got["state"], "running")

    def test_surfaces_needs_user_input(self):
        """The state worth building a UI around."""
        got = handoff_status(
            CID, _get=lambda p: _get([_task(CID, "needs_user_input")])
        )
        self.assertEqual(got["state"], "needs_user_input")
        self.assertTrue(got["waiting_on_user"])

    def test_completed_is_not_waiting_on_user(self):
        got = handoff_status(CID, _get=lambda p: _get([_task(CID, "completed")]))
        self.assertFalse(got["waiting_on_user"])

    def test_carries_last_activity(self):
        got = handoff_status(CID, _get=lambda p: _get([_task(CID, "running")]))
        self.assertEqual(got["last_activity"], 1786400663554)

    def test_unknown_conversation_returns_none(self):
        got = handoff_status(CID, _get=lambda p: _get([_task("someone-else")]))
        self.assertIsNone(got)

    def test_blank_conversation_id_does_not_call_the_network(self):
        called = []

        def spy(path):
            called.append(path)
            return _get([])

        self.assertIsNone(handoff_status("", _get=spy))
        self.assertEqual(called, [])


class TestFailsSoft(HandoffTestBase):
    """Decoration must never be able to break a card."""

    def test_network_error_returns_none(self):
        def boom(path):
            raise OSError("no route to host")

        self.assertIsNone(handoff_status(CID, _get=boom))

    def test_malformed_payload_returns_none(self):
        bad = mock.Mock(json=mock.Mock(return_value={"tasks": "not a list"}))
        self.assertIsNone(handoff_status(CID, _get=lambda p: bad))

    def test_non_dict_task_entries_are_skipped(self):
        got = handoff_status(
            CID, _get=lambda p: _get(["nope", None, _task(CID, "running")])
        )
        self.assertEqual(got["state"], "running")


class TestPaging(HandoffTestBase):
    def test_follows_next_offset_to_find_a_later_conversation(self):
        """Ours was on page 3 of 5 in the real capture."""
        pages = [
            _get([_task("other-1")], next_offset="50"),
            _get([_task("other-2")], next_offset="100"),
            _get([_task(CID, "running")]),
        ]
        calls = []

        def paged(path):
            calls.append(path)
            return pages[len(calls) - 1]

        got = handoff_status(CID, _get=paged)
        self.assertEqual(got["state"], "running")
        self.assertEqual(len(calls), 3)

    def test_paging_is_bounded(self):
        """A cursor that never terminates must not spin forever."""
        calls = []

        def endless(path):
            calls.append(path)
            return _get([_task(f"other-{len(calls)}")], next_offset=str(len(calls)))

        self.assertIsNone(handoff_status(CID, _get=endless))
        self.assertLessEqual(len(calls), 6)


class TestCaching(HandoffTestBase):
    """A dashboard poll must not mean a network round trip per card.

    A real cost_snapshot() call in the hot path once took the unit suite from
    35s to 313s. The same discipline applies here.
    """

    def test_a_second_lookup_reuses_the_first_fetch(self):
        calls = []

        def spy(path):
            calls.append(path)
            return _get([_task(CID, "running")])

        handoff_status(CID, _get=spy)
        handoff_status(CID, _get=spy)
        self.assertEqual(len(calls), 1)

    def test_two_conversations_share_one_fetch(self):
        other = "72f988bf:08b7be88:cw-b91921ef"
        calls = []

        def spy(path):
            calls.append(path)
            return _get([_task(CID, "running"), _task(other, "completed")])

        self.assertEqual(handoff_status(CID, _get=spy)["state"], "running")
        self.assertEqual(handoff_status(other, _get=spy)["state"], "completed")
        self.assertEqual(len(calls), 1)

    def test_reset_clears_the_cache(self):
        calls = []

        def spy(path):
            calls.append(path)
            return _get([_task(CID, "running")])

        handoff_status(CID, _get=spy)
        reset_handoff_cache()
        handoff_status(CID, _get=spy)
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
