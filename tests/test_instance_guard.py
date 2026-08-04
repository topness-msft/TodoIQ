"""Tests for the tray instance guard.

Written after a live incident on 2026-08-04: the dogfood silently rolled back
because a second tray was launched from a different checkout of the same repo.

The tray computes ``PROJECT_ROOT`` from ``__file__``, so its database, log and
PID file are all relative to *whichever copy of the script was run*. The
single-instance guard is therefore per-checkout, and two checkouts competing for
port 8766 cannot see each other. The user gets no signal at all: the wrong code
serves a different database on the expected URL.

The guard makes that visible instead of silent.
"""

import unittest
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from src.services.instance_guard import (
    checkout_mismatch,
    parse_registered_script,
    port_owner_message,
)


WORKTREE = Path(r"C:\Users\p\copilot\projects\copilot-worktrees\X\scripts\todoness_tray.pyw")
MAIN = Path(r"C:\Users\p\claude\projects\ClaudeTodo\scripts\todoness_tray.pyw")


class TestParseRegisteredScript(unittest.TestCase):
    """The canonical instance is whatever the scheduled task launches.

    Deriving it rather than hard-coding a path means the guard keeps working if
    the deployment target ever moves.
    """

    def test_extracts_quoted_script_path(self):
        args = (
            '"C:\\Users\\p\\copilot\\projects\\copilot-worktrees\\X'
            '\\scripts\\todoness_tray.pyw"'
        )
        self.assertEqual(parse_registered_script(args), WORKTREE)

    def test_extracts_unquoted_script_path(self):
        args = r"C:\Users\p\claude\projects\ClaudeTodo\scripts\todoness_tray.pyw"
        self.assertEqual(parse_registered_script(args), MAIN)

    def test_ignores_the_interpreter_and_finds_the_script(self):
        args = r'"C:\Python\pythonw.exe" "C:\Users\p\claude\projects\ClaudeTodo\scripts\todoness_tray.pyw"'
        self.assertEqual(parse_registered_script(args), MAIN)

    def test_returns_none_when_no_script_present(self):
        for args in ("", None, "   ", "notepad.exe"):
            with self.subTest(args=args):
                self.assertIsNone(parse_registered_script(args))


class TestCheckoutMismatch(unittest.TestCase):
    def test_no_warning_when_paths_match(self):
        self.assertIsNone(checkout_mismatch(WORKTREE, WORKTREE))

    def test_no_warning_when_nothing_is_registered(self):
        # Never nag a user who has not installed the startup task.
        self.assertIsNone(checkout_mismatch(MAIN, None))

    def test_match_is_case_insensitive_on_windows(self):
        shouty = Path(str(WORKTREE).upper())
        self.assertIsNone(checkout_mismatch(shouty, WORKTREE))

    def test_warns_when_launched_from_another_checkout(self):
        msg = checkout_mismatch(MAIN, WORKTREE)
        self.assertIsNotNone(msg)
        self.assertIn(str(MAIN), msg)
        self.assertIn(str(WORKTREE), msg)

    def test_warning_states_the_real_consequence(self):
        # The incident was invisible precisely because nothing said "different
        # database". Naming that is the whole point of the dialog.
        msg = checkout_mismatch(MAIN, WORKTREE)
        self.assertIn("database", msg.lower())

    def test_warning_tells_the_user_the_right_way(self):
        msg = checkout_mismatch(MAIN, WORKTREE)
        self.assertIn("schtasks /run /tn TodoNess", msg)


class TestPortOwnerMessage(unittest.TestCase):
    def test_none_when_port_is_free(self):
        self.assertIsNone(port_owner_message(8766, probe=lambda p: False))

    def test_message_when_port_is_taken(self):
        msg = port_owner_message(8766, probe=lambda p: True)
        self.assertIsNotNone(msg)
        self.assertIn("8766", msg)

    def test_message_mentions_another_instance(self):
        msg = port_owner_message(8766, probe=lambda p: True)
        self.assertIn("already", msg.lower())

    def test_probe_failure_is_not_treated_as_in_use(self):
        # Failing open would block a legitimate start; the guard must never be
        # the reason TodoNess will not run.
        def boom(_):
            raise OSError("no socket for you")

        self.assertIsNone(port_owner_message(8766, probe=boom))


if __name__ == "__main__":
    unittest.main()
