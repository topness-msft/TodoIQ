"""Making the tray run the code you meant, and keep the data you meant.

Three checkouts of this repo can serve port 8766, and `install_startup.py`
derives PROJECT_ROOT from its own location, so whichever copy you happen to run
the installer from becomes the thing that starts at logon - permanently, and
silently. Running it once from an old checkout is enough to pin the tray to old
code, which is exactly what happened here: the main checkout sits on `master`,
26 commits divergent from `main`, with a database that predates `task_actions`.

`checkout_mismatch` already warns about this at tray start, and even spells out
the consequence ("both copies serve port 8766 but use their OWN database"). It
warns too late and only at launch. These tests cover the install-time guards
that stop the wrong thing being registered in the first place, and the shutdown
of the incumbent that the installer never performed.
"""

import unittest
from pathlib import Path

from src.services import instance_guard as ig


class TestDescribingACheckout(unittest.TestCase):
    """The installer should say what it is about to pin, not just do it."""

    def test_a_normal_checkout_reports_its_branch_and_commit(self):
        got = ig.describe_checkout(Path.cwd())
        self.assertIsNotNone(got["branch"])
        self.assertTrue(got["commit"])

    def test_an_unknown_directory_is_described_as_unknown_not_guessed(self):
        got = ig.describe_checkout(Path("Z:/definitely/not/a/repo"))
        self.assertIsNone(got["branch"])
        self.assertIsNone(got["commit"])

    def test_a_git_worktree_is_identified_as_one(self):
        # A session worktree is disposable; pinning logon startup to it means
        # the tray dies, or silently falls back to older code, when it is
        # cleaned up.
        got = ig.describe_checkout(Path.cwd())
        self.assertIn("is_worktree", got)


class TestRefusingAStaleCheckout(unittest.TestCase):
    def _desc(self, **over):
        base = {"root": Path("C:/repo"), "branch": "main", "commit": "abc1234",
                "is_worktree": False, "behind": 0}
        base.update(over)
        return base

    def test_a_current_main_checkout_raises_nothing(self):
        self.assertIsNone(ig.stale_checkout_warning(self._desc()))

    def test_a_checkout_behind_the_remote_is_flagged(self):
        warning = ig.stale_checkout_warning(self._desc(behind=26))
        self.assertIsNotNone(warning)
        self.assertIn("26", warning)

    def test_a_non_main_branch_is_flagged(self):
        warning = ig.stale_checkout_warning(self._desc(branch="master"))
        self.assertIsNotNone(warning)
        self.assertIn("master", warning)

    def test_a_worktree_is_flagged_as_disposable(self):
        warning = ig.stale_checkout_warning(self._desc(is_worktree=True))
        self.assertIsNotNone(warning)
        self.assertIn("worktree", warning.lower())

    def test_an_unknown_checkout_is_flagged_rather_than_assumed_fine(self):
        warning = ig.stale_checkout_warning(
            self._desc(branch=None, commit=None))
        self.assertIsNotNone(warning)

    def test_the_warning_names_the_directory_so_it_can_be_acted_on(self):
        warning = ig.stale_checkout_warning(
            self._desc(branch="master", root=Path("C:/old/checkout")))
        self.assertIn("old", warning)


class TestStoppingTheIncumbent(unittest.TestCase):
    """The installer started a second tray without stopping the first.

    The new tray's port guard then refused - correctly - but it refused via a
    blocking dialog that an unattended deploy cannot answer, so the deploy hung
    while the OLD binary carried on serving. Recorded as a gotcha after it
    happened twice; this is the fix.
    """

    def test_nothing_to_stop_is_success_not_failure(self):
        stopped = ig.stop_tray_processes(
            pids=[], _kill=lambda pid: None)
        self.assertEqual(stopped, [])

    def test_a_running_tray_is_stopped(self):
        killed = []
        stopped = ig.stop_tray_processes(
            pids=[4242], _kill=killed.append)
        self.assertEqual(killed, [4242])
        self.assertEqual(stopped, [4242])

    def test_a_process_that_is_already_gone_is_not_an_error(self):
        def boom(pid):
            raise ProcessLookupError(pid)

        self.assertEqual(ig.stop_tray_processes(pids=[7], _kill=boom), [])

    def test_every_owner_is_stopped_not_just_the_first(self):
        killed = []
        ig.stop_tray_processes(pids=[1, 2, 3], _kill=killed.append)
        self.assertEqual(killed, [1, 2, 3])

    def test_one_failure_does_not_abandon_the_rest(self):
        killed = []

        def flaky(pid):
            if pid == 2:
                raise PermissionError(pid)
            killed.append(pid)

        stopped = ig.stop_tray_processes(pids=[1, 2, 3], _kill=flaky)
        self.assertEqual(killed, [1, 3])
        self.assertEqual(stopped, [1, 3])


if __name__ == "__main__":
    unittest.main()
