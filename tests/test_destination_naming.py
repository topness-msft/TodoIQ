"""The destination block should name the chat, not just its shape.

Phil, looking at a real card: "I'd like this to resolve to the chat name and a
link to it". Today action 37 renders

    Drafted for: group chat  [TEAMS]
    Everyone in the chat would see this.

which tells him the SHAPE of the audience but not WHO. The information to do
better is already stored - that same row carries

    destination_ref  19:7cc89641bdb74f8199fa1ebb2eee631b@thread.v2
    key_people       Greg Hurlman, Srilakshmi Ramaswamy
    source_url       a working Teams deep link

so the generic word is a presentation gap, not a data gap. No new lookup, and
no dependency on WorkIQ.

Safety note: naming participants makes the broadcast warning MORE concrete, not
less. "Everyone in the chat" is easy to skim past; two named colleagues are not.
The risky styling and the warning text are unchanged.
"""

import unittest

from src.handlers.cowork import _resolve_destination


def _task(**over):
    task = {
        "id": 2188,
        "title": "Follow up with Greg Hurlman on Eaton nomination status",
        "source_type": "chat",
        "key_people": (
            '[{"name": "Greg Hurlman", "email": "grhurl@microsoft.com"}, '
            '{"name": "Srilakshmi Ramaswamy", "email": "v-srilramasw@microsoft.com"}]'
        ),
    }
    task.update(over)
    return task


GROUP = {"conversation_id": "19:7cc8@thread.v2", "audience_label": "group chat"}
ONE_TO_ONE = {"conversation_id": "19:abc@unq.gbl.spaces",
              "audience_label": "1:1 chat"}


class TestGroupChatsAreNamed(unittest.TestCase):
    def test_two_participants_are_both_named(self):
        got = _resolve_destination(_task(), GROUP)
        self.assertIn("Greg Hurlman", got["destination_display"])
        self.assertIn("Srilakshmi Ramaswamy", got["destination_display"])

    def test_the_audience_shape_is_still_stated(self):
        """Naming people must not lose the broadcast signal."""
        got = _resolve_destination(_task(), GROUP)
        self.assertIn("group chat", got["destination_display"])

    def test_a_long_roster_is_truncated_rather_than_wrapping(self):
        people = ", ".join(
            '{"name": "Person %d", "email": "p%d@x.com"}' % (i, i)
            for i in range(6)
        )
        got = _resolve_destination(_task(key_people=f"[{people}]"), GROUP)
        self.assertIn("+4", got["destination_display"])
        self.assertLess(len(got["destination_display"]), 70)

    def test_no_known_people_falls_back_to_the_shape(self):
        got = _resolve_destination(_task(key_people=""), GROUP)
        self.assertEqual(got["destination_display"], "group chat")

    def test_a_one_to_one_is_unchanged(self):
        """Single-person binding already read well and must not regress."""
        one = '[{"name": "Greg Hurlman", "email": "grhurl@microsoft.com"}]'
        got = _resolve_destination(_task(key_people=one), ONE_TO_ONE)
        self.assertEqual(got["destination_display"], "Greg Hurlman (1:1 chat)")

    def test_the_binding_itself_is_untouched(self):
        """Only the LABEL changes. The ref is what a send would ever use."""
        got = _resolve_destination(_task(), GROUP)
        self.assertEqual(got["destination_ref"], "19:7cc8@thread.v2")
        self.assertEqual(got["destination_source"], "auto_source_url")


if __name__ == "__main__":
    unittest.main()
