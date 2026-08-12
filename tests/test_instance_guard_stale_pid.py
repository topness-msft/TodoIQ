"""The installer must not report success when the tray silently failed.

WHAT HAPPENED, THREE DEPLOYS RUNNING
------------------------------------
`install_startup.py` printed "Starting TodoNess tray app... Done." and the tray
was not running. Port 8766 stayed free.

The cause is an interaction, not a single bug:

1. A deploy stops the old tray with `Stop-Process -Force`, so it never runs its
   cleanup and `data/todoness.pid` keeps the dead PID.
2. The new tray starts, sees a PID file, assumes another instance owns the port,
   and exits silently. That single-instance guard is correct and deliberate.
3. The installer `Popen`s the tray and prints success without ever checking
   whether the child survived.

So a failed deploy looked like a successful one. That is the exact class of
problem the instance guard exists to prevent, reintroduced one layer up.

Two fixes, both needed:

- Treat a PID file as stale when that PID is not running, and remove it. A dead
  PID is not an owner.
- Stop printing success unconditionally. Wait briefly for the child and report
  honestly if it did not come up.

Deliberately conservative about what counts as stale: only a PID that is *not
running at all*. If a process with that PID exists we leave the file alone, even
though PIDs can be recycled, because deleting a live instance's lock is worse
than a stale-file warning.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services.instance_guard import (  # noqa: E402
    is_stale_pidfile,
    clear_stale_pidfile,
)


class TestStaleDetection(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.pidfile = os.path.join(self.tmp.name, "todoness.pid")

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, text):
        with open(self.pidfile, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_missing_file_is_not_stale(self):
        """Nothing to clean up is not a failure."""
        self.assertFalse(is_stale_pidfile(self.pidfile))

    def test_a_dead_pid_is_stale(self):
        self._write("999999")
        self.assertTrue(is_stale_pidfile(self.pidfile, _alive=lambda p: False))

    def test_a_live_pid_is_not_stale(self):
        self._write("4242")
        self.assertFalse(is_stale_pidfile(self.pidfile, _alive=lambda p: True))

    def test_our_own_pid_is_not_stale(self):
        """Sanity check against the real liveness probe, not a stub."""
        self._write(str(os.getpid()))
        self.assertFalse(is_stale_pidfile(self.pidfile))

    def test_garbage_contents_count_as_stale(self):
        """An unparseable lock cannot be protecting anything."""
        self._write("not-a-pid")
        self.assertTrue(is_stale_pidfile(self.pidfile))

    def test_an_empty_file_counts_as_stale(self):
        self._write("")
        self.assertTrue(is_stale_pidfile(self.pidfile))

    def test_whitespace_is_tolerated(self):
        self._write("  4242\n")
        self.assertFalse(is_stale_pidfile(self.pidfile, _alive=lambda p: True))

    def test_an_unreadable_file_is_not_assumed_stale(self):
        """Fail safe. If we cannot tell, do not delete someone's lock."""
        self._write("4242")
        with mock.patch(
            "pathlib.Path.read_text", side_effect=PermissionError("locked")
        ):
            self.assertFalse(is_stale_pidfile(self.pidfile))


class TestClearing(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.pidfile = os.path.join(self.tmp.name, "todoness.pid")

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, text):
        with open(self.pidfile, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_a_stale_file_is_removed_and_reported(self):
        self._write("999999")
        self.assertTrue(clear_stale_pidfile(self.pidfile, _alive=lambda p: False))
        self.assertFalse(os.path.exists(self.pidfile))

    def test_a_live_file_is_left_alone(self):
        self._write("4242")
        self.assertFalse(clear_stale_pidfile(self.pidfile, _alive=lambda p: True))
        self.assertTrue(os.path.exists(self.pidfile))

    def test_missing_file_is_a_no_op(self):
        self.assertFalse(clear_stale_pidfile(self.pidfile))

    def test_clearing_never_raises(self):
        """This runs on a deploy path. It must not be able to abort one."""
        self._write("999999")
        with mock.patch("os.remove", side_effect=OSError("busy")):
            clear_stale_pidfile(self.pidfile, _alive=lambda p: False)


if __name__ == "__main__":
    unittest.main()


class TestTheLivenessProbeIsNotDestructive(unittest.TestCase):
    """The bug this class exists for.

    The first version of `_pid_alive` used `os.kill(pid, 0)`, the portable POSIX
    idiom for "does this process exist". On Windows `os.kill` ignores the signal
    and calls TerminateProcess, so the probe KILLS the target. Checking our own
    PID killed the interpreter, which presented as pytest exiting with no output
    at all.

    A liveness check that can kill a process is worse than no check, and this is
    a deploy-path helper.
    """

    def test_probing_our_own_pid_does_not_kill_us(self):
        from src.services.instance_guard import _pid_alive

        self.assertTrue(_pid_alive(os.getpid()))
        # Reaching this line at all is the assertion that matters.
        self.assertTrue(True)

    def test_a_live_child_reads_as_alive_and_survives(self):
        import subprocess
        import sys
        import time

        from src.services.instance_guard import _pid_alive

        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            self.assertTrue(_pid_alive(proc.pid))
            time.sleep(0.3)
            self.assertIsNone(proc.poll(), "the probe terminated the child")
        finally:
            proc.kill()
            proc.wait(timeout=10)

    def test_a_finished_child_reads_as_dead(self):
        import subprocess
        import sys

        from src.services.instance_guard import _pid_alive

        proc = subprocess.Popen(
            [sys.executable, "-c", "pass"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        proc.wait(timeout=15)
        self.assertFalse(_pid_alive(proc.pid))

    def test_a_nonsense_pid_reads_as_dead(self):
        from src.services.instance_guard import _pid_alive

        self.assertFalse(_pid_alive(0))
        self.assertFalse(_pid_alive(-1))
