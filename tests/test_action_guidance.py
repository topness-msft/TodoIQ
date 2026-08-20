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

import json
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
        "user_notes": "",
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


class TestSchedulingUsesNativeCalendarFlow(unittest.TestCase):
    def setUp(self):
        self.prompt = compose_prompt(_task(
            skill_output=(
                "Suggested meeting slots:\n"
                "1. Monday, August 17, 10:00-10:30 AM ET\n"
                "2. Monday, August 17, 3:00-3:30 PM ET"
            )
        ))

    def test_it_is_a_concise_native_calendar_request(self):
        self.assertTrue(self.prompt.startswith("Riveter: "))
        self.assertIn("Schedule a recurring 1:1 with Rima Reyes", self.prompt)
        self.assertIn("rima.reyes@microsoft.com", self.prompt)
        self.assertIn("native calendar scheduling flow", self.prompt)
        self.assertIn("FindMeetingTimes", self.prompt)
        self.assertIn("ask_user", self.prompt)
        self.assertIn("three exact available times", self.prompt)
        self.assertIn("CreateEvent", self.prompt)
        self.assertIn("[slot:", self.prompt)
        self.assertIn("[avail:", self.prompt)
        self.assertLess(len(self.prompt), 1150)

    def test_it_does_not_request_a_message_draft(self):
        self.assertNotIn("[VOICE]", self.prompt)
        self.assertNotIn("[OUTPUT]", self.prompt)
        self.assertNotIn("draft message", self.prompt.lower())

    def test_it_waits_for_the_selected_time_before_creating_event(self):
        self.assertIn(
            "do not call createevent before the user selects one",
            self.prompt.lower(),
        )
        self.assertIn("only the selected time", self.prompt.lower())

    def test_it_preserves_duration_and_exact_selected_slot(self):
        lowered = self.prompt.lower()
        self.assertIn("requested duration is invariant", lowered)
        self.assertIn("never shorten", lowered)
        self.assertIn("never shift", lowered)
        self.assertIn(
            "timing correction requires fresh findmeetingtimes and a new selection",
            lowered,
        )
        self.assertIn(
            "createevent start and end must exactly match the selected slot",
            lowered,
        )

    def test_it_binds_slot_metadata_to_the_requested_duration(self):
        prompt = compose_prompt(_task(
            title="Schedule a 45-minute architecture review with Rima Reyes",
        ))
        self.assertIn("Times must be 45m", prompt)
        hour_prompt = compose_prompt(_task(
            title="Schedule a 1 hour architecture review with Rima Reyes",
        ))
        self.assertIn("Times must be 60m", hour_prompt)
        word_prompt = compose_prompt(_task(
            title="Arrange a five-minute sync with Rima Reyes",
        ))
        self.assertIn("Times must be 5m", word_prompt)
        half_hour = compose_prompt(_task(
            title="Arrange a half an hour sync with Rima Reyes",
        ))
        self.assertIn("Times must be 30m", half_hour)
        two_hours = compose_prompt(_task(
            title="Arrange a two-hour workshop with Rima Reyes",
        ))
        self.assertIn("Times must be 120m", two_hours)
        compound = compose_prompt(_task(
            title="Arrange an hour and a half workshop with Rima Reyes",
        ))
        self.assertIn("Times must be 90m", compound)
        natural_compound = compose_prompt(_task(
            title="Arrange a two and a half hours workshop with Rima Reyes",
        ))
        self.assertIn("Times must be 150m", natural_compound)
        hyphenated_compound = compose_prompt(_task(
            title="Arrange an hour-and-a-half workshop with Rima Reyes",
        ))
        self.assertIn("Times must be 90m", hyphenated_compound)
        bare_compound = compose_prompt(_task(
            title="Arrange an hour-and-a-half with Rima Reyes",
        ))
        self.assertIn("Times must be 90m", bare_compound)
        twenty_five = compose_prompt(_task(
            title="Arrange a twenty five minute review with Rima Reyes",
        ))
        self.assertIn("Times must be 25m", twenty_five)
        forty_five = compose_prompt(_task(
            title="Arrange a forty five minutes review with Rima Reyes",
        ))
        self.assertIn("Times must be 45m", forty_five)
        mixed_units = compose_prompt(_task(
            title="Arrange a 1 hour and 30 minutes workshop with Rima Reyes",
        ))
        self.assertIn("Times must be 90m", mixed_units)
        adjacent_units = compose_prompt(_task(
            title="Arrange a 1 hour 30 minutes workshop with Rima Reyes",
        ))
        self.assertIn("Times must be 90m", adjacent_units)
        adjacent_word_units = compose_prompt(_task(
            title="Arrange a two hour fifteen minute workshop with Rima Reyes",
        ))
        self.assertIn("Times must be 135m", adjacent_word_units)

    def test_it_does_not_reuse_stale_enrichment_slots(self):
        self.assertNotIn("Monday, August 17, 10:00-10:30 AM ET", self.prompt)

    def test_it_carries_user_agenda_notes(self):
        prompt = compose_prompt(_task(
            user_notes="Topic: Sync up on Project Whale",
        ))
        self.assertIn("Sync up on Project Whale", prompt)

    def test_it_requires_timezone_checks_before_availability_search(self):
        prompt = compose_prompt(_task(
            key_people=json.dumps([
                {
                    "name": "Chris Garty",
                    "email": "chris.garty@microsoft.com",
                    "timezone": "Central Standard Time",
                },
                {
                    "name": "Doug Bellingeri",
                    "email": "dbellingeri@microsoft.com",
                    "timezone": "Eastern Standard Time",
                },
            ])
        ))
        self.assertLess(
            prompt.index("confirmed email"),
            prompt.index("FindMeetingTimes"),
        )
        self.assertIn("Chris Garty", prompt)
        self.assertIn("Central Standard Time", prompt)
        self.assertIn("Doug Bellingeri", prompt)
        self.assertIn("Eastern Standard Time", prompt)
        self.assertIn("local time", prompt)
        self.assertIn("work schedules", prompt)
        self.assertIn("do not use people profile", prompt.lower())
        self.assertIn("text-only clarification", prompt.lower())
        self.assertNotIn("timezone is unknown", prompt.lower())

    def test_it_rejects_an_empty_attendee_list(self):
        with self.assertRaisesRegex(ValueError, "confirmed attendee"):
            compose_prompt(_task(key_people="[]"))

    def test_it_rejects_uncertain_group_attendance(self):
        people = [{
            "name": "Exact Chat Member",
            "email": "member@example.test",
            "aad_object_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "attendance_uncertain": True,
        }]
        with self.assertRaisesRegex(ValueError, "confirmed attendee"):
            compose_prompt(_task(key_people=json.dumps(people)))


class TestSchedulingUsesCalendarVoice(unittest.TestCase):
    def test_teams_fallback_does_not_turn_meeting_into_chat_draft(self):
        prompt = compose_prompt(_task(), delivery_channel="teams")
        self.assertIn("native calendar scheduling flow", prompt.lower())
        self.assertNotIn("work-teams-voice", prompt)
        self.assertNotIn("match chat register", prompt.lower())


class TestSelectedPeopleOnly(unittest.TestCase):
    def test_disambiguation_alternatives_are_not_sent_to_cowork(self):
        prompt = compose_prompt(_task(
            key_people=json.dumps([
                {
                    "name": "Rima Reyes",
                    "email": "rima.reyes@microsoft.com",
                    "role": "Principal Product Manager",
                    "alternatives": [
                        {
                            "name": "Rima Gooden",
                            "email": "rimagooden@microsoft.com",
                        }
                    ],
                }
            ])
        ))

        self.assertIn("Rima Reyes", prompt)
        self.assertIn("rima.reyes@microsoft.com", prompt)
        self.assertNotIn("Rima Gooden", prompt)
        self.assertNotIn("rimagooden@microsoft.com", prompt)
        self.assertNotIn('"alternatives"', prompt)


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


class TestNativeSchedulingCorrection(unittest.TestCase):
    def test_a_correction_is_kept_in_the_concise_request(self):
        prompt = compose_prompt(_task(), redirect_text="start 5 minutes late")
        self.assertIn("start 5 minutes late", prompt)
        self.assertLess(len(prompt), 1150)
        self.assertLess(
            prompt.index("User correction:"),
            prompt.index("Non-negotiable after any correction:"),
        )


if __name__ == "__main__":
    unittest.main()
