"""Tests for Teams/Outlook source URL parsing (F9 wrong-audience protection).

Every URL in this file is a REAL url taken from the live database (GUIDs are real
tenant object IDs, which are not secrets). Audience misclassification is the
highest-consequence bug in the Cowork action layer: replying to what looks like a
1:1 but is actually a channel broadcasts to a whole team.

Live distribution at time of writing (1132 Teams message links):
    629  @unq.gbl.spaces   -> 1:1
    390  @thread.v2        -> group
     94  meeting_*         -> meeting
     14  @thread.skype     -> channel
      4  @thread.tacv2     -> group
Only 44% are 1:1, so the group/meeting/channel paths are the common case.
"""

import unittest
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from src.services.cowork_runner import parse_source_url

PHIL = "08b7be88-37ac-4e2b-82af-f8bb67e5f2f7"

# Real 1:1 chat links (Phase 1 targets #2076, #2057, #2100)
URL_1TO1_2076 = (
    "https://teams.microsoft.com/l/message/"
    "19:007b4f8b-2585-442b-91d9-581972e27761_08b7be88-37ac-4e2b-82af-f8bb67e5f2f7"
    "@unq.gbl.spaces/1785358519108?context=%7B%22contextType%22:%22chat%22%7D"
)
URL_1TO1_2057 = (
    "https://teams.microsoft.com/l/message/"
    "19:08b7be88-37ac-4e2b-82af-f8bb67e5f2f7_db4dc955-ec8f-449f-92c7-1ee80f3feeba"
    "@unq.gbl.spaces/1784831900904?context=%7B%22contextType%22:%22chat%22%7D"
)
URL_1TO1_UNDASHED = (
    "https://teams.microsoft.com/l/message/"
    "19:08b7be88-37ac-4e2b-82af-f8bb67e5f2f7_fe1c66c549f449abb2750c1eb2d2cbf6"
    "@unq.gbl.spaces/1772216063082?context=%7B%22contextType%22:%22chat%22%7D"
)
URL_1TO1_UNDASHED_FIRST = (
    "https://teams.microsoft.com/l/message/"
    "19:02b4cffd3e93446191c40ef3789bcb3e_08b7be88-37ac-4e2b-82af-f8bb67e5f2f7"
    "@unq.gbl.spaces/1772582489267?context=%7B%22contextType%22:%22chat%22%7D"
)
URL_1TO1_CHAT = (
    "https://teams.microsoft.com/l/chat/"
    "19:08b7be88-37ac-4e2b-82af-f8bb67e5f2f7_d87be1b4-816c-4eff-9edd-7a5823986db1"
    "@unq.gbl.spaces/conversations?context=%7B%22contextType%22:%22chat%22%7D"
)
URL_GROUP = (
    "https://teams.microsoft.com/l/message/"
    "19:723efdcdeef840a983dcc68779914cbb@thread.v2/1771978315725"
)
URL_MEETING = (
    "https://teams.microsoft.com/l/message/"
    "19:meeting_MjQ4ZWEzMWUtYjhlMi00ODYzLWIxZTctZmVhNDRjOWY0YjE4"
    "@thread.v2/1771972842751"
)
URL_CHANNEL = (
    "https://teams.microsoft.com/l/message/"
    "19:kpVc_JKmRAY_zandEVrXjn3ZSZt1oWT9B1o_K5ifhC41@thread.skype/1771911643376"
    "?tenantId=72f988bf-86f1-41af-91ab-2d7cd011db47"
    "&groupId=b9e6f984-de27-4110-9546-1a4b0e0b2f5a"
)
URL_TACV2 = (
    "https://teams.microsoft.com/l/message/"
    "19:aebe4a1ba8d9464a9b1d0b1c5c2f8f0a@thread.tacv2/1772001111222"
)
URL_OUTLOOK = (
    "https://outlook.office365.com/owa/?ItemID=AAMkADFkODcyODkwLTE0MjItNDVmOC05Yjk"
    "4LWYzYjRkMWNjMWRjOABGAAAAAACqtvZcafJOQpm3UWtumEp1&exvsurl=1"
)
URL_SHAREPOINT = (
    "https://microsoft-my.sharepoint-df.com/personal/aamerkal_microsoft_com1/"
    "Documents/Recordings/PhilAamer%201x1-20260224_201247UTC-Meeting%20Recording.mp4"
)
URL_MEETING_DETAILS = (
    "https://teams.microsoft.com/l/meeting/details?eventId=AAMkADFkODcyODkwLTE0MjIt"
)


class TestAudienceClassification(unittest.TestCase):
    """kind must never under-report the size of the audience."""

    def test_one_to_one_chat(self):
        for url in (URL_1TO1_2076, URL_1TO1_2057, URL_1TO1_CHAT):
            with self.subTest(url=url[:70]):
                self.assertEqual(parse_source_url(url)["kind"], "one_to_one")

    def test_group_chat(self):
        self.assertEqual(parse_source_url(URL_GROUP)["kind"], "group")

    def test_tacv2_is_group(self):
        self.assertEqual(parse_source_url(URL_TACV2)["kind"], "group")

    def test_meeting_chat(self):
        # meeting_ prefix wins over the @thread.v2 suffix
        self.assertEqual(parse_source_url(URL_MEETING)["kind"], "meeting")

    def test_channel_post(self):
        self.assertEqual(parse_source_url(URL_CHANNEL)["kind"], "channel")

    def test_non_chat_urls_have_no_chat_destination(self):
        for url in (URL_OUTLOOK, URL_SHAREPOINT, URL_MEETING_DETAILS):
            with self.subTest(url=url[:60]):
                self.assertEqual(parse_source_url(url)["kind"], "none")

    def test_missing_url_is_none_kind(self):
        for url in (None, "", "   ", "not a url"):
            with self.subTest(url=repr(url)):
                self.assertEqual(parse_source_url(url)["kind"], "none")

    def test_unknown_thread_suffix_is_not_treated_as_one_to_one(self):
        # Fail safe: an unrecognised conversation type must never be assumed 1:1.
        url = "https://teams.microsoft.com/l/message/19:something@thread.future/123"
        self.assertNotEqual(parse_source_url(url)["kind"], "one_to_one")


class TestSafetyClass(unittest.TestCase):
    """is_broadcast collapses kind to the binary the UI actually gates on."""

    def test_only_one_to_one_is_not_broadcast(self):
        self.assertFalse(parse_source_url(URL_1TO1_2076)["is_broadcast"])

    def test_group_meeting_channel_are_broadcast(self):
        for url in (URL_GROUP, URL_MEETING, URL_CHANNEL, URL_TACV2):
            with self.subTest(url=url[:70]):
                self.assertTrue(parse_source_url(url)["is_broadcast"])

    def test_unknown_is_broadcast(self):
        url = "https://teams.microsoft.com/l/message/19:something@thread.future/123"
        self.assertTrue(parse_source_url(url)["is_broadcast"])


class TestConversationExtraction(unittest.TestCase):

    def test_conversation_id_extracted(self):
        self.assertEqual(
            parse_source_url(URL_GROUP)["conversation_id"],
            "19:723efdcdeef840a983dcc68779914cbb@thread.v2",
        )

    def test_conversation_id_excludes_query_string(self):
        conv = parse_source_url(URL_1TO1_2076)["conversation_id"]
        self.assertNotIn("?", conv)
        self.assertNotIn("context", conv)
        self.assertTrue(conv.endswith("@unq.gbl.spaces"))

    def test_message_id_extracted(self):
        self.assertEqual(parse_source_url(URL_GROUP)["message_id"], "1771978315725")

    def test_message_id_for_one_to_one(self):
        self.assertEqual(parse_source_url(URL_1TO1_2076)["message_id"], "1785358519108")

    def test_chat_link_has_conversation_but_no_message(self):
        parsed = parse_source_url(URL_1TO1_CHAT)
        self.assertEqual(
            parsed["conversation_id"],
            "19:08b7be88-37ac-4e2b-82af-f8bb67e5f2f7_"
            "d87be1b4-816c-4eff-9edd-7a5823986db1@unq.gbl.spaces",
        )
        self.assertIsNone(parsed["message_id"])

    def test_no_conversation_id_for_non_chat(self):
        self.assertIsNone(parse_source_url(URL_OUTLOOK)["conversation_id"])


class TestCounterpartyExtraction(unittest.TestCase):
    """A 1:1 conversation id is {userA}_{userB}; the other GUID is the recipient."""

    def test_counterparty_is_the_other_guid(self):
        self.assertEqual(
            parse_source_url(URL_1TO1_2076, me=PHIL)["counterparty_id"],
            "007b4f8b-2585-442b-91d9-581972e27761",
        )

    def test_counterparty_when_self_is_first(self):
        self.assertEqual(
            parse_source_url(URL_1TO1_2057, me=PHIL)["counterparty_id"],
            "db4dc955-ec8f-449f-92c7-1ee80f3feeba",
        )

    def test_counterparty_undashed_guid(self):
        # 38 of 629 real 1:1 links write the participant id as bare 32-hex with no
        # hyphens. Found by sweeping the live DB, not by unit tests.
        self.assertEqual(
            parse_source_url(URL_1TO1_UNDASHED, me=PHIL)["counterparty_id"],
            "fe1c66c549f449abb2750c1eb2d2cbf6",
        )

    def test_counterparty_undashed_guid_listed_first(self):
        self.assertEqual(
            parse_source_url(URL_1TO1_UNDASHED_FIRST, me=PHIL)["counterparty_id"],
            "02b4cffd3e93446191c40ef3789bcb3e",
        )

    def test_me_matches_regardless_of_hyphenation(self):
        # The caller supplies `me` in canonical dashed form; the URL may not use it.
        undashed_me = PHIL.replace("-", "")
        self.assertEqual(
            parse_source_url(URL_1TO1_2076, me=undashed_me)["counterparty_id"],
            "007b4f8b-2585-442b-91d9-581972e27761",
        )

    def test_no_counterparty_without_me(self):
        self.assertIsNone(parse_source_url(URL_1TO1_2076)["counterparty_id"])

    def test_no_counterparty_for_group(self):
        self.assertIsNone(parse_source_url(URL_GROUP, me=PHIL)["counterparty_id"])

    def test_no_counterparty_when_me_absent_from_conversation(self):
        stranger = "11111111-2222-3333-4444-555555555555"
        self.assertIsNone(parse_source_url(URL_1TO1_2076, me=stranger)["counterparty_id"])


class TestResultShape(unittest.TestCase):

    def test_keys_always_present(self):
        # Callers destructure this unconditionally; a missing key is a crash at
        # preview time, which is the worst place to discover it.
        expected = {"kind", "is_broadcast", "conversation_id", "message_id",
                    "counterparty_id", "audience_label"}
        for url in (URL_1TO1_2076, URL_GROUP, URL_OUTLOOK, None, "junk"):
            with self.subTest(url=str(url)[:50]):
                self.assertEqual(set(parse_source_url(url)), expected)

    def test_audience_label_is_human_readable(self):
        self.assertEqual(parse_source_url(URL_1TO1_2076)["audience_label"],
                         "direct message")
        self.assertEqual(parse_source_url(URL_CHANNEL)["audience_label"],
                         "team channel")


if __name__ == "__main__":
    unittest.main()
