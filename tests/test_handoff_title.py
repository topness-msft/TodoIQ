"""The handed-off task must be identifiable in the Cowork web app.

Every Riveter conversation shows up in Phil's Cowork task list titled
"[ROLE] You are helping the user act". That is literally the first 35
characters of our prompt: the runtime derives a task title by truncating the
opening text, and there is no title field on /v1/subscribe to override it.

Confirmed against the live API for three real conversations, all identical:

    "title": "[ROLE] You are helping the user act"

This only started to matter now that "Finish in Cowork" actually resolves.
Phil is being sent into a list where every row we created is indistinguishable
from every other, while the information that would identify it (the task
title) sits unused on line 5 of the same prompt.

The fix is a human-readable first line. Ordering of the tagged layers is
semantic and must not move, so the title goes ABOVE [ROLE] and changes
nothing else.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from src.services.cowork_runner import compose_prompt  # noqa: E402

from test_cowork_prompt import make_task, sections  # noqa: E402


class HandoffTitleTest(unittest.TestCase):
    def test_the_first_line_is_not_the_role_tag(self):
        first = compose_prompt(make_task()).splitlines()[0]
        self.assertNotIn("[ROLE]", first)
        self.assertNotIn("You are helping the user act", first)

    def test_the_first_line_carries_the_task_title(self):
        task = make_task(title="Send Raj the Kickstarter materials")
        first = compose_prompt(task).splitlines()[0]
        self.assertIn("Send Raj the Kickstarter materials", first)

    def test_the_first_line_names_riveter_as_the_origin(self):
        """So a handed-off task is distinguishable from one started in Cowork."""
        first = compose_prompt(make_task()).splitlines()[0]
        self.assertIn("Riveter", first)

    def test_the_first_line_survives_truncation(self):
        """A task list shows a short prefix, so the useful part must be early."""
        task = make_task(title="Follow up with Brandon on the PPCC exec list")
        first = compose_prompt(task).splitlines()[0]
        self.assertLessEqual(len(first), 90)

    def test_a_very_long_title_is_shortened(self):
        task = make_task(title="Coordinate " + "the quarterly planning cycle " * 12)
        first = compose_prompt(task).splitlines()[0]
        self.assertLessEqual(len(first), 90)

    def test_a_missing_title_still_produces_a_usable_line(self):
        first = compose_prompt(make_task(title="")).splitlines()[0]
        self.assertIn("Riveter", first)
        self.assertTrue(first.strip())

    def test_the_title_line_is_stripped_of_newlines(self):
        """A multi-line title would push [ROLE] out of the first position."""
        task = make_task(title="Line one\nLine two\r\nLine three")
        first = compose_prompt(task).splitlines()[0]
        self.assertIn("Line one", first)
        self.assertIn("Line two", first)

    def test_layer_order_is_untouched(self):
        """The tagged layers are semantic; the title must not disturb them."""
        got = sections(compose_prompt(make_task()))
        self.assertEqual(got[0], "[ROLE]")
        self.assertEqual(
            got,
            ["[ROLE]", "[TASK]", "[INTENT]", "[SOURCE]", "[VOICE]", "[OUTPUT]"],
        )

    def test_the_safety_line_is_still_last(self):
        p = compose_prompt(make_task())
        self.assertGreater(p.lower().rindex("do not send"), p.index("[OUTPUT]"))


if __name__ == "__main__":
    unittest.main()
