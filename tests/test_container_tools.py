"""Container-local tools are denied for CONTAINMENT, not because they mutate M365.

Found by the dogfood soak on a real run (action 37). Its tool list was entirely
reads plus `Skill`, yet the verdict came back `held_unconfirmed` - "a write tool
was called" - because `skill` is on the denylist and the name match treats every
denylist entry as a write.

Invoking a Cowork skill is not an M365 write. Our own prompt asks for skills by
name (`work-email-voice`, `work-teams-voice`), so this would fire on a large
share of normal runs and quietly degrade the canary again - the same slide that
7b693b0 fixed for `Bash`.

The denylist has a clean structural rule for this. Every M365 service tool is
namespaced:

    outlook-SendEmailWithAttachments      m365_teams-PostMessage
    graph-CallGraph                       host-SetupScheduledPrompt

while the container-local ones are bare single words:

    bash  create  edit  skill  stop_bash  task  write_agent

So "unqualified means container-local" is a property of the data, not a guess,
and deriving the set keeps it correct if the list changes.

These stay ON the denylist - they are still intercepted, which is what stops a
run shelling out to bypass the barrier. They are simply not evidence that an
M365 write was attempted.
"""

import unittest

from src.services.cowork_runner import (
    _CONTAINER_TOOLS,
    _looks_like_write,
    load_write_tools,
)


class TestContainerToolsAreNotM365Writes(unittest.TestCase):
    def test_skill_is_not_a_write(self):
        """The one observed live, on action 37."""
        self.assertFalse(_looks_like_write("Skill"))

    def test_every_unqualified_denylist_entry_is_container_local(self):
        for tool in load_write_tools():
            if "-" in tool:
                continue
            with self.subTest(tool=tool):
                self.assertFalse(_looks_like_write(tool))

    def test_the_set_matches_the_denylist_shape(self):
        """Pins membership so a surprising denylist change is visible."""
        unqualified = {t.strip().lower() for t in load_write_tools() if "-" not in t}
        self.assertEqual(unqualified, set(_CONTAINER_TOOLS))

    def test_they_are_still_denied(self):
        """Excluding them from WRITE DETECTION must not remove them from the
        barrier: `bash` is how a run would shell out and bypass it."""
        names = {t.strip().lower() for t in load_write_tools()}
        for tool in ("bash", "skill", "task", "write_agent"):
            with self.subTest(tool=tool):
                self.assertIn(tool, names)


class TestM365WritesAreUnaffected(unittest.TestCase):
    """The exclusion is on the EXACT bare name, so real writes still match."""

    def test_a_create_action_on_a_service_is_still_a_write(self):
        self.assertTrue(_looks_like_write("mcp__outlook_calendar__CreateEvent"))

    def test_a_create_display_label_is_still_a_write(self):
        self.assertTrue(_looks_like_write("Create folder"))

    def test_an_edit_action_on_a_service_is_still_a_write(self):
        self.assertTrue(_looks_like_write("sharepoint_onedrive-EditFile"))

    def test_the_send_tool_is_still_a_write(self):
        self.assertTrue(
            _looks_like_write("mcp__outlook__SendEmailWithAttachments")
        )


class TestARealReadOnlyRunReadsAsNotExercised(unittest.TestCase):
    """The exact tool list from action 37, which prompted this."""

    TOOLS = [
        "tool_search_tool",
        "mcp__m365_search__SearchM365",
        "mcp__m365_teams__ListChats",
        "Bash",
        "mcp__m365_teams__ListChatMessages",
        "mcp__me_profile__GetMultipleUsersDetails",
        "Skill",
    ]

    def test_no_tool_in_a_research_run_reads_as_a_write(self):
        flagged = [t for t in self.TOOLS if _looks_like_write(t)]
        self.assertEqual(flagged, [])

    def test_the_verdict_is_not_exercised(self):
        from src.services.cowork_runner import _barrier_verdict

        trace = [{"tool_name": t, "ok": True} for t in self.TOOLS]
        verdict = _barrier_verdict(trace, [], "Here are the findings.")
        self.assertEqual(verdict["status"], "not_exercised")


if __name__ == "__main__":
    unittest.main()
