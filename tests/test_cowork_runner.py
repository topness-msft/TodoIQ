"""Tests for the Cowork preview subprocess runner.

These tests never invoke the real `cowork` binary. `start_preview()` takes a
`spawn` injection point so the process wiring can be asserted exactly.

The safety assertions here are the point of the file. G1 proved `--deny-tools`
does not stop an M365 write; G1b proved `--tool-callback-config` does; G1c
enumerated the write tools that must appear in it. If any of those
assertions regress, preview mode is silently unprotected.
"""

import io
import json
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.services import cowork_runner as cr  # noqa: E402


class FakeProc:
    """Stand-in for subprocess.Popen.

    Exposes real readable pipes as well as a scripted ``communicate()``, so the
    fake exercises the same drain path as production. Before live progress
    existed, ``_collect`` called ``communicate()`` and a scripted return value
    was enough; it now drains ``stdout``/``stderr`` line by line, so a fake
    without pipes would silently take a different path from the real thing.
    """

    def __init__(self, stdout="", stderr="", returncode=0, raise_timeout=False):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self._raise_timeout = raise_timeout
        self.communicate_calls = []
        self.wait_calls = []
        self.killed = False
        self.waited = False
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)

    def communicate(self, timeout=None):
        self.communicate_calls.append(timeout)
        if self._raise_timeout:
            raise subprocess.TimeoutExpired(cmd="cowork", timeout=timeout)
        return self._stdout, self._stderr

    def wait(self, timeout=None):
        self.waited = True
        self.wait_calls.append(timeout)
        if self._raise_timeout:
            raise subprocess.TimeoutExpired(cmd="cowork", timeout=timeout)
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9
        # A real kill closes the pipes, which unblocks the readers.
        self._raise_timeout = False

    def poll(self):
        return self.returncode


class RunnerTestBase(unittest.TestCase):
    def setUp(self):
        cr.reset_registry()
        # A real cost snapshot is a ~1s network call and _collect runs in
        # hundreds of tests; unmocked it took the suite from 35s to 313s.
        self._base_cost_fn = cr._cost_snapshot_fn
        cr._cost_snapshot_fn = lambda: None
        # Tests must never read the user's real data/settings.json. With
        # `cowork_api_transport` turned on for dogfood, start_preview took the
        # API path and every subprocess test made REAL network calls — one file
        # went from seconds to 275s. Pin the transport; tests that exercise the
        # API path patch this themselves.
        self._base_api_flag = cr.api_transport_enabled
        cr.api_transport_enabled = lambda: False
        self.calls = []
        self._base_auth_login = cr._auth_login_fn
        cr._auth_login_fn = lambda *args, **kwargs: type(
            "Login", (), {"returncode": 1}
        )()

    def tearDown(self):
        cr._cost_snapshot_fn = self._base_cost_fn
        cr.api_transport_enabled = self._base_api_flag
        cr._auth_login_fn = self._base_auth_login
        cr.reset_registry()

    def spawner(self, proc):
        def _spawn(argv, **kwargs):
            self.calls.append({"argv": argv, "kwargs": kwargs})
            return proc

        return _spawn

    def run_preview(self, task_id=2076, prompt="hello", proc=None, **kw):
        proc = proc or FakeProc(stdout="{}")
        label = cr.start_preview(
            task_id, prompt, spawn=self.spawner(proc), log_dir=self.log_dir(), **kw
        )
        cr.wait_for(label, timeout=10)
        return label, proc

    def log_dir(self):
        import tempfile

        if not hasattr(self, "_tmp"):
            self._tmp = tempfile.mkdtemp(prefix="cowork-test-")
        return Path(self._tmp)


# ---------------------------------------------------------------- write tools


class TestWriteToolList(RunnerTestBase):
    def test_list_loads_from_vendored_file(self):
        tools = cr.load_write_tools()
        self.assertGreaterEqual(len(tools), 84)

    def test_cli_1_21_88_write_tools_present(self):
        """G1c rerun additions must not arrive un-intercepted."""
        tools = cr.load_write_tools()
        for tool in (
            "core-RenderSlide",
            "core-render_ui",
            "host-render_ui",
            "skill",
        ):
            self.assertIn(tool, tools)

    def test_teams_send_tool_present(self):
        """The G1c headline. Every Phase 1 target is a Teams chat."""
        self.assertIn("m365_teams-PostMessage", cr.load_write_tools())

    def test_graph_universal_bypass_present(self):
        """graph-CallGraph can perform any M365 write on its own."""
        self.assertIn("graph-CallGraph", cr.load_write_tools())

    def test_schedulers_present(self):
        """These run work AFTER our process exits, escaping interception."""
        tools = cr.load_write_tools()
        self.assertIn("host-SetupScheduledPrompt", tools)
        self.assertIn("host-SetupEventTrigger", tools)

    def test_local_execution_tools_present(self):
        """Retain bash defensively, though G1f proved callbacks do not block it."""
        self.assertIn("bash", cr.load_write_tools())

    def test_fabric_query_retained_as_potential_write(self):
        """T-SQL/KQL query interfaces can carry mutating statements."""
        self.assertIn("fabricdocs-execute_query", cr.load_write_tools())

    def test_outlook_draft_creation_is_denied(self):
        """Phil's ruling: drafts live in our DB, not in the mailbox."""
        self.assertIn("outlook-CreateDraftMessage", cr.load_write_tools())


# ------------------------------------------------------------ callback config


class TestCallbackConfig(RunnerTestBase):
    def test_config_written_to_disk(self):
        path = cr.build_callback_config(2076, log_dir=self.log_dir())
        self.assertTrue(path.exists())

    def test_config_has_proven_schema(self):
        path = cr.build_callback_config(2076, log_dir=self.log_dir())
        cfg = json.loads(path.read_text(encoding="utf-8"))
        for key in ("tool_names", "static_results", "callback_hints", "timeout_seconds"):
            self.assertIn(key, cfg)

    def test_every_write_tool_is_denied(self):
        path = cr.build_callback_config(2076, log_dir=self.log_dir())
        cfg = json.loads(path.read_text(encoding="utf-8"))
        for tool in cr.load_write_tools():
            self.assertIn(tool, cfg["tool_names"], f"{tool} not denied")
            self.assertIn(tool, cfg["static_results"], f"{tool} has no static result")

    def test_bare_aliases_included(self):
        """G1b's working config carried both qualified and bare names, so we do
        not know which form the CLI matches on. Include both."""
        cfg = json.loads(
            cr.build_callback_config(2076, log_dir=self.log_dir()).read_text(
                encoding="utf-8"
            )
        )
        self.assertIn("m365_teams-PostMessage", cfg["tool_names"])
        self.assertIn("PostMessage", cfg["tool_names"])

    def test_static_result_says_blocked_and_do_not_retry(self):
        cfg = json.loads(
            cr.build_callback_config(2076, log_dir=self.log_dir()).read_text(
                encoding="utf-8"
            )
        )
        msg = cfg["static_results"]["m365_teams-PostMessage"]
        self.assertIn("BLOCKED", msg)
        self.assertIn("Nothing was sent", msg)
        self.assertIn("Do not retry", msg)
        # Must also close the "try a different tool" loophole.
        self.assertIn("another tool", msg)

    def test_config_is_utf8(self):
        path = cr.build_callback_config(2076, log_dir=self.log_dir())
        path.read_text(encoding="utf-8")


# ------------------------------------------------------------------- argv


class TestArgv(RunnerTestBase):
    def test_callback_config_flag_always_present(self):
        """The single most important assertion in this file."""
        self.run_preview()
        argv = self.calls[0]["argv"]
        self.assertIn("--tool-callback-config", argv)

    def test_callback_config_flag_points_at_real_file(self):
        self.run_preview()
        argv = self.calls[0]["argv"]
        path = Path(argv[argv.index("--tool-callback-config") + 1])
        self.assertTrue(path.exists())

    def test_deny_tools_not_used(self):
        """G1 proved it is not a control; keeping it would imply false safety."""
        self.run_preview()
        self.assertNotIn("--deny-tools", self.calls[0]["argv"])

    def test_json_output_requested(self):
        self.run_preview()
        self.assertIn("--json", self.calls[0]["argv"])

    def test_prompt_passed_by_file_not_argv(self):
        """Prompts contain newlines and emoji; Windows argv mangles both."""
        self.run_preview(prompt="line one\nline two")
        argv = self.calls[0]["argv"]
        self.assertIn("--prompt-file", argv)
        self.assertNotIn("line one\nline two", argv)

    def test_prompt_file_round_trips_utf8(self):
        """23 real tasks (1.2%) contain characters cp1252 cannot encode."""
        prompt = "next \u2192 step \U0001f4a1 warn \u26a0"
        self.run_preview(prompt=prompt)
        argv = self.calls[0]["argv"]
        path = Path(argv[argv.index("--prompt-file") + 1])
        self.assertEqual(path.read_text(encoding="utf-8"), prompt)

    def test_refs_passed_through(self):
        self.run_preview(refs=["person:a@b.com", "person:c@d.com"])
        argv = self.calls[0]["argv"]
        self.assertEqual(argv.count("--ref"), 2)
        self.assertIn("person:a@b.com", argv)

    def test_no_refs_means_no_ref_flag(self):
        self.run_preview()
        self.assertNotIn("--ref", self.calls[0]["argv"])


# -------------------------------------------------------------- process wiring


class TestProcessWiring(RunnerTestBase):
    def test_label_format(self):
        self.assertEqual(cr.preview_label(2076), "cowork:preview:2076")

    def test_label_never_starts_with_skill(self):
        """A 'skill:' label passes the claude_runner.py:123 guard and would
        write the raw 21KB CLI log into tasks.skill_output."""
        self.assertFalse(cr.preview_label(2076).startswith("skill:"))

    def test_timeout_is_660_not_300(self):
        """claude_runner's 300s default would kill a live Cowork session."""
        _, proc = self.run_preview()
        self.assertEqual(proc.wait_calls, [660])

    def test_pipes_are_drained_concurrently_not_after_exit(self):
        """The anti-deadlock invariant, stated as behaviour rather than as the
        name of the method that used to provide it.

        This replaces an assertion that `communicate()` was called and `wait()`
        was not. That wording locked in the implementation: draining live needs
        `wait()`, so the old test would have failed a correct change while
        proving nothing about deadlock. What actually matters is that both
        pipes are consumed while the child is still running, which is how
        `communicate()` was implemented anyway.

        The real proof lives in test_cowork_progress.py, which runs a genuine
        child emitting 400KB (well past any pipe buffer) with 2000 concurrent
        stderr lines. Here we assert only the wiring.
        """
        _, proc = self.run_preview()
        self.assertTrue(proc.waited, "the child was never waited on")
        # Both pipes were read to EOF rather than left for a post-exit read.
        self.assertEqual(proc.stdout.read(), "")
        self.assertEqual(proc.stderr.read(), "")

    def test_stdout_and_stderr_are_separate_pipes(self):
        self.run_preview()
        kw = self.calls[0]["kwargs"]
        self.assertEqual(kw["stdout"], subprocess.PIPE)
        self.assertEqual(kw["stderr"], subprocess.PIPE)
        self.assertNotEqual(kw["stderr"], subprocess.STDOUT)

    def test_subprocess_forced_to_utf8(self):
        self.run_preview()
        kw = self.calls[0]["kwargs"]
        self.assertEqual(kw["encoding"], "utf-8")

    def test_duplicate_label_does_not_double_spawn(self):
        proc = FakeProc(stdout="{}", raise_timeout=False)

        def slow_spawn(argv, **kwargs):
            self.calls.append({"argv": argv, "kwargs": kwargs})
            return proc

        cr.start_preview(2076, "a", spawn=slow_spawn, log_dir=self.log_dir())
        with self.assertRaises(cr.AlreadyRunning):
            cr.start_preview(2076, "b", spawn=slow_spawn, log_dir=self.log_dir())

    def test_different_tasks_run_in_parallel(self):
        cr.start_preview(1, "a", spawn=self.spawner(FakeProc("{}")), log_dir=self.log_dir())
        cr.start_preview(2, "b", spawn=self.spawner(FakeProc("{}")), log_dir=self.log_dir())
        self.assertEqual(len(self.calls), 2)


# ----------------------------------------------------------------- results


class TestResults(RunnerTestBase):
    def test_stdout_captured(self):
        label, _ = self.run_preview(proc=FakeProc(stdout='{"a": 1}'))
        self.assertEqual(cr.get_result(label)["stdout"], '{"a": 1}')

    def test_exit_code_captured(self):
        label, _ = self.run_preview(proc=FakeProc(stdout="{}", returncode=0))
        self.assertEqual(cr.get_result(label)["exit_code"], 0)

    def test_label_released_after_completion(self):
        label, _ = self.run_preview()
        self.assertFalse(cr.is_running(label))

    def test_can_restart_after_completion(self):
        self.run_preview()
        self.run_preview()  # must not raise AlreadyRunning
        self.assertEqual(len(self.calls), 2)

    def test_timeout_kills_process(self):
        proc = FakeProc(raise_timeout=True)
        label, _ = self.run_preview(proc=proc)
        self.assertTrue(proc.killed)
        self.assertIn("timed out", cr.get_result(label)["error"].lower())

    def test_auth_failure_detected_from_stderr(self):
        """cowork exits 1 with EMPTY stdout and the hint only on stderr."""
        proc = FakeProc(
            stdout="", stderr="Not authenticated. Run: cowork auth login", returncode=1
        )
        label, _ = self.run_preview(proc=proc)
        result = cr.get_result(label)
        self.assertTrue(result["auth_failed"])

    def test_healthy_run_is_not_auth_failure(self):
        label, _ = self.run_preview(proc=FakeProc(stdout="{}"))
        self.assertFalse(cr.get_result(label)["auth_failed"])

    def test_stderr_written_to_log_file(self):
        label, _ = self.run_preview(proc=FakeProc(stdout="{}", stderr="heartbeat\n"))
        log = self.log_dir() / "cowork_preview_2076.log"
        self.assertIn("heartbeat", log.read_text(encoding="utf-8"))

    def test_unknown_label_returns_none(self):
        self.assertIsNone(cr.get_result("cowork:preview:99999"))


class TestAuthRecovery(RunnerTestBase):
    AUTH_ERROR = "Not authenticated. Run: cowork auth login"

    def setUp(self):
        super().setUp()
        self.original_login = cr._auth_login_fn

    def tearDown(self):
        cr._auth_login_fn = self.original_login
        super().tearDown()

    def multi_spawner(self, procs):
        remaining = iter(procs)

        def _spawn(argv, **kwargs):
            self.calls.append({"argv": argv, "kwargs": kwargs})
            return next(remaining)

        return _spawn

    def test_silent_login_success_then_retry_success(self):
        cr._auth_login_fn = lambda *args, **kwargs: type(
            "Login", (), {"returncode": 0}
        )()
        spawn = self.multi_spawner(
            [
                FakeProc(stderr=self.AUTH_ERROR, returncode=1),
                FakeProc(stdout="{}", returncode=0),
            ]
        )

        label = cr.start_preview(2076, "hello", spawn=spawn, log_dir=self.log_dir())
        cr.wait_for(label, timeout=10)
        result = cr.get_result(label)

        self.assertEqual(len(self.calls), 2)
        self.assertFalse(result["auth_failed"])
        self.assertEqual(result["exit_code"], 0)
        self.assertIsNone(result["error"])

    def test_login_failure_preserves_original_error_without_retry(self):
        cr._auth_login_fn = lambda *args, **kwargs: type(
            "Login", (), {"returncode": 1}
        )()
        spawn = self.multi_spawner(
            [FakeProc(stderr=self.AUTH_ERROR, returncode=1)]
        )

        label = cr.start_preview(2076, "hello", spawn=spawn, log_dir=self.log_dir())
        cr.wait_for(label, timeout=10)
        result = cr.get_result(label)

        self.assertEqual(len(self.calls), 1)
        self.assertTrue(result["auth_failed"])
        self.assertIn("cowork auth login", result["error"])

    def test_retry_auth_failure_does_not_attempt_third_preview(self):
        cr._auth_login_fn = lambda *args, **kwargs: type(
            "Login", (), {"returncode": 0}
        )()
        spawn = self.multi_spawner(
            [
                FakeProc(stderr=self.AUTH_ERROR, returncode=1),
                FakeProc(stderr=self.AUTH_ERROR, returncode=1),
            ]
        )

        label = cr.start_preview(2076, "hello", spawn=spawn, log_dir=self.log_dir())
        cr.wait_for(label, timeout=10)
        result = cr.get_result(label)

        self.assertEqual(len(self.calls), 2)
        self.assertTrue(result["auth_failed"])
        self.assertEqual(result["exit_code"], 1)

    def test_non_auth_failure_never_runs_login_or_retry(self):
        login_calls = []
        cr._auth_login_fn = lambda *args, **kwargs: login_calls.append(True)
        spawn = self.multi_spawner([FakeProc(stderr="boom", returncode=1)])

        label = cr.start_preview(2076, "hello", spawn=spawn, log_dir=self.log_dir())
        cr.wait_for(label, timeout=10)

        self.assertEqual(login_calls, [])
        self.assertEqual(len(self.calls), 1)

    def test_concurrent_auth_recovery_is_serialized(self):
        active = 0
        max_active = 0
        state_lock = threading.Lock()

        def login(*args, **kwargs):
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.05)
            with state_lock:
                active -= 1
            return type("Login", (), {"returncode": 1})()

        cr._auth_login_fn = login
        cr.start_preview(
            1,
            "one",
            spawn=self.spawner(FakeProc(stderr=self.AUTH_ERROR, returncode=1)),
            log_dir=self.log_dir(),
        )
        cr.start_preview(
            2,
            "two",
            spawn=self.spawner(FakeProc(stderr=self.AUTH_ERROR, returncode=1)),
            log_dir=self.log_dir(),
        )
        cr.wait_for(cr.preview_label(1), timeout=10)
        cr.wait_for(cr.preview_label(2), timeout=10)

        self.assertEqual(max_active, 1)


class TestIslandResolver(RunnerTestBase):
    def setUp(self):
        super().setUp()
        self.original_probe = getattr(cr, "_ISLAND_PROBE_FN", None)

    def tearDown(self):
        cr._ISLAND_PROBE_FN = self.original_probe
        super().tearDown()

    def test_successful_probe_is_cached_once(self):
        calls = []
        cr._ISLAND_PROBE_FN = lambda: (
            calls.append(True) or "https://ia302.example"
        )

        assert cr.resolve_cowork_island() == "https://ia302.example"
        assert cr.resolve_cowork_island() == "https://ia302.example"
        assert len(calls) == 1

    def test_failed_probe_is_attempted_only_once(self):
        calls = []
        cr._ISLAND_PROBE_FN = lambda: calls.append(True) or None

        assert cr.resolve_cowork_island() is None
        assert cr.resolve_cowork_island() is None
        assert len(calls) == 1

    def test_concurrent_callers_probe_once(self):
        import threading
        import time

        calls = []
        barrier = threading.Barrier(8)

        def probe():
            calls.append(True)
            time.sleep(0.05)
            return "https://ia302.example"

        cr._ISLAND_PROBE_FN = probe
        results = []

        def worker():
            barrier.wait()
            results.append(cr.resolve_cowork_island())

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert calls == [True]
        assert results == ["https://ia302.example"] * 8

    def test_reset_registry_clears_island_cache(self):
        values = iter(["https://first.example", "https://second.example"])
        cr._ISLAND_PROBE_FN = lambda: next(values)

        assert cr.resolve_cowork_island() == "https://first.example"
        cr.reset_registry()
        assert cr.resolve_cowork_island() == "https://second.example"


if __name__ == "__main__":
    unittest.main()
