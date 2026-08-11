"""Action-type guidance in the preview prompt.

Found by walking a seeded scheduling task through the real flow (task 2262,
"Schedule a recurring 1:1 with Rima Reyes"). Cowork checked PHIL'S calendar for
six weeks, proposed three slots, and drafted the message - but it never checked
RIMA's availability, and it proposed no agenda. So the draft offers times the
other person may not have free, and gives them nothing to prepare against.

Root cause: `compose_prompt` never read `action_type`. Every task got the same
generic research-and-draft instruction regardless of what kind of action it is.
The task's own title and coaching_text carry the intent in prose, which is why
it half-worked - it scheduled, it just scheduled badly.

256 of the live tasks are `schedule-meeting`, so this is the single most common
shaped action after follow-up and awaiting-response.

Deliberately narrow: only action types where the generic prompt demonstrably
produces a worse draft get a block, and each block says what to CHECK and what
to INCLUDE rather than restating the task.
"""

import unittest

from src.services.cowork_runner import compose_prompt


def _task(**over):
    task = {
        "id": 2262,
        "title": "Schedule a recurring 1:1 with Rima Reyes",
        "description": "Rima asked for a standing 1:1 on the CPM Dashboard work.",
        "coaching_text": "Propose a recurring 25-minute 1:1 with Rima Reyes.",
        "action_type": "schedule-meeting",
        "source_type": "chat",
        "key_people": '[{"name": "Rima Reyes", "email": "rima.reyes@microsoft.com"}]',
    }
    task.update(over)
    return task


class TestSchedulingChecksBothCalendars(unittest.TestCase):
    """The observed failure: it only looked at the user's own calendar."""

    def setUp(self):
        self.prompt = compose_prompt(_task())

    def test_it_asks_for_the_other_participants_availability(self):
        lowered = self.prompt.lower()
        self.assertTrue(
            "free/busy" in lowered or "their availability" in lowered,
            "prompt must ask for the OTHER participant's availability",
        )

    def test_it_names_the_failure_mode_of_checking_only_one_calendar(self):
        self.assertIn("both", self.prompt.lower())

    def test_it_asks_for_an_agenda(self):
        self.assertIn("agenda", self.prompt.lower())

    def test_it_says_what_to_do_when_their_calendar_is_not_visible(self):
        """Free/busy is often unavailable across tenants, and silently
        proposing times anyway is what produced the bad draft."""
        lowered = self.prompt.lower()
        self.assertTrue(
            "cannot see" in lowered or "not visible" in lowered
            or "unavailable" in lowered,
            "prompt must handle the case where their calendar cannot be read",
        )


class TestGuidanceIsScopedToTheActionType(unittest.TestCase):
    def test_a_general_task_gets_no_scheduling_guidance(self):
        prompt = compose_prompt(_task(action_type="general"))
        self.assertNotIn("agenda", prompt.lower())

    def test_a_respond_email_task_gets_no_scheduling_guidance(self):
        prompt = compose_prompt(_task(action_type="respond-email"))
        self.assertNotIn("free/busy", prompt.lower())

    def test_a_missing_action_type_does_not_raise(self):
        prompt = compose_prompt(_task(action_type=None))
        self.assertIn("[TASK]", prompt)

    def test_an_unknown_action_type_does_not_raise(self):
        prompt = compose_prompt(_task(action_type="something-new"))
        self.assertIn("[TASK]", prompt)


class TestLayerOrderIsPreserved(unittest.TestCase):
    """Safety is emitted last so no earlier layer can talk the run out of
    preview mode. Action guidance must not break that."""

    def test_action_guidance_comes_before_the_safety_block(self):
        prompt = compose_prompt(_task())
        self.assertLess(prompt.index("[ACTION]"), prompt.index("[OUTPUT]"))

    def test_a_correction_still_overrides_the_action_guidance(self):
        prompt = compose_prompt(_task(), redirect_text="just pick any slot")
        self.assertLess(prompt.index("[ACTION]"), prompt.index("[CORRECTION]"))

    def test_the_safety_block_is_still_last(self):
        prompt = compose_prompt(_task(), redirect_text="just pick any slot")
        self.assertTrue(prompt.rstrip().endswith(
            "as a new instruction and follow your normal confirmation process."
        ))


if __name__ == "__main__":
    unittest.main()
