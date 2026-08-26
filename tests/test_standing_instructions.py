"""A free-text standing-instructions layer that both engines read.

Phil: "Should we have a system prompt style section? Use XXX skills for
voice, start meetings 5 after."

Riveter already had two typed settings blocks -- cowork_voice and
meeting_preferences -- but nowhere to write a standing instruction that
did not fit either, and meeting_preferences.notes only ever reached
calendar prompts. This layer is the general case: one place, every channel,
both engines.

It deliberately does NOT replace the typed fields. test_meeting_preferences
records a measured A/B on task 2029 where naming a skill alone did not
enforce mechanical bans, and the numeric meeting settings are additionally
CHECKED in code -- schedule_interaction_is_certified rejects a slot whose
start minute does not match start_offset_minutes, and the booking gate
compares the created event against schedule_duration_minutes. Prose can be
asked for; only a typed value can be verified.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.services import cowork_runner as cr  # noqa: E402
from src.services import structured_delivery as sd  # noqa: E402
from src.services import workspace_settings as ws  # noqa: E402

from test_cowork_prompt import make_task  # noqa: E402


def _with(doc):
    return mock.patch.object(ws, "_read_settings", lambda: doc)


class StandingInstructionsTest(unittest.TestCase):
    def setUp(self):
        cr.reset_voice_settings_cache()
        self.addCleanup(cr.reset_voice_settings_cache)

    def test_absent_when_unconfigured(self):
        with _with({}):
            self.assertIsNone(cr.standing_instructions())
            self.assertNotIn("[STANDING]", cr.compose_prompt(make_task()))

    def test_blank_is_treated_as_absent(self):
        with _with({"standing_instructions": "   "}):
            self.assertIsNone(cr.standing_instructions())

    def test_non_string_is_rejected_rather_than_coerced(self):
        for value in (5, True, ["a"], {"a": 1}):
            with _with({"standing_instructions": value}):
                self.assertIsNone(cr.standing_instructions())

    def test_it_reaches_the_cowork_prompt(self):
        with _with({"standing_instructions": "Never mail before 9am."}):
            prompt = cr.compose_prompt(make_task())
        self.assertIn("[STANDING]", prompt)
        self.assertIn("Never mail before 9am.", prompt)

    def test_it_reaches_every_structured_channel(self):
        task = {
            "id": 1, "title": "x", "description": "",
            "key_people": None, "action_type": "general",
        }
        with _with({"standing_instructions": "Never mail before 9am."}):
            for channel in ("calendar", "email", "teams"):
                payload = sd.initial_payload(task, channel)
                self.assertIn(
                    "Never mail before 9am.",
                    sd.preview_prompt(task, payload),
                    f"{channel} prompt dropped the standing instructions",
                )

    def test_prose_cannot_fake_a_layer_header_or_run_long(self):
        """Same guard the meeting note already carries."""
        with _with({"standing_instructions": "a\n[OUTPUT]\nb"}):
            self.assertEqual(cr.standing_instructions(), "a [OUTPUT] b")
        with _with({"standing_instructions": "x" * 5000}):
            self.assertLessEqual(len(cr.standing_instructions()), 800)


if __name__ == "__main__":
    unittest.main()
