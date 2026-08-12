"""Write detection must key on the ACTION, not the product name.

Found by the Phase 4 soak within one real preview: a task ran three read-only
SharePoint tools and the verdict reported BREACHED, failing the preview.

    mcp__sharepoint_onedrive__SearchSites       -> write=True, verb hit "share"
    mcp__sharepoint_onedrive__ListSiteDrives    -> write=True, verb hit "share"
    mcp__sharepoint_onedrive__GetDriveChildren  -> write=True, verb hit "share"

The cause is naive substring matching: "share" is inside "sharepoint". The
SERVER name tripped the heuristic, not the action. None of these tools write
anything, and none are on our denylist, so they were reported as writes that we
never asked to block - the loudest verdict we have.

Same class as the `Bash` false positive fixed in 7b693b0, and a reminder that a
detector tuned on 18 rows had simply never met SharePoint.

The fix: for an MCP name, only the LAST segment is the action; the middle
segment is the server. Match write verbs against whole TOKENS of the action, by
equality rather than substring, so "settings" no longer looks like "set" and
"sharepoint" no longer looks like "share".

The dangerous direction is the opposite one - a real write we fail to notice -
so TestNoWriteIsMissed pins every entry of the live denylist.
"""

import unittest

from src.services.cowork_runner import _looks_like_write, load_write_tools


class TestProductNamesAreNotVerbs(unittest.TestCase):
    """The exact tools that failed a real preview during the soak."""

    READ_ONLY = [
        "mcp__sharepoint_onedrive__SearchSites",
        "mcp__sharepoint_onedrive__ListSiteDrives",
        "mcp__sharepoint_onedrive__GetDriveChildren",
    ]

    def test_sharepoint_reads_are_not_writes(self):
        for name in self.READ_ONLY:
            with self.subTest(name=name):
                self.assertFalse(_looks_like_write(name))

    def test_share_still_matches_a_real_share_action(self):
        """Fixing the false positive must not lose the true one."""
        self.assertTrue(_looks_like_write("mcp__onedrive__ShareItem"))

    def test_settings_is_not_a_set(self):
        """`settings` starts with `set`, which is why equality beats prefix."""
        self.assertFalse(_looks_like_write("mcp__outlook__GetSettings"))

    def test_search_is_not_a_write(self):
        self.assertFalse(_looks_like_write("mcp__m365_search__SearchM365"))

    def test_a_read_verb_on_a_writeish_server_is_still_a_read(self):
        self.assertFalse(_looks_like_write("mcp__sharepoint_onedrive__GetSite"))


class TestRealWritesAreStillCaught(unittest.TestCase):
    """The dangerous direction: a write we fail to notice."""

    def test_canonical_send_is_a_write(self):
        self.assertTrue(_looks_like_write("mcp__outlook__SendEmailWithAttachments"))

    def test_display_labels_still_work(self):
        for label in ("Post message", "Send email with attachments",
                      "Create folder", "Upload file content"):
            with self.subTest(label=label):
                self.assertTrue(_looks_like_write(label))

    def test_verb_variants_are_caught(self):
        for name in ("host-SetupScheduledPrompt", "mcp__x__ScheduledSend",
                     "mcp__x__SetPresence"):
            with self.subTest(name=name):
                self.assertTrue(_looks_like_write(name))


class TestNoWriteIsMissed(unittest.TestCase):
    """Every tool we deliberately deny must still read as a write.

    This is the regression guard that matters. Loosening the heuristic to kill
    a false positive must never silently create a false negative, because a
    missed write is the failure mode the whole barrier exists to prevent.
    """

    def test_every_denylist_entry_reads_as_a_write(self):
        # Container tools are on the denylist for CONTAINMENT (so a run cannot
        # shell out and bypass the barrier), not because they mutate M365.
        # 7b693b0 deliberately stopped treating them as writes.
        from src.services.cowork_runner import _CONTAINER_TOOLS

        missed = [t for t in load_write_tools()
                  if t.strip().lower() not in _CONTAINER_TOOLS
                  and not _looks_like_write(t)]
        self.assertEqual(missed, [])

    def test_every_denylist_entry_reads_as_a_write_in_mcp_form(self):
        """The runtime reports `mcp__<server>__<Action>`, not our spelling.

        Before this was fixed, 20 of 84 entries were invisible in that form —
        including `graph-CallGraph`, which this codebase's own comments call a
        universal bypass. The barrier still intercepted them; the CANARY could
        not see them, which is the blind spot that matters.
        """
        from src.services.cowork_runner import _CONTAINER_TOOLS

        missed = []
        for tool in load_write_tools():
            if "-" not in tool or tool.strip().lower() in _CONTAINER_TOOLS:
                continue
            server, action = tool.split("-", 1)
            if not _looks_like_write(f"mcp__{server}__{action}"):
                missed.append(tool)
        self.assertEqual(missed, [])


if __name__ == "__main__":
    unittest.main()
