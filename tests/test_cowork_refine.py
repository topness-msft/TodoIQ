"""Refine: a follow-up turn on the SAME Cowork conversation.

Today every Redo starts a BRAND NEW conversation. `_api_run_default` mints a
fresh `cw-<uuid>` per run, so a correction as small as "make it shorter"
re-researches M365 from zero. Measured on real runs: 27s to 6 minutes, and 69
to 355 credits EVERY TIME.

Continuing an existing conversation was verified against the live runtime: a
follow-up posted to an existing conversationId retained the recipients and
content from the earlier turn and completed in ~30s. So refine is roughly an
order of magnitude cheaper and faster than Redo.

Shape per the architect ruling (findings/architect-interactive-refine.md):

  continue_preview()      separate entry point. start_preview, _api_run_default
                          and compose_prompt are NOT touched - threading a
                          resume flag through a 2100-line high-blast-radius
                          module was rejected.
  compose_refine_prompt() minimal. The conversation already holds [TASK],
                          [SOURCE], [VOICE], [INTENT]. Re-sending them wastes
                          tokens and risks stacking conflicting corrections.

SAFETY: the barrier travels in the request body PER TURN, so a refine turn must
build and send its own callback config. Forgetting that would send an
unbarriered turn. That is the single most important test in this file.
"""

import json
import shutil
import tempfile
import unittest
from unittest import mock

from src.services import cowork_runner as cr
from src.services.cowork_runner import compose_refine_prompt


class TestRefinePromptIsMinimal(unittest.TestCase):
    def test_it_carries_the_instruction_verbatim(self):
        prompt = compose_refine_prompt("Make it shorter and aim it just at Greg")
        self.assertIn("Make it shorter and aim it just at Greg", prompt)

    def test_it_does_not_resend_the_full_task_context(self):
        """The conversation already has it; re-sending stacks corrections."""
        prompt = compose_refine_prompt("shorter")
        for section in ("[TASK]", "[SOURCE]", "[VOICE]", "[INTENT]"):
            with self.subTest(section=section):
                self.assertNotIn(section, prompt)

    def test_it_restates_the_output_contract(self):
        """Q6 risk: a free-form instruction can make Cowork answer in prose,
        `_extract_draft` then finds nothing and the card renders blank."""
        prompt = compose_refine_prompt("what did Brandon originally write?")
        self.assertIn("[OUTPUT]", prompt)

    def test_it_restates_the_safety_block(self):
        """Decoration, not the control - but free. The real barrier is the
        per-request toolCallbackConfig."""
        prompt = compose_refine_prompt("shorter")
        self.assertIn("do not send", prompt.lower())

    def test_an_empty_instruction_is_rejected(self):
        for bad in ("", "   ", None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    compose_refine_prompt(bad)

    def test_it_is_far_shorter_than_a_full_prompt(self):
        task = {"id": 1, "title": "Follow up with Greg",
                "coaching_text": "ask about the nomination",
                "key_people": '[{"name":"Greg","email":"g@x.com"}]',
                "source_type": "chat"}
        full = cr.compose_prompt(task)
        refine = compose_refine_prompt("make it shorter")
        self.assertLess(len(refine), len(full) / 2)


class TestContinuePreview(unittest.TestCase):
    def setUp(self):
        cr.reset_registry()
        self.addCleanup(cr.reset_registry)
        self._cost = cr._cost_snapshot_fn
        cr._cost_snapshot_fn = lambda: None
        self.addCleanup(lambda: setattr(cr, "_cost_snapshot_fn", self._cost))
        self._pre = cr.tenant_barrier_precheck
        cr.tenant_barrier_precheck = lambda **k: {"status": "ok", "reason": ""}
        self.addCleanup(lambda: setattr(cr, "tenant_barrier_precheck", self._pre))
        self.tmp = tempfile.mkdtemp(prefix="cw-refine-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.seen = {}

        def runner(prompt, config, on_progress, conversation_id=None, is_follow_up=None):
            self.seen["prompt"] = prompt
            self.seen["config"] = config
            self.seen["conversation_id"] = conversation_id
            return {"terminal_status": "ok", "text": "revised",
                    "sse_events": [], "tool_trace": [],
                    "conversation_id": conversation_id,
                    "callback_exchanges": [], "duration_seconds": None}

        self.runner = runner

    def _run(self, cid="t:u:cw-abc", instruction="make it shorter"):
        with mock.patch.object(cr, "_api_run_fn", self.runner):
            label = cr.continue_preview(4242, cid, instruction, log_dir=self.tmp)
            cr.wait_for(label, timeout=10)
        return label

    def test_it_reuses_the_conversation_id(self):
        """The whole point: context is retained, so no re-research."""
        self._run(cid="t:u:cw-keepme")
        self.assertEqual(self.seen["conversation_id"], "t:u:cw-keepme")

    def test_it_sends_the_write_barrier_on_the_follow_up_turn(self):
        """SAFETY-CRITICAL. The barrier is per-request, so a turn without it is
        an unbarriered turn."""
        self._run()
        self.assertGreater(len(self.seen["config"]["tool_names"]), 100)
        self.assertTrue(self.seen["config"]["static_results"])

    def test_it_sends_the_minimal_prompt(self):
        self._run(instruction="aim it just at Greg")
        self.assertIn("aim it just at Greg", self.seen["prompt"])
        self.assertNotIn("[VOICE]", self.seen["prompt"])

    def test_the_result_shape_matches_every_other_run(self):
        label = self._run()
        result = cr.get_result(label)
        self.assertEqual(
            sorted(result.keys()),
            ["auth_failed", "cost_credits", "error", "exit_code", "stderr", "stdout"],
        )
        self.assertIsNone(result["error"])

    def test_a_blank_conversation_id_is_refused(self):
        with self.assertRaises(ValueError):
            cr.continue_preview(1, "", "shorter", log_dir=self.tmp)

    def test_it_refuses_when_a_run_is_already_in_flight(self):
        cr._runs[cr.preview_label(4242)] = {
            "proc": None, "thread": None, "result": None,
            "progress": __import__("collections").deque(maxlen=10),
        }
        with self.assertRaises(cr.AlreadyRunning):
            cr.continue_preview(4242, "t:u:cw-x", "shorter", log_dir=self.tmp)

    def test_a_failure_is_reported_not_swallowed(self):
        def boom(prompt, config, on_progress, conversation_id=None, is_follow_up=None):
            raise OSError("island unreachable")

        with mock.patch.object(cr, "_api_run_fn", boom):
            label = cr.continue_preview(99, "t:u:cw-x", "shorter", log_dir=self.tmp)
            cr.wait_for(label, timeout=10)
        self.assertTrue(cr.get_result(label)["error"])


class TestSubprocessCannotResume(unittest.TestCase):
    """Continuation exists only on the API transport. The CLI has no --resume
    and its stdout carries no conversation id, so a subprocess-produced row has
    nothing to continue from. The UI gates on conversation_id for exactly this
    reason."""

    def test_a_row_without_a_conversation_id_cannot_be_refined(self):
        with self.assertRaises(ValueError):
            cr.continue_preview(1, None, "shorter")


if __name__ == "__main__":
    unittest.main()
