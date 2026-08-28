"""Where a task came from, in a form you can re-open.

`tasks.source_id` reads like a locator and is not one. It is a dedup key built
as `{type}::{person}::{subject_first_50}` (.claude/commands/todo-refresh.md:102-110),
so two different threads about the same subject collide by design and no thread
can be re-opened from it. The only re-usable identifier we store today is
`source_url`, captured opportunistically.

This module gives that identifier a name and a shape, so a check can ask "can I
re-read the origin of this task?" and get an honest answer instead of a regex
guess at the call site.

Deliberately NOT solved here, because the spikes are open: an Outlook URL does
not yet map to a Graph message id, and a meeting URL does not yield a Calendar
event id. Those fields exist in the shape and stay null - a null placeholder is
a promise about layout, whereas a populated guess would be a claim we cannot
support.
"""

import json
import unittest

from src.services import source_locator as sl


ONE_TO_ONE = (
    "https://teams.microsoft.com/l/message/"
    "19:08b7be88-37ac-4e2b-82af-f8bb67e5f2f7_db4dc955-ec8f-449f-92c7-1ee80f3feeba"
    "@unq.gbl.spaces/1756000000000"
)
GROUP = (
    "https://teams.microsoft.com/l/message/"
    "19:2ff5b5b3ca2d44e1bd3a32eba70c7a31@thread.v2/1756000000000"
)
OUTLOOK = "https://outlook.office365.com/mail/inbox/id/AAQkAG..."
OUTLOOK_ITEM = (
    "https://outlook.office365.com/owa/?ItemID=AAMkADFkODcy"
    "ODkwLTE0MjItNDVmOC05Yjk4LWYzYjRkMWNjMWRjOABGAAAAAAAbRXCQ%2brCcTGYSHAAWFAKN7AAA%3d"
    "&exvsurl=1&viewmodel=ReadMessageItem"
)
MEETING_DETAILS = (
    "https://teams.microsoft.com/l/meeting/details?eventId=AAMkADFkODcyODkw"
    "LTE0MjItNDVmOC05Yjk4LWYzYjRkMWNjMWRjOABGAAAAAACqtvZcafJO%3d"
)


class TestIdentifiersProvenAgainstGraph(unittest.TestCase):
    """Email and meeting links DO carry re-openable ids.

    Both were first recorded as unresolved spikes and their fields left null.
    Probing live Graph through WorkIQ on 2026-08-24 settled both, so leaving
    them null stopped being caution and became wrong:

      Outlook `?ItemID=` IS a Graph message id.
        GET /me/messages/{ItemID} -> 200, carrying conversationId.
        GET /me/messages?$filter=conversationId eq '...' -> 200, whole thread.
        Combining that filter with $orderby is rejected: InefficientFilter.

      Teams `/l/meeting/details?eventId=` IS a Graph event id.
        GET /me/events/{eventId} -> 200, carrying onlineMeeting.joinUrl,
        which embeds 19:meeting_...@thread.v2.
        GET /me/chats/{that id}/messages -> 200, real messages.

    Measured on the live database: 87 Outlook and 300 meeting URLs carry one of
    these and were being discarded.
    """

    def test_an_outlook_item_id_is_captured_as_an_email_locator(self):
        got = sl.from_source_url(OUTLOOK_ITEM)
        self.assertEqual(got["kind"], sl.KIND_EMAIL)
        self.assertTrue(got["message_id"].startswith("AAMkADFk"))

    def test_the_item_id_is_url_decoded(self):
        # %3d must become '=' or Graph rejects the id.
        self.assertTrue(sl.from_source_url(OUTLOOK_ITEM)["message_id"].endswith("="))

    def test_a_meeting_details_link_is_captured_as_an_event(self):
        got = sl.from_source_url(MEETING_DETAILS)
        self.assertEqual(got["kind"], sl.KIND_MEETING)
        self.assertTrue(got["event_id"].startswith("AAMkADFk"))

    def test_an_unrelated_event_id_query_is_not_a_meeting(self):
        self.assertIsNone(
            sl.from_source_url(
                "https://example.com/page?eventId=not-a-meeting"
            )
        )

    def test_an_outlook_url_without_an_item_id_still_yields_nothing(self):
        self.assertIsNone(sl.from_source_url(OUTLOOK))


class TestReadPlan(unittest.TestCase):
    """The endpoint sequence, owned here rather than re-derived per prompt.

    A worker that is not shown an endpoint does not invent one - that is what
    3b5e16d fixed for Teams delivery, and what a live run repeated for the
    thread read. So the proven sequence lives in one place.
    """

    def test_nothing_to_read_without_a_locator(self):
        self.assertEqual(sl.read_plan(None), [])

    def test_a_chat_is_one_hop(self):
        plan = sl.read_plan(sl.normalise(
            {"kind": "teams_chat", "conversation_id": "19:a@thread.v2"}))
        self.assertEqual(len(plan), 1)
        self.assertIn("/me/chats/19:a@thread.v2/messages", plan[0])

    def test_a_channel_reads_the_reply_thread(self):
        plan = sl.read_plan(sl.normalise(
            {"kind": "teams_channel", "team_id": "t", "channel_id": "c",
             "message_id": "1"}))
        self.assertEqual(len(plan), 1)
        self.assertIn("/teams/t/channels/c/messages/1/replies", plan[0])

    def test_an_email_is_two_hops_ending_in_the_conversation(self):
        plan = sl.read_plan(sl.normalise(
            {"kind": "email", "message_id": "AAMk123="}))
        self.assertEqual(len(plan), 2)
        self.assertIn("/me/messages/AAMk123=", plan[0])
        self.assertIn("conversationId", plan[1])

    def test_the_email_plan_never_adds_an_orderby(self):
        # $filter=conversationId with $orderby is rejected by Graph as
        # InefficientFilter; measured 2026-08-24.
        plan = sl.read_plan(sl.normalise({"kind": "email", "message_id": "AAMk123="}))
        self.assertNotIn("$orderby", " ".join(plan))

    def test_a_meeting_is_two_hops_via_the_join_url(self):
        plan = sl.read_plan(sl.normalise(
            {"kind": "meeting", "event_id": "AAMkEvent="}))
        self.assertEqual(len(plan), 2)
        self.assertIn("/me/events/AAMkEvent=", plan[0])
        self.assertIn("onlineMeeting", plan[0])
        self.assertIn("/me/chats/", plan[1])

    def test_a_meeting_known_only_by_its_chat_reads_that_chat_directly(self):
        plan = sl.read_plan(sl.normalise(
            {"kind": "meeting", "conversation_id": "19:meeting_x@thread.v2"}))
        self.assertEqual(len(plan), 1)
        self.assertIn("/me/chats/19:meeting_x@thread.v2/messages", plan[0])


class TestDerivingFromASourceUrl(unittest.TestCase):
    def test_a_one_to_one_chat_yields_a_readable_conversation(self):
        got = sl.from_source_url(ONE_TO_ONE)
        self.assertEqual(got["kind"], sl.KIND_TEAMS_CHAT)
        self.assertTrue(got["conversation_id"].startswith("19:"))
        self.assertTrue(sl.is_thread_readable(got))

    def test_a_group_chat_yields_a_readable_conversation(self):
        got = sl.from_source_url(GROUP)
        self.assertEqual(got["kind"], sl.KIND_TEAMS_CHAT)
        self.assertEqual(got["conversation_id"], "19:2ff5b5b3ca2d44e1bd3a32eba70c7a31@thread.v2")

    def test_the_linked_message_is_kept(self):
        # The message id is what makes "since this message" possible later.
        self.assertEqual(sl.from_source_url(ONE_TO_ONE)["message_id"], "1756000000000")

    def test_a_derived_locator_says_it_was_derived(self):
        # Not "captured": nobody recorded this, it was recovered from a URL.
        self.assertEqual(sl.from_source_url(GROUP)["source"], sl.SOURCE_DERIVED)

    def test_an_outlook_url_yields_nothing_rather_than_a_guess(self):
        # Whether an Outlook URL maps to a Graph message id is an open spike.
        # Returning a locator here would be inventing the answer.
        self.assertIsNone(sl.from_source_url(OUTLOOK))

    def test_absent_and_junk_urls_yield_nothing(self):
        for url in (None, "", "   ", "not a url", "https://example.com/x"):
            with self.subTest(url=url):
                self.assertIsNone(sl.from_source_url(url))

    def test_a_chat_locator_carries_no_email_or_event_identifiers(self):
        # Not because they are unknowable - both were later proven reachable -
        # but because a Teams chat link simply has neither.
        got = sl.from_source_url(ONE_TO_ONE)
        self.assertIn("internet_message_id", got)
        self.assertIn("event_id", got)
        self.assertIsNone(got["internet_message_id"])
        self.assertIsNone(got["event_id"])


class TestTheStoredShape(unittest.TestCase):
    def test_none_and_junk_normalise_to_absent(self):
        for raw in (None, "", "   ", "{not json", "[]", '"s"', "42"):
            with self.subTest(raw=raw):
                self.assertIsNone(sl.normalise(raw))

    def test_a_stored_locator_round_trips(self):
        stored = json.dumps({
            "version": 1, "kind": "teams_chat",
            "conversation_id": "19:abc@thread.v2", "message_id": "175",
            "source": "captured",
        })
        got = sl.normalise(stored)
        self.assertEqual(got["kind"], sl.KIND_TEAMS_CHAT)
        self.assertEqual(got["conversation_id"], "19:abc@thread.v2")
        self.assertEqual(got["source"], sl.SOURCE_CAPTURED)

    def test_every_locator_has_a_stable_key_set(self):
        # Readers destructure this unconditionally.
        self.assertEqual(
            set(sl.normalise({"kind": "teams_chat", "conversation_id": "19:a"})),
            {"version", "kind", "conversation_id", "message_id", "team_id",
             "channel_id", "internet_message_id", "event_id", "source"},
        )

    def test_an_unknown_kind_is_refused_rather_than_stored(self):
        self.assertIsNone(sl.normalise({"kind": "telepathy", "conversation_id": "19:a"}))

    def test_a_teams_chat_without_a_conversation_is_not_a_locator(self):
        # The whole point of the record is the id. Without it there is nothing
        # to re-open, and keeping it would let a caller believe otherwise.
        self.assertIsNone(sl.normalise({"kind": "teams_chat", "conversation_id": None}))

    def test_a_channel_needs_the_full_triple(self):
        partial = {"kind": "teams_channel", "team_id": "t", "channel_id": None,
                   "message_id": "1"}
        self.assertIsNone(sl.normalise(partial))
        full = {"kind": "teams_channel", "team_id": "t", "channel_id": "c",
                "message_id": "1"}
        self.assertIsNotNone(sl.normalise(full))

    def test_an_unrecognised_source_falls_back_to_derived(self):
        # "captured" is the stronger claim, so it is never the fallback.
        got = sl.normalise({"kind": "teams_chat", "conversation_id": "19:a",
                            "source": "wishful"})
        self.assertEqual(got["source"], sl.SOURCE_DERIVED)


class TestReadability(unittest.TestCase):
    def test_absent_is_not_readable(self):
        self.assertFalse(sl.is_thread_readable(None))

    def test_a_chat_with_a_conversation_is_readable(self):
        self.assertTrue(sl.is_thread_readable(
            sl.normalise({"kind": "teams_chat", "conversation_id": "19:a"})))

    def test_a_channel_with_its_triple_is_readable(self):
        self.assertTrue(sl.is_thread_readable(sl.normalise(
            {"kind": "teams_channel", "team_id": "t", "channel_id": "c",
             "message_id": "1"})))

    def test_a_meeting_is_readable_through_its_chat(self):
        # Originally recorded as unreadable, on the assumption that a meeting
        # link gave no way through to a conversation. Probing Graph showed
        # otherwise: the event carries onlineMeeting.joinUrl, which embeds the
        # meeting chat thread. Two hops, but proven, so it is readable.
        got = sl.normalise({"kind": "meeting", "conversation_id": "19:m@thread.v2"})
        self.assertIsNotNone(got)
        self.assertTrue(sl.is_thread_readable(got))

    def test_a_meeting_known_only_by_its_event_is_readable(self):
        self.assertTrue(sl.is_thread_readable(
            sl.normalise({"kind": "meeting", "event_id": "AAMkEvent="})))

    def test_an_email_with_a_message_id_is_readable(self):
        self.assertTrue(sl.is_thread_readable(
            sl.normalise({"kind": "email", "message_id": "AAMk123="})))

    def test_readable_means_exactly_that_there_is_a_way_to_read_it(self):
        # One definition, so a caller cannot be told "readable" and then handed
        # an empty plan.
        for payload in (
            {"kind": "teams_chat", "conversation_id": "19:a"},
            {"kind": "teams_channel", "team_id": "t", "channel_id": "c", "message_id": "1"},
            {"kind": "email", "message_id": "AAMk="},
            {"kind": "meeting", "event_id": "AAMkE="},
        ):
            with self.subTest(kind=payload["kind"]):
                located = sl.normalise(payload)
                self.assertEqual(
                    sl.is_thread_readable(located), bool(sl.read_plan(located)))


class TestProducerReaderContract(unittest.TestCase):
    """The shape /todo-refresh writes inline from bash must be readable here.

    Same rule as waiting_activity: the command cannot import this module, so a
    test is the only thing holding writer and reader together.
    """

    def test_the_captured_chat_payload_round_trips(self):
        written = json.dumps({
            "version": 1, "kind": "teams_chat",
            "conversation_id": "19:08b7be88_db4dc955@unq.gbl.spaces",
            "message_id": "1756000000000",
            "team_id": None, "channel_id": None,
            "internet_message_id": None, "event_id": None,
            "source": "captured",
        })
        got = sl.normalise(written)
        self.assertEqual(got["source"], sl.SOURCE_CAPTURED)
        self.assertTrue(sl.is_thread_readable(got))

    def test_the_captured_channel_payload_round_trips(self):
        written = json.dumps({
            "version": 1, "kind": "teams_channel",
            "conversation_id": None, "message_id": "1756000000000",
            "team_id": "19:team@thread.tacv2", "channel_id": "19:chan@thread.tacv2",
            "internet_message_id": None, "event_id": None,
            "source": "captured",
        })
        got = sl.normalise(written)
        self.assertTrue(sl.is_thread_readable(got))
        self.assertEqual(got["team_id"], "19:team@thread.tacv2")


if __name__ == "__main__":
    unittest.main()
