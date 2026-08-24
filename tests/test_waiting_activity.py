"""The waiting_activity contract: what a check found, and whether it ran.

Riveter's animating rule is that it must never claim something was verified
when it was not. `/waiting-check` currently breaks that rule by omission: when
WorkIQ errors it skips the task entirely and writes nothing
(.claude/commands/waiting-check.md:79), so the card keeps showing the previous
result. "I could not look" and "I looked and there was nothing" are rendered
identically, and the older answer is presented as though it were current.

These tests pin the contract that makes the difference visible, and pin the
honesty rules around the two signals the dashboard shows:

  - relevant new activity      (the person moved on this)
  - looks done                 (an inference, never a verified completion)

The third state - could not check - is what is missing today.
"""

import json
import os
import tempfile
import unittest

from src.services import waiting_activity as wa


class TestParsingLegacyRows(unittest.TestCase):
    """Every row in the live database predates v2, so v1 must keep working."""

    def test_none_and_blank_are_absent_not_errors(self):
        for raw in (None, "", "   "):
            with self.subTest(raw=raw):
                self.assertIsNone(wa.normalise(raw))

    def test_malformed_json_is_absent_not_an_exception(self):
        # A half-written row must not take the dashboard down.
        self.assertIsNone(wa.normalise("{not json"))

    def test_non_object_json_is_absent(self):
        for raw in ("[]", '"a string"', "42"):
            with self.subTest(raw=raw):
                self.assertIsNone(wa.normalise(raw))

    def test_legacy_v1_fields_are_preserved(self):
        raw = json.dumps({
            "status": "activity_detected",
            "summary": "Jason replied on Tuesday",
            "checked_at": "2026-08-20T10:00:00Z",
        })
        got = wa.normalise(raw)
        self.assertEqual(got["status"], "activity_detected")
        self.assertEqual(got["summary"], "Jason replied on Tuesday")
        self.assertEqual(got["checked_at"], "2026-08-20T10:00:00Z")

    def test_legacy_v1_is_upgraded_to_the_current_version(self):
        got = wa.normalise(json.dumps({"status": "no_activity", "summary": "s"}))
        self.assertEqual(got["version"], wa.SCHEMA_VERSION)

    def test_legacy_row_without_check_state_is_treated_as_a_completed_check(self):
        # v1 only ever wrote on success, so absence of check_state means it ran.
        got = wa.normalise(json.dumps({"status": "no_activity", "summary": "s"}))
        self.assertEqual(got["check_state"], wa.CHECK_OK)

    def test_legacy_out_of_office_keeps_its_return_date(self):
        raw = json.dumps({
            "status": "out_of_office",
            "summary": "Back Monday",
            "return_date": "2026-08-31",
            "checked_at": "2026-08-24T09:00:00Z",
        })
        self.assertEqual(wa.normalise(raw)["return_date"], "2026-08-31")

    def test_a_dict_is_accepted_as_well_as_a_json_string(self):
        got = wa.normalise({"status": "no_activity", "summary": "s"})
        self.assertEqual(got["status"], "no_activity")


class TestProducerAttribution(unittest.TestCase):
    """Three commands write this one column with different vocabularies.

    waiting-check   out_of_office | no_activity | activity_detected | may_be_resolved
    suggestion-check likely_resolved | still_pending | unclear
    todo-parse      out_of_office only

    Without a discriminator a reader cannot tell whose vocabulary it is holding.
    """

    def test_recorded_producer_is_kept(self):
        got = wa.normalise({"status": "still_pending", "producer": "suggestion-check"})
        self.assertEqual(got["producer"], wa.PRODUCER_SUGGESTION_CHECK)

    def test_suggestion_vocabulary_is_attributed_when_unlabelled(self):
        for status in ("likely_resolved", "still_pending", "unclear"):
            with self.subTest(status=status):
                got = wa.normalise({"status": status})
                self.assertEqual(got["producer"], wa.PRODUCER_SUGGESTION_CHECK)

    def test_waiting_vocabulary_is_attributed_when_unlabelled(self):
        for status in ("no_activity", "activity_detected", "may_be_resolved"):
            with self.subTest(status=status):
                got = wa.normalise({"status": status})
                self.assertEqual(got["producer"], wa.PRODUCER_WAITING_CHECK)

    def test_out_of_office_is_not_attributed_to_a_guess(self):
        # Both waiting-check and todo-parse write out_of_office, so an
        # unlabelled OOO row genuinely cannot be attributed. Saying so beats
        # picking the likelier one.
        self.assertIsNone(wa.normalise({"status": "out_of_office"})["producer"])


class TestCheckFailureIsRecorded(unittest.TestCase):
    """The gap this whole change exists to close."""

    def test_a_failure_can_be_expressed(self):
        got = wa.normalise({
            "check_state": "failed",
            "error": "WorkIQ returned no readable output",
            "checked_at": "2026-08-24T09:00:00Z",
        })
        self.assertEqual(got["check_state"], wa.CHECK_FAILED)
        self.assertEqual(got["error"], "WorkIQ returned no readable output")

    def test_a_failure_does_not_invent_a_status(self):
        got = wa.normalise({"check_state": "failed", "error": "timed out"})
        self.assertIsNone(got["status"])

    def test_a_failed_recheck_keeps_the_previous_finding_but_marks_it_stale(self):
        # Retaining the old answer is useful; presenting it as current is not.
        got = wa.normalise({
            "check_state": "failed",
            "error": "timed out",
            "previous": {"status": "activity_detected", "summary": "replied Tue",
                         "checked_at": "2026-08-20T10:00:00Z"},
        })
        self.assertEqual(got["check_state"], wa.CHECK_FAILED)
        self.assertEqual(got["previous"]["status"], "activity_detected")
        self.assertIsNone(got["status"])


class TestSignalForTheDashboard(unittest.TestCase):
    """One place decides which of the signals a card is entitled to show."""

    def test_no_row_means_unchecked(self):
        self.assertEqual(wa.signal_for("waiting", None), wa.SIGNAL_UNCHECKED)

    def test_activity_detected_is_the_new_activity_signal(self):
        got = wa.normalise({"status": "activity_detected", "summary": "s"})
        self.assertEqual(wa.signal_for("waiting", got), wa.SIGNAL_ACTIVITY)

    def test_may_be_resolved_is_the_looks_done_signal(self):
        got = wa.normalise({"status": "may_be_resolved", "summary": "s"})
        self.assertEqual(wa.signal_for("waiting", got), wa.SIGNAL_LOOKS_DONE)

    def test_no_activity_is_quiet_not_missing(self):
        got = wa.normalise({"status": "no_activity", "summary": "s"})
        self.assertEqual(wa.signal_for("waiting", got), wa.SIGNAL_QUIET)

    def test_out_of_office_is_its_own_signal(self):
        got = wa.normalise({"status": "out_of_office", "summary": "s"})
        self.assertEqual(wa.signal_for("waiting", got), wa.SIGNAL_OUT_OF_OFFICE)

    def test_a_failed_check_never_reports_a_finding(self):
        got = wa.normalise({
            "check_state": "failed",
            "error": "no output",
            "previous": {"status": "may_be_resolved", "summary": "looked done"},
        })
        # The strongest claim in the row is "looks done", and it must not be
        # what the card shows: nobody checked.
        self.assertEqual(wa.signal_for("waiting", got), wa.SIGNAL_CHECK_FAILED)

    def test_suggestion_rows_do_not_drive_the_waiting_signals(self):
        # /suggestion-check shares the column. Its vocabulary must not be read
        # as waiting activity on a waiting card.
        for status in ("likely_resolved", "still_pending", "unclear"):
            with self.subTest(status=status):
                got = wa.normalise({"status": status, "producer": "suggestion-check"})
                self.assertEqual(wa.signal_for("waiting", got), wa.SIGNAL_NONE)

    def test_an_unknown_status_is_not_guessed_at(self):
        got = wa.normalise({"status": "something_new", "summary": "s"})
        self.assertEqual(wa.signal_for("waiting", got), wa.SIGNAL_NONE)


class TestSourceScope(unittest.TestCase):
    """Whether the originating thread was read, or only the person.

    Carried from the first version even though thread replay is not built yet:
    a card that says "no reply on this thread" is making a much stronger claim
    than one that says "nothing from this person anywhere", and the reader has
    no way to tell them apart unless the check records which it did.
    """

    def test_person_fallback_is_the_honest_default(self):
        got = wa.normalise({"status": "no_activity", "summary": "s"})
        self.assertEqual(got["source_scope"], wa.SCOPE_PERSON)

    def test_a_thread_scoped_check_can_say_so(self):
        got = wa.normalise({
            "status": "no_activity",
            "summary": "s",
            "source_scope": "thread",
            "conversation_id": "19:meeting_abc@thread.v2",
        })
        self.assertEqual(got["source_scope"], wa.SCOPE_THREAD)
        self.assertEqual(got["conversation_id"], "19:meeting_abc@thread.v2")

    def test_an_unrecognised_scope_falls_back_to_person(self):
        got = wa.normalise({"status": "no_activity", "source_scope": "telepathy"})
        self.assertEqual(got["source_scope"], wa.SCOPE_PERSON)


class TestEvidence(unittest.TestCase):
    """A summary alone is an assertion. Evidence is what makes it checkable."""

    def test_evidence_defaults_to_empty_not_missing(self):
        got = wa.normalise({"status": "no_activity", "summary": "s"})
        self.assertEqual(got["evidence"], [])

    def test_evidence_entries_are_kept_in_order(self):
        got = wa.normalise({
            "status": "activity_detected",
            "summary": "two replies",
            "evidence": [
                {"excerpt": "first", "when": "2026-08-21T09:00:00Z", "where": "Teams"},
                {"excerpt": "second", "when": "2026-08-22T09:00:00Z", "where": "Teams"},
            ],
        })
        self.assertEqual([e["excerpt"] for e in got["evidence"]], ["first", "second"])

    def test_evidence_entries_have_a_stable_key_set(self):
        # The renderer destructures these unconditionally.
        got = wa.normalise({
            "status": "activity_detected",
            "evidence": [{"excerpt": "only this"}],
        })
        self.assertEqual(
            set(got["evidence"][0]),
            {"excerpt", "when", "where", "url"},
        )

    def test_junk_evidence_is_discarded_rather_than_half_rendered(self):
        got = wa.normalise({
            "status": "activity_detected",
            "evidence": ["a bare string", None, 42, {"excerpt": "kept"}],
        })
        self.assertEqual([e["excerpt"] for e in got["evidence"]], ["kept"])

    def test_evidence_that_is_not_a_list_is_discarded(self):
        got = wa.normalise({"status": "activity_detected", "evidence": "nope"})
        self.assertEqual(got["evidence"], [])


class TestCheckCursor(unittest.TestCase):
    """`updated_at` cannot be the cursor: the checks themselves move it.

    waiting-check.md:106,109 set updated_at on every write, so "activity since
    last update" would mean "since the last time I looked", which silently
    shrinks the window each run.
    """

    def test_check_since_is_carried_when_given(self):
        got = wa.normalise({"status": "no_activity", "check_since": "2026-08-01T00:00:00Z"})
        self.assertEqual(got["check_since"], "2026-08-01T00:00:00Z")

    def test_check_since_is_absent_rather_than_assumed(self):
        got = wa.normalise({"status": "no_activity"})
        self.assertIsNone(got["check_since"])

    def test_next_cursor_prefers_the_last_successful_check(self):
        got = wa.normalise({
            "status": "no_activity",
            "checked_at": "2026-08-20T10:00:00Z",
        })
        self.assertEqual(
            wa.next_check_since(got, created_at="2026-08-01T00:00:00Z"),
            "2026-08-20T10:00:00Z",
        )

    def test_next_cursor_falls_back_to_creation_when_never_checked(self):
        self.assertEqual(
            wa.next_check_since(None, created_at="2026-08-01T00:00:00Z"),
            "2026-08-01T00:00:00Z",
        )

    def test_a_failed_check_does_not_advance_the_cursor(self):
        # Otherwise the window skips exactly the period nobody managed to read.
        got = wa.normalise({
            "check_state": "failed",
            "error": "no output",
            "checked_at": "2026-08-24T09:00:00Z",
            "check_since": "2026-08-01T00:00:00Z",
        })
        self.assertEqual(
            wa.next_check_since(got, created_at="2026-07-01T00:00:00Z"),
            "2026-08-01T00:00:00Z",
        )


class TestProducerReaderContract(unittest.TestCase):
    """The shape `/waiting-check` writes must be the shape this module reads.

    The command builds its JSON inline in a bash `python -c` snippet and cannot
    import this module, so nothing but a test holds the two together. These
    mirror the payloads in .claude/commands/waiting-check.md Step 4 exactly.
    """

    def test_a_successful_thread_scoped_check_round_trips(self):
        written = {
            "version": 2,
            "producer": "waiting-check",
            "check_state": "ok",
            "checked_at": "2026-08-24T09:00:00Z",
            "check_since": "2026-08-20T10:00:00Z",
            "source_scope": "thread",
            "status": "activity_detected",
            "summary": "Jason replied twice",
            "evidence": [{"excerpt": "Sending now", "when": "2026-08-21T09:00:00Z",
                          "where": "Teams", "url": None}],
            "conversation_id": "19:abc@thread.v2",
        }
        got = wa.normalise(json.dumps(written))
        self.assertEqual(wa.signal_for("waiting", got), wa.SIGNAL_ACTIVITY)
        self.assertEqual(got["source_scope"], wa.SCOPE_THREAD)
        self.assertEqual(got["producer"], wa.PRODUCER_WAITING_CHECK)
        self.assertEqual(got["evidence"][0]["excerpt"], "Sending now")
        self.assertEqual(got["conversation_id"], "19:abc@thread.v2")

    def test_a_failed_check_round_trips_without_a_finding(self):
        written = {
            "version": 2,
            "producer": "waiting-check",
            "check_state": "failed",
            "checked_at": "2026-08-24T09:00:00Z",
            "check_since": "2026-08-20T10:00:00Z",
            "source_scope": "person",
            "error": "WorkIQ returned no readable output",
            "previous": {"status": "activity_detected", "summary": "replied Tue",
                         "checked_at": "2026-08-20T10:00:00Z"},
        }
        got = wa.normalise(json.dumps(written))
        self.assertEqual(wa.signal_for("waiting", got), wa.SIGNAL_CHECK_FAILED)
        self.assertIsNone(got["status"])
        self.assertEqual(got["previous"]["summary"], "replied Tue")
        # And the window nobody managed to read is not skipped next time.
        self.assertEqual(
            wa.next_check_since(got, created_at="2026-01-01T00:00:00Z"),
            "2026-08-20T10:00:00Z",
        )

    def test_an_out_of_office_check_round_trips(self):
        written = {
            "version": 2, "producer": "waiting-check", "check_state": "ok",
            "checked_at": "2026-08-24T09:00:00Z", "check_since": None,
            "source_scope": "person", "status": "out_of_office",
            "summary": "Back on the 31st", "return_date": "2026-08-31",
        }
        got = wa.normalise(json.dumps(written))
        self.assertEqual(wa.signal_for("waiting", got), wa.SIGNAL_OUT_OF_OFFICE)
        self.assertEqual(got["return_date"], "2026-08-31")


class TestSuggestionCheckContract(unittest.TestCase):
    """/suggestion-check shares this column with its own vocabulary.

    It was attributed by inference - "likely_resolved must mean the suggestion
    checker" - which works only while the two vocabularies stay disjoint. An
    inference is not evidence, so the command now states who it is, and the
    inference stays only as a fallback for rows written before it did.

    It also had the same honesty gap waiting-check had: on a WorkIQ error it
    skipped the task entirely (suggestion-check.md:74), so the badge kept
    showing the previous verdict at its previous timestamp.
    """

    def test_a_labelled_suggestion_result_round_trips(self):
        written = json.dumps({
            "version": 2, "producer": "suggestion-check", "check_state": "ok",
            "checked_at": "2026-08-24T09:00:00Z",
            "status": "likely_resolved",
            "summary": "Aarti confirmed the PUID list was sent",
        })
        got = wa.normalise(written)
        self.assertEqual(got["producer"], wa.PRODUCER_SUGGESTION_CHECK)
        self.assertEqual(got["status"], "likely_resolved")

    def test_a_failed_suggestion_check_round_trips_without_a_verdict(self):
        written = json.dumps({
            "version": 2, "producer": "suggestion-check", "check_state": "failed",
            "checked_at": "2026-08-24T09:00:00Z",
            "error": "WorkIQ returned no readable output",
            "previous": {"status": "still_pending", "summary": "no reply yet",
                         "checked_at": "2026-08-20T10:00:00Z"},
        })
        got = wa.normalise(written)
        self.assertEqual(got["check_state"], wa.CHECK_FAILED)
        self.assertIsNone(got["status"])
        self.assertEqual(got["previous"]["status"], "still_pending")

    def test_a_labelled_suggestion_row_never_drives_a_waiting_card(self):
        # The two vocabularies must not leak across surfaces even when the
        # producer is explicit.
        for status in ("likely_resolved", "still_pending", "unclear"):
            with self.subTest(status=status):
                got = wa.normalise({"producer": "suggestion-check", "status": status})
                self.assertEqual(wa.signal_for("waiting", got), wa.SIGNAL_NONE)

    def test_an_explicit_producer_beats_the_vocabulary_guess(self):
        # A waiting-check row that happens to carry an odd status is still a
        # waiting-check row when it says so.
        got = wa.normalise({"producer": "waiting-check", "status": "no_activity"})
        self.assertEqual(got["producer"], wa.PRODUCER_WAITING_CHECK)


class TestReadPathNormalisation(unittest.TestCase):
    """Legacy rows become v2-shaped on the way out, without a migration.

    The slash-command writes this JSON inline from bash and cannot import
    anything, so the module can only be enforced where the data is READ. Every
    task the dashboard receives therefore carries a derived `waiting_signal`
    alongside the untouched raw column - the raw string still backs the
    existing client-side search and the `json_extract` queries the commands run
    directly against the database.
    """

    def setUp(self):
        import src.db as db_module

        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self._old_path = db_module.DB_PATH
        db_module.DB_PATH = self.tmp.name
        conn = db_module.get_connection()
        db_module.init_db(conn)
        conn.close()

    def tearDown(self):
        import src.db as db_module

        db_module.DB_PATH = self._old_path
        os.unlink(self.tmp.name)

    def _task_with(self, raw):
        from src.models import create_task, get_task, update_task

        task = create_task(title="Waiting on a reply", status="waiting")
        update_task(task["id"], waiting_activity=raw)
        return get_task(task["id"])

    def test_get_task_carries_the_derived_signal(self):
        got = self._task_with(json.dumps({
            "status": "activity_detected", "summary": "Jason replied",
        }))
        self.assertEqual(got["waiting_signal"]["signal"], wa.SIGNAL_ACTIVITY)

    def test_the_raw_column_is_left_exactly_as_written(self):
        raw = json.dumps({"status": "no_activity", "summary": "quiet"})
        got = self._task_with(raw)
        self.assertEqual(got["waiting_activity"], raw)

    def test_a_task_without_a_check_reports_unchecked(self):
        from src.models import create_task, get_task

        task = create_task(title="Never checked", status="waiting")
        got = get_task(task["id"])
        self.assertEqual(got["waiting_signal"]["signal"], wa.SIGNAL_UNCHECKED)

    def test_a_failed_check_does_not_surface_the_previous_finding(self):
        got = self._task_with(json.dumps({
            "check_state": "failed",
            "error": "WorkIQ returned no readable output",
            "previous": {"status": "may_be_resolved", "summary": "looked done"},
        }))
        self.assertEqual(got["waiting_signal"]["signal"], wa.SIGNAL_CHECK_FAILED)
        self.assertIsNone(got["waiting_signal"]["activity"]["status"])

    def test_malformed_stored_json_does_not_break_the_list(self):
        from src.models import create_task, list_tasks, update_task

        task = create_task(title="Corrupt row", status="waiting")
        update_task(task["id"], waiting_activity="{not json")
        rows = [t for t in list_tasks() if t["id"] == task["id"]]
        self.assertEqual(rows[0]["waiting_signal"]["signal"], wa.SIGNAL_UNCHECKED)

    def test_list_tasks_normalises_too(self):
        # get_task and list_tasks used separate row-to-dict paths; the
        # dashboard reads the list one, so it must not be the untreated route.
        from src.models import create_task, list_tasks, update_task

        task = create_task(title="Listed", status="waiting")
        update_task(task["id"], waiting_activity=json.dumps(
            {"status": "may_be_resolved", "summary": "looks done"}))
        rows = [t for t in list_tasks() if t["id"] == task["id"]]
        self.assertEqual(rows[0]["waiting_signal"]["signal"], wa.SIGNAL_LOOKS_DONE)


if __name__ == "__main__":
    unittest.main()
