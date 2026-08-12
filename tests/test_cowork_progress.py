"""Live preview progress, drained from the CLI's stderr while it runs.

WHY THIS EXISTS
---------------
`cowork send --json` already emits human-readable liveness to stderr, default-on
in json mode (cowork_cli/services/send_progress.py; cli/send.py:327). We already
captured it and wrote it to data/logs/cowork_preview_<id>.log, which nothing
read, while the card showed a bare spinner.

Measured across 14 real preview logs: median 119s, p90 224s, max 279s, and 93%
run longer than 60s. So a user watched a dead spinner for about two minutes
typically.

The only reason it was not live is that `_collect` used
`proc.communicate(timeout=660)`, which returns everything at once when the child
exits.

THE DEADLOCK INVARIANT
----------------------
cowork_runner.py carried a load-bearing comment: "communicate() - never wait()
then read(). The naive pattern deadlocks once the child exceeds the OS pipe
buffer, and the spike output was already 21KB."

That warning is about `wait()` with NO concurrent drain. `communicate()` is
itself implemented with one reader thread per pipe. So draining both pipes on
their own threads and then waiting preserves the invariant exactly, and is the
only way to see output before exit.

These tests therefore assert the INVARIANT (a payload far larger than any pipe
buffer completes without hanging) rather than the old implementation detail
(that `communicate` was the method called). Testing the method name would have
locked in the implementation and told us nothing about deadlock.
"""

import os
import subprocess
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.services.cowork_runner as cr  # noqa: E402


# Comfortably beyond the 64KB Windows pipe buffer that the original comment
# warned about, and 10x the 21KB payload that motivated it.
_BIG = 400_000


def _real_proc(stdout_bytes=0, stderr_lines=0, exit_code=0, delay=0.0):
    """A genuine child process. A fake cannot prove absence of deadlock."""
    script = (
        "import sys, time\n"
        f"for i in range({stderr_lines}):\n"
        "    sys.stderr.write('[cowork] streaming - 0:%02d elapsed - step %d\\n' % (i, i))\n"
        "    sys.stderr.flush()\n"
        f"    time.sleep({delay})\n"
        f"sys.stdout.write('x' * {stdout_bytes})\n"
        "sys.stdout.flush()\n"
        f"sys.exit({exit_code})\n"
    )
    return subprocess.Popen(
        [sys.executable, "-u", "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
    )


class TestNoDeadlockOnLargeOutput(unittest.TestCase):
    """Spike 2. The question that gated Phase 1: can we drain live without
    reintroducing the deadlock the original comment warns about?"""

    def test_large_stdout_completes(self):
        proc = _real_proc(stdout_bytes=_BIG, stderr_lines=5)
        out, err, code = cr._drain_process(proc, timeout=60)
        self.assertEqual(len(out), _BIG)
        self.assertEqual(code, 0)

    def test_large_stdout_with_heavy_stderr_completes(self):
        """Both pipes under pressure at once is the actual deadlock shape."""
        proc = _real_proc(stdout_bytes=_BIG, stderr_lines=2000)
        out, err, code = cr._drain_process(proc, timeout=60)
        self.assertEqual(len(out), _BIG)
        self.assertIn("step 1999", err)
        self.assertEqual(code, 0)

    def test_it_actually_finishes_quickly(self):
        """A deadlock would present as the timeout, not as an error."""
        proc = _real_proc(stdout_bytes=_BIG, stderr_lines=500)
        start = time.monotonic()
        cr._drain_process(proc, timeout=60)
        self.assertLess(time.monotonic() - start, 30)


class TestProgressArrivesBeforeExit(unittest.TestCase):
    """The whole point: lines must be visible while the child still runs."""

    def test_callback_fires_during_the_run_not_after(self):
        seen = []
        exited = threading.Event()

        def on_line(line):
            # Record whether the child was still running when this arrived.
            seen.append((line, exited.is_set()))

        proc = _real_proc(stdout_bytes=1000, stderr_lines=6, delay=0.15)

        def drain():
            cr._drain_process(proc, timeout=60, on_stderr_line=on_line)
            exited.set()

        t = threading.Thread(target=drain, daemon=True)
        t.start()
        t.join(timeout=60)

        self.assertTrue(seen, "no progress lines were observed at all")
        during = [s for s in seen if not s[1]]
        self.assertTrue(during, "every line arrived only after the run ended")

    def test_lines_are_delivered_in_order(self):
        seen = []
        proc = _real_proc(stderr_lines=40)
        cr._drain_process(proc, timeout=60, on_stderr_line=seen.append)
        idx = [int(l.split("step ")[1]) for l in seen if "step " in l]
        self.assertEqual(idx, sorted(idx))

    def test_a_raising_callback_cannot_break_the_run(self):
        """A UI-side bug must never take down a preview."""
        def boom(_line):
            raise ValueError("callback bug")

        proc = _real_proc(stdout_bytes=5000, stderr_lines=10)
        out, err, code = cr._drain_process(proc, timeout=60, on_stderr_line=boom)
        self.assertEqual(len(out), 5000)
        self.assertEqual(code, 0)


class TestTimeoutStillKills(unittest.TestCase):
    """The invariant that made the subprocess worth keeping."""

    def test_a_hung_child_is_killed_and_reported(self):
        proc = subprocess.Popen(
            [sys.executable, "-u", "-c", "import time; time.sleep(120)"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            encoding="utf-8", errors="replace",
        )
        start = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            cr._drain_process(proc, timeout=2)
        self.assertLess(time.monotonic() - start, 30)
        proc.kill()
        proc.wait(timeout=10)

    def test_output_before_a_timeout_is_not_lost(self):
        """Whatever the run managed to say should survive the kill."""
        seen = []
        proc = subprocess.Popen(
            [sys.executable, "-u", "-c",
             "import sys,time\n"
             "sys.stderr.write('[cowork] streaming - 0:01 elapsed - alive\\n')\n"
             "sys.stderr.flush()\n"
             "time.sleep(120)\n"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            encoding="utf-8", errors="replace",
        )
        try:
            cr._drain_process(proc, timeout=3, on_stderr_line=seen.append)
        except subprocess.TimeoutExpired:
            pass
        finally:
            proc.kill()
            proc.wait(timeout=10)
        self.assertTrue(any("alive" in s for s in seen))


class TestProgressLineParsing(unittest.TestCase):
    """Only the CLI's own liveness lines are worth showing a user. The update
    banner and stack traces are noise."""

    def test_a_streaming_line_is_kept_and_tidied(self):
        got = cr._progress_text(
            "[cowork] streaming - 1:22 elapsed - Searching your Teams and calendar"
        )
        self.assertIsNotNone(got)
        self.assertNotIn("[cowork]", got)
        self.assertNotIn("elapsed", got)

    def test_the_update_banner_is_dropped(self):
        self.assertIsNone(
            cr._progress_text("Update available: 1.21.92 -> 1.21.97. Run: cowork update")
        )

    def test_the_iex_install_line_is_dropped(self):
        self.assertIsNone(
            cr._progress_text("Or: irm https://aka.ms/cowork/ps1 | iex")
        )

    def test_blank_lines_are_dropped(self):
        self.assertIsNone(cr._progress_text("   \n"))

    def test_raw_tool_lines_are_dropped(self):
        """`tool: mcp__outlook_calendar__FindMeetingTimes` is developer
        gibberish on a user-facing card. The CLI also emits its own human copy
        ("Searching your Teams and calendar"), and a real log carries 35 of
        those against 12 tool lines, so dropping them loses nothing and the
        card stays readable. Per-tool detail belongs in the completed trace."""
        self.assertIsNone(
            cr._progress_text("[cowork] streaming - 0:44 elapsed - tool: tool_search_tool")
        )
        self.assertIsNone(
            cr._progress_text(
                "[cowork] streaming - 1:31 elapsed - "
                "tool: mcp__outlook_calendar__FindMeetingTimes"
            )
        )

    def test_init_lines_survive(self):
        got = cr._progress_text("[cowork] streaming - 0:04 elapsed - init: Ready")
        self.assertIn("Ready", got)

    def test_writing_progress_survives(self):
        got = cr._progress_text("[cowork] streaming - 3:24 elapsed - writing - 971 chars")
        self.assertIn("971 chars", got)

    def test_a_human_status_line_survives(self):
        got = cr._progress_text(
            "[cowork] streaming - 1:22 elapsed - Searching for your training sessions"
        )
        self.assertIn("Searching for your training sessions", got)


if __name__ == "__main__":
    unittest.main()
