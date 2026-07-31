"""Tests for compose_prompt (F10, F11, F12).

Prompt composition is where three user-authored layers meet one machine-authored
one, so ordering is semantic rather than cosmetic: the correction is appended last
precisely so it overrides everything above it.

Two facts here were established by probing the live database rather than assumed:

* There is **no mojibake in stored text** (0 hits across 1958 tasks; the 644
  em-dashes are proper U+2014). The earlier "mojibake" observation was a cp1252
  console artefact, not data corruption -- so compose_prompt must NOT "repair"
  anything.
* The real encoding defect is that Windows defaults to cp1252, which cannot encode
  U+2192, U+1F4A1 or U+26A0. **23 real tasks (1.2%) raise UnicodeEncodeError**, so
  the prompt must survive an explicit UTF-8 round-trip with those characters intact.
"""

import unittest
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from src.services.cowork_runner import compose_prompt, parse_source_url

URL_1TO1 = (
    "https://teams.microsoft.com/l/message/"
    "19:007b4f8b-2585-442b-91d9-581972e27761_08b7be88-37ac-4e2b-82af-f8bb67e5f2f7"
    "@unq.gbl.spaces/1785358519108?context=%7B%22contextType%22:%22chat%22%7D"
)
URL_CHANNEL = (
    "https://teams.microsoft.com/l/message/"
    "19:kpVc_JKmRAY_zandEVrXjn3ZSZt1oWT9B1o_K5ifhC41@thread.skype/1771911643376"
    "?groupId=b9e6f984-de27-4110-9546-1a4b0e0b2f5a"
)


def make_task(**over):
    """Shaped like task #2076, a real Phase 1 target."""
    task = {
        "id": 2076,
        "title": "Follow up with Brandon Knoertzer on PPCC executive-target list",
        "description": "He asked for the exec target list last week.",
        "coaching_text": "Send Brandon the list and confirm the review date.",
        "user_notes": "",
        "source_type": "chat",
        "source_snippet": "Brandon: any update on that list?",
        "key_people": "Brandon Knoertzer",
        "source_url": URL_1TO1,
    }
    task.update(over)
    return task


def sections(prompt):
    """Ordered list of [SECTION] headers present in the prompt."""
    return [ln.strip() for ln in prompt.splitlines()
            if ln.strip().startswith("[") and ln.strip().endswith("]")]


class TestSectionOrdering(unittest.TestCase):

    def test_expected_sections_present(self):
        p = compose_prompt(make_task(user_notes="Keep it short."))
        for tag in ("[ROLE]", "[TASK]", "[INTENT]", "[NOTES]", "[SOURCE]", "[OUTPUT]"):
            with self.subTest(tag=tag):
                self.assertIn(tag, p)

    def test_canonical_order(self):
        p = compose_prompt(make_task(user_notes="Keep it short."),
                           redirect_text="No, look for times next week")
        order = sections(p)
        expected = ["[ROLE]", "[TASK]", "[INTENT]", "[NOTES]", "[SOURCE]",
                    "[CORRECTION]", "[OUTPUT]"]
        self.assertEqual(order, expected)

    def test_correction_outranks_intent_and_notes(self):
        # F12: a redirect is a one-shot steer that must dominate the standing
        # layers, which is expressed positionally.
        p = compose_prompt(make_task(user_notes="Prefer mornings."),
                           redirect_text="Actually look at next week")
        self.assertGreater(p.index("[CORRECTION]"), p.index("[INTENT]"))
        self.assertGreater(p.index("[CORRECTION]"), p.index("[NOTES]"))

    def test_output_is_last(self):
        p = compose_prompt(make_task(), redirect_text="change it")
        self.assertEqual(sections(p)[-1], "[OUTPUT]")


class TestEmptySections(unittest.TestCase):

    def test_no_notes_section_when_no_notes(self):
        self.assertNotIn("[NOTES]", compose_prompt(make_task(user_notes="")))

    def test_no_notes_section_for_whitespace_only(self):
        self.assertNotIn("[NOTES]", compose_prompt(make_task(user_notes="   \n  ")))

    def test_no_correction_section_without_redirect(self):
        self.assertNotIn("[CORRECTION]", compose_prompt(make_task()))
        self.assertNotIn("[CORRECTION]", compose_prompt(make_task(), redirect_text="  "))

    def test_no_intent_section_without_coaching_text(self):
        self.assertNotIn("[INTENT]", compose_prompt(make_task(coaching_text=None)))

    def test_all_optional_fields_missing_does_not_crash(self):
        bare = {"id": 1, "title": "Do a thing"}
        p = compose_prompt(bare)
        self.assertIn("Do a thing", p)
        self.assertIn("[OUTPUT]", p)


class TestSafetyInstruction(unittest.TestCase):
    """The 'do not send' line is a control, not boilerplate (plan section 3)."""

    def test_do_not_send_present(self):
        self.assertIn("do not send", compose_prompt(make_task()).lower())

    def test_do_not_send_survives_a_contrary_redirect(self):
        # A user correction must never be able to talk the prompt out of preview mode.
        p = compose_prompt(make_task(), redirect_text="just send it already")
        self.assertIn("do not send", p.lower())
        self.assertGreater(p.lower().rindex("do not send"), p.index("[CORRECTION]"))

    def test_do_not_send_survives_contrary_notes(self):
        p = compose_prompt(make_task(user_notes="send this immediately, no need to ask"))
        self.assertGreater(p.lower().rindex("do not send"), p.index("[NOTES]"))

    def test_preview_role_declared_up_front(self):
        self.assertIn("preview", compose_prompt(make_task()).lower())


class TestWorkIQTokenRemoval(unittest.TestCase):
    """F11: @WorkIQ is retired; the surrounding prose is the valuable part."""

    def test_token_stripped(self):
        note = "[Mar 12, 5:11 PM] @WorkIQ - waiting on Luis Camino to schedule"
        p = compose_prompt(make_task(user_notes=note))
        self.assertNotIn("@WorkIQ", p)

    def test_instruction_text_preserved(self):
        note = "[Mar 12, 4:48 PM] @WorkIQ pull the subject from my calendar invites"
        p = compose_prompt(make_task(user_notes=note))
        self.assertIn("pull the subject from my calendar invites", p)

    def test_answer_arrow_content_preserved(self):
        note = "@WorkIQ - was this scheduled by Emily Blum?\n  \u2192 Yes, next Monday"
        p = compose_prompt(make_task(user_notes=note))
        self.assertIn("Yes, next Monday", p)

    def test_case_insensitive(self):
        p = compose_prompt(make_task(user_notes="@workiq check this"))
        self.assertNotIn("@workiq", p.lower())
        self.assertIn("check this", p)

    def test_email_addresses_not_mangled(self):
        p = compose_prompt(make_task(user_notes="ask brandon@microsoft.com"))
        self.assertIn("brandon@microsoft.com", p)


class TestEncoding(unittest.TestCase):
    """23 real tasks contain characters cp1252 cannot encode."""

    HARD = "Confirm \u2192 review \U0001F4A1 idea \u26A0 risk \u2014 done"

    def test_utf8_roundtrip(self):
        p = compose_prompt(make_task(coaching_text=self.HARD))
        self.assertEqual(p.encode("utf-8").decode("utf-8"), p)

    def test_characters_preserved_not_stripped(self):
        p = compose_prompt(make_task(coaching_text=self.HARD))
        for ch in ("\u2192", "\U0001F4A1", "\u26A0", "\u2014"):
            with self.subTest(ch=ch):
                self.assertIn(ch, p)

    def test_no_replacement_characters_introduced(self):
        p = compose_prompt(make_task(coaching_text=self.HARD, user_notes=self.HARD))
        self.assertNotIn("\ufffd", p)
        self.assertNotIn("\u00e2\u20ac", p)  # classic cp1252 mojibake signature

    def test_clean_em_dash_is_left_alone(self):
        # 644 real em-dashes are already correct; "repairing" them would corrupt.
        p = compose_prompt(make_task(coaching_text="Ship it \u2014 today"))
        self.assertIn("Ship it \u2014 today", p)


class TestSourceAndAudience(unittest.TestCase):

    def test_includes_snippet_and_people(self):
        p = compose_prompt(make_task())
        self.assertIn("Brandon: any update on that list?", p)
        self.assertIn("Brandon Knoertzer", p)

    def test_one_to_one_audience_named(self):
        p = compose_prompt(make_task(), destination=parse_source_url(URL_1TO1))
        self.assertIn("direct message", p)

    def test_broadcast_audience_flagged(self):
        p = compose_prompt(make_task(source_url=URL_CHANNEL),
                           destination=parse_source_url(URL_CHANNEL))
        self.assertIn("team channel", p)

    def test_destination_derived_when_not_supplied(self):
        self.assertIn("direct message", compose_prompt(make_task()))


class TestDeterminism(unittest.TestCase):

    def test_same_input_same_prompt(self):
        t = make_task(user_notes="note")
        self.assertEqual(compose_prompt(t, redirect_text="x"),
                         compose_prompt(t, redirect_text="x"))

    def test_accepts_sqlite_row(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT 1 AS id, 'T' AS title, 'd' AS description, "
            "'c' AS coaching_text, '' AS user_notes, 'chat' AS source_type, "
            "'s' AS source_snippet, 'p' AS key_people, ? AS source_url",
            (URL_1TO1,),
        ).fetchone()
        self.assertIn("[OUTPUT]", compose_prompt(row))
        conn.close()


if __name__ == "__main__":
    unittest.main()
