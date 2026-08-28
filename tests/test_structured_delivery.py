import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import tornado.testing

import src.db as db_module
from src.app import make_app
from src.models import (
    confirm_destination,
    create_structured_execution_action,
    create_task,
    create_task_action,
    get_latest_task_action,
    get_task,
    update_task_action,
)
from src.services import structured_delivery


def _measure(slots, schedules, view_start):
    """One measurement per slot, all sharing a single measured window.

    Production takes a separate narrow reading per slot; these cases were
    written against one wide window, and this keeps them expressing the same
    intent without restating every fixture.
    """
    return [
        {"schedules": schedules, "view_start": view_start}
        for _ in slots
    ]


class StructuredDeliveryTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.original_db_path = db_module.DB_PATH
        db_module.DB_PATH = self.tmp.name
        conn = db_module.get_connection()
        db_module.init_db(conn)
        conn.close()
        # finish_preview measures attendee availability, which spawns a real
        # subprocess against WorkIQ. Tests must never make that call: it hangs
        # the suite and would make results depend on someone's live calendar.
        # Cases that care about verification drive the functions directly or
        # install their own probe.
        self._real_fetch_availability = structured_delivery.fetch_availability
        self.availability_probe_calls = []

        def _no_probe(attendees, slots):
            self.availability_probe_calls.append((list(attendees), list(slots)))
            return None

        structured_delivery.fetch_availability = _no_probe

    def tearDown(self):
        structured_delivery.fetch_availability = self._real_fetch_availability
        db_module.DB_PATH = self.original_db_path
        os.unlink(self.tmp.name)


class TestStructuredDeliveryContract(StructuredDeliveryTestBase):
    def test_preview_command_exposes_only_read_tools(self):
        argv = structured_delivery.preview_command("prompt")

        self.assertIn("--allow-tool=workiq", argv)
        available = next(
            value for value in argv if value.startswith("--available-tools=")
        )
        self.assertIn("workiq-fetch", available)
        self.assertIn("workiq-ask", available)
        self.assertNotIn("workiq-create_entity", available)
        self.assertNotIn("workiq-do_action", available)
        self.assertNotIn("shell", " ".join(argv))

    def test_execute_command_exposes_only_the_channel_write_primitive(self):
        calendar = structured_delivery.execute_command("prompt", "calendar")
        email = structured_delivery.execute_command("prompt", "email")
        teams = structured_delivery.execute_command("prompt", "teams")

        self.assertIn("--available-tools=workiq-create_entity", calendar)
        self.assertIn("--available-tools=workiq-create_entity", teams)
        # Email additionally reads the sent copy back, because /sendMail and
        # /reply return 202 with no body and the alternative is inventing a
        # delivery reference. That is a read tool, not a second write.
        email_tools = next(
            value for value in email if value.startswith("--available-tools=")
        ).split("=", 1)[1].split(",")
        self.assertEqual(
            sorted(email_tools), ["workiq-do_action", "workiq-fetch"]
        )

        write_primitives = {
            "workiq-create_entity", "workiq-do_action",
            "workiq-update_entity", "workiq-delete_entity",
        }
        for argv in (calendar, email, teams):
            tools = next(
                value for value in argv
                if value.startswith("--available-tools=")
            ).split("=", 1)[1].split(",")
            self.assertEqual(
                len(write_primitives.intersection(tools)), 1,
                f"exactly one write primitive expected, got {tools}",
            )
            self.assertNotIn("shell", " ".join(argv))
        # Only email may read; the others stay write-only.
        self.assertNotIn("workiq-fetch", " ".join(calendar))
        self.assertNotIn("workiq-fetch", " ".join(teams))

    def test_marker_requires_matching_correlation_and_delivery_reference(self):
        output = (
            "noise\n<<<RIVETER_RESULT>>>\n"
            '{"correlation_id":"corr-1","phase":"execute","ok":true,'
            '"delivery_ref":"event-42"}\n<<<END_RIVETER_RESULT>>>\n'
        )
        parsed = structured_delivery.parse_result_marker(
            output, correlation_id="corr-1", phase="execute"
        )

        self.assertEqual(parsed["delivery_ref"], "event-42")
        with self.assertRaises(ValueError):
            structured_delivery.parse_result_marker(
                output, correlation_id="corr-2", phase="execute"
            )
        with self.assertRaises(ValueError):
            structured_delivery.parse_result_marker(
                output.replace('"delivery_ref":"event-42"', '"delivery_ref":""'),
                correlation_id="corr-1",
                phase="execute",
                require_delivery_ref=True,
            )

    def test_marker_rejects_duplicate_result_blocks(self):
        block = (
            '<<<RIVETER_RESULT>>>\n{"correlation_id":"corr-1",'
            '"phase":"execute","ok":true,"delivery_ref":"event-42"}'
            "\n<<<END_RIVETER_RESULT>>>"
        )
        with self.assertRaises(ValueError):
            structured_delivery.parse_result_marker(
                block + "\n" + block,
                correlation_id="corr-1",
                phase="execute",
            )

    def test_structured_execution_claim_does_not_require_conversation(self):
        task = create_task(
            "Reply to Sarah",
            action_type="respond-email",
            key_people=json.dumps(
                [{"name": "Sarah Goodwin", "email": "sarah@microsoft.com"}]
            ),
        )
        parent = create_task_action(
            task["id"],
            conversation_id="legacy:cowork:conversation",
            delivery_channel="email",
            destination_ref="sarah@microsoft.com",
            destination_display="Sarah Goodwin",
            destination_confirmed_at="2026-08-20T12:00:00Z",
            structured_payload=json.dumps(
                {
                    "schema_version": 1,
                    "channel": "email",
                    "mode": "reply",
                    "message_id": "message-1",
                    "to": ["sarah@microsoft.com"],
                    "subject": "Re: Project update",
                    "body": "Approved body",
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
        parent = update_task_action(
            parent["id"],
            frozenset({"state", "draft"}),
            state="ready",
            draft="Subject: Re: Project update\n\nApproved body",
        )
        snapshot = {
            "parent_action_id": parent["id"],
            "draft": parent["draft"],
            "destination_ref": parent["destination_ref"],
            "destination_display": parent["destination_display"],
            "delivery_channel": parent["delivery_channel"],
            "destination_confirmed_at": parent["destination_confirmed_at"],
        }

        child = create_structured_execution_action(parent["id"], snapshot)

        self.assertIsNotNone(child)
        self.assertIsNone(child["conversation_id"])
        self.assertEqual(child["state"], "executing")
        self.assertEqual(child["structured_payload"], parent["structured_payload"])
        self.assertIsNone(
            create_structured_execution_action(parent["id"], snapshot)
        )

    def test_result_without_delivery_reference_is_unconfirmed(self):
        task = create_task(
            "Schedule a meeting",
            action_type="schedule-meeting",
            key_people=json.dumps(
                [{"name": "Rima Reyes", "email": "rima@microsoft.com"}]
            ),
        )
        action = create_task_action(
            task["id"],
            delivery_channel="calendar",
            destination_ref="rima@microsoft.com",
            destination_display="Rima Reyes",
            structured_payload=json.dumps(
                {"schema_version": 1, "channel": "calendar"}
            ),
        )
        action = update_task_action(
            action["id"],
            frozenset({"state"}),
            state="executing",
        )

        structured_delivery.finish_execute(
            action["id"],
            stdout=(
                '<<<RIVETER_RESULT>>>\n{"correlation_id":"wrong",'
                '"phase":"execute","ok":true,"delivery_ref":"event-1"}'
                "\n<<<END_RIVETER_RESULT>>>"
            ),
            stderr="",
            exit_code=0,
            correlation_id="expected",
        )

        latest = get_latest_task_action(task["id"])
        self.assertEqual(latest["state"], "execute_unconfirmed")
        self.assertIsNone(latest["workiq_delivery_ref"])

    def test_empty_success_output_is_unconfirmed(self):
        task = create_task("Send a Teams message", action_type="follow-up")
        action = create_task_action(
            task["id"],
            delivery_channel="teams",
            destination_ref="chat-1",
            destination_display="Project chat",
            structured_payload=json.dumps(
                {"schema_version": 1, "channel": "teams", "body": "Approved"}
            ),
        )
        update_task_action(
            action["id"], frozenset({"state"}), state="executing"
        )

        structured_delivery.finish_execute(
            action["id"],
            stdout="",
            stderr="",
            exit_code=0,
            correlation_id="corr-1",
        )

        latest = get_latest_task_action(task["id"])
        self.assertEqual(latest["state"], "execute_unconfirmed")
        self.assertIn("check the destination", latest["error"].lower())

    def test_calendar_preview_rejects_duration_drift(self):
        task = create_task(
            "Schedule a 25-minute review",
            action_type="schedule-meeting",
            key_people=json.dumps(
                [{"name": "Rima Reyes", "email": "rima@microsoft.com"}]
            ),
        )
        envelope = structured_delivery.initial_payload(task, "calendar")
        action = create_task_action(
            task["id"],
            delivery_channel="calendar",
            structured_payload=json.dumps(envelope),
        )
        payload = {
            "schema_version": 1,
            "channel": "calendar",
            "subject": "Review",
            "body": "Agenda",
            "duration_minutes": 30,
            "attendees": [
                {"name": "Rima Reyes", "email": "rima@microsoft.com"}
            ],
            "timezone": "America/Los_Angeles",
            "slots": [
                {
                    "id": "0",
                    "label": "Monday at 9:05 AM",
                    "start": "2028-08-21T09:05:00-07:00",
                    "end": "2028-08-21T09:35:00-07:00",
                    "timezone": "America/Los_Angeles",
                    "availability": {"rima@microsoft.com": "free"},
                }
            ],
        }

        structured_delivery.finish_preview(
            action["id"],
            stdout=(
                "<<<RIVETER_RESULT>>>\n"
                + json.dumps(
                    {
                        "correlation_id": envelope["correlation_id"],
                        "phase": "preview",
                        "ok": True,
                        "payload": payload,
                    }
                )
                + "\n<<<END_RIVETER_RESULT>>>"
            ),
            stderr="",
            exit_code=0,
            correlation_id=envelope["correlation_id"],
            expected_channel="calendar",
            expected_attendees={"rima@microsoft.com"},
            expected_duration=25,
        )

        latest = get_latest_task_action(task["id"])
        self.assertEqual(latest["state"], "failed")
        self.assertIn("duration changed", latest["error"].lower())


class TestStructuredWorkerTransport(StructuredDeliveryTestBase):
    """Guards the seam between the subprocess and the result parser.

    Production ran every structured channel through `_run`, but no test ever
    called it: the suite handed `finish_preview`/`finish_execute` a ready-made
    string. So a decode fault that returned `returncode == 0` with
    `stdout is None` broke all six paths while the suite stayed green.
    """

    # '\u25cf' is e2 97 8f in UTF-8, and 0x8f is undefined in cp1252 - the exact
    # byte that killed the reader thread on the CLI's status banner.
    UTF8_BANNER = "\u25cf Disabled tools: \u2500\u2500 done\n"

    def _emit(self, text: str) -> subprocess.CompletedProcess:
        return structured_delivery._run(
            [
                sys.executable,
                "-c",
                "import sys;sys.stdout.buffer.write(sys.argv[1].encode('utf-8'))",
                text,
            ],
            timeout=60,
        )

    def test_run_decodes_utf8_output_instead_of_losing_it(self):
        result = self._emit(self.UTF8_BANNER)

        self.assertEqual(result.returncode, 0)
        self.assertIsNotNone(result.stdout, "captured stdout was silently dropped")
        self.assertIn("Disabled tools", result.stdout)

    def test_run_survives_undecodable_bytes_without_dropping_output(self):
        """Never trade one lost-output bug for another: replace, don't raise."""
        result = structured_delivery._run(
            [
                sys.executable,
                "-c",
                "import sys;sys.stdout.buffer.write(b'\\xff\\xfe ok')",
            ],
            timeout=60,
        )

        self.assertEqual(result.returncode, 0)
        self.assertIsNotNone(result.stdout)
        self.assertIn("ok", result.stdout)

    def test_marker_survives_a_utf8_banner_ahead_of_the_result(self):
        marker = (
            f"{structured_delivery.RESULT_START}\n"
            '{"correlation_id":"corr-1","phase":"execute","ok":true,'
            '"delivery_ref":"event-42"}\n'
            f"{structured_delivery.RESULT_END}\n"
        )

        result = self._emit(self.UTF8_BANNER + marker)
        parsed = structured_delivery.parse_result_marker(
            result.stdout, correlation_id="corr-1", phase="execute"
        )

        self.assertEqual(parsed["delivery_ref"], "event-42")

    def test_missing_output_fails_closed_rather_than_crashing(self):
        with self.assertRaises(ValueError):
            structured_delivery.parse_result_marker(
                None, correlation_id="corr-1", phase="preview"
            )

    def test_lost_preview_output_fails_the_action_with_a_clear_reason(self):
        task = create_task("Ping the project chat", action_type="follow-up",
                           source_type="chat")
        action = create_task_action(
            task["id"],
            delivery_channel="teams",
            structured_payload=json.dumps(
                structured_delivery.initial_payload(task, "teams")
            ),
        )

        structured_delivery.finish_preview(
            action["id"],
            stdout=None,
            stderr="",
            exit_code=0,
            correlation_id="corr-1",
        )

        latest = get_latest_task_action(task["id"])
        self.assertEqual(latest["state"], "failed")
        self.assertIn("no readable output", latest["error"].lower())

    def test_lost_execution_output_never_claims_delivery(self):
        """An unreadable write is ambiguous: the message may already be sent."""
        task = create_task("Reply to Sarah", action_type="respond-email",
                           source_type="email")
        action = create_task_action(
            task["id"],
            delivery_channel="email",
            destination_ref="sarah@microsoft.com",
            destination_display="Sarah Goodwin",
            structured_payload=json.dumps(
                {"schema_version": 1, "channel": "email", "body": "Approved"}
            ),
        )
        update_task_action(action["id"], frozenset({"state"}), state="executing")

        structured_delivery.finish_execute(
            action["id"],
            stdout=None,
            stderr="",
            exit_code=0,
            correlation_id="corr-1",
        )

        latest = get_latest_task_action(task["id"])
        self.assertEqual(latest["state"], "execute_unconfirmed")
        self.assertIsNone(latest["workiq_delivery_ref"])


class TestStructuredDestinationResolution(StructuredDeliveryTestBase):
    def test_teams_channel_reply_needs_the_whole_triple(self):
        """Joining empty ids produced "||", which is truthy and passed the guard."""
        ref, _display = structured_delivery._preview_destination(
            {
                "channel": "teams",
                "chat_id": None,
                "team_id": None,
                "channel_id": None,
                "message_id": None,
            }
        )
        self.assertEqual(ref, "")

        partial, _ = structured_delivery._preview_destination(
            {
                "channel": "teams",
                "chat_id": None,
                "team_id": "team-1",
                "channel_id": "channel-1",
                "message_id": None,
            }
        )
        self.assertEqual(partial, "")

        complete, _ = structured_delivery._preview_destination(
            {
                "channel": "teams",
                "chat_id": None,
                "team_id": "team-1",
                "channel_id": "channel-1",
                "message_id": "message-1",
            }
        )
        self.assertEqual(complete, "team-1|channel-1|message-1")

    def test_unresolved_teams_destination_fails_the_preview(self):
        task = create_task("Ping the project chat", action_type="follow-up",
                           source_type="chat")
        envelope = structured_delivery.initial_payload(task, "teams")
        action = create_task_action(
            task["id"],
            delivery_channel="teams",
            structured_payload=json.dumps(envelope),
        )

        structured_delivery.finish_preview(
            action["id"],
            stdout=(
                f"{structured_delivery.RESULT_START}\n"
                + json.dumps({
                    "correlation_id": envelope["correlation_id"],
                    "phase": "preview",
                    "ok": True,
                    "payload": {
                        "schema_version": 1,
                        "channel": "teams",
                        "destination_kind": "channel",
                        "chat_id": None,
                        "team_id": None,
                        "channel_id": None,
                        "message_id": None,
                        "destination_display": "Project channel",
                        "body": "Following up on the rollout.",
                    },
                })
                + f"\n{structured_delivery.RESULT_END}"
            ),
            stderr="",
            exit_code=0,
            correlation_id=envelope["correlation_id"],
        )

        latest = get_latest_task_action(task["id"])
        self.assertEqual(latest["state"], "failed")
        self.assertIn("destination", latest["error"].lower())


class TestCalendarIdempotency(StructuredDeliveryTestBase):
    """Graph dedupes event creates that share a transactionId.

    Measured 2026-08-22: posting the same event twice with
    transactionId "rvt-txn-c4d1" returned the SAME event id and an unchanged
    createdDateTime, so a repeated create cannot double-book. That makes a
    stable, persisted transaction id the difference between an ambiguous
    execution being retryable and being a dead end.
    """

    def _calendar_action(self):
        task = create_task(
            "Schedule a 25-minute review",
            action_type="schedule-meeting",
            key_people=json.dumps(
                [{"name": "Rima Reyes", "email": "rima@microsoft.com"}]
            ),
        )
        action = create_task_action(
            task["id"],
            delivery_channel="calendar",
            destination_ref="rima@microsoft.com",
            destination_display="Rima Reyes",
            structured_payload=json.dumps({
                "schema_version": 1,
                "channel": "calendar",
                "subject": "Project review",
                "body": "Agenda",
                "attendees": [{"name": "Rima Reyes", "email": "rima@microsoft.com"}],
                "duration_minutes": 25,
                "start": "2028-08-21T09:05:00-07:00",
                "end": "2028-08-21T09:30:00-07:00",
                "time_zone": "America/Los_Angeles",
            }),
        )
        update_task_action(action["id"], frozenset({"state"}), state="executing")
        return task, action

    def test_calendar_key_is_stable_for_one_row_and_unique_across_rows(self):
        row = {"id": 41, "task_id": 2495, "delivery_channel": "calendar"}
        other = {"id": 42, "task_id": 2495, "delivery_channel": "calendar"}

        self.assertEqual(
            structured_delivery.idempotency_key(row),
            structured_delivery.idempotency_key(dict(row)),
            "a retry must reuse the same key",
        )
        self.assertNotEqual(
            structured_delivery.idempotency_key(row),
            structured_delivery.idempotency_key(other),
        )
        self.assertIn("41", structured_delivery.idempotency_key(row))

    def test_execute_prompt_pins_the_key_for_calendar_only(self):
        payload = {
            "schema_version": 1,
            "channel": "calendar",
            "subject": "Project review",
            "start": "2028-08-21T09:05:00-07:00",
        }
        prompt = structured_delivery.execute_prompt(
            payload, "corr-1", "riveter-cal-t2495-a41"
        )
        self.assertIn("riveter-cal-t2495-a41", prompt)
        self.assertIn("transactionId", prompt)

        teams_prompt = structured_delivery.execute_prompt(
            {"schema_version": 1, "channel": "teams", "chat_id": "chat-1",
             "body": "hi"},
            "corr-1",
            None,
        )
        self.assertNotIn("transactionId", teams_prompt)

    def test_calendar_execution_requires_the_key_to_be_echoed(self):
        _task, action = self._calendar_action()

        structured_delivery.finish_execute(
            action["id"],
            stdout=(
                f"{structured_delivery.RESULT_START}\n"
                + json.dumps({
                    "correlation_id": "corr-1",
                    "phase": "execute",
                    "ok": True,
                    "delivery_ref": "event-42",
                    "idempotency_key": "riveter-cal-WRONG",
                })
                + f"\n{structured_delivery.RESULT_END}"
            ),
            stderr="",
            exit_code=0,
            correlation_id="corr-1",
            expected_idempotency_key="riveter-cal-t2495-a41",
        )

        latest = get_latest_task_action(action["task_id"])
        self.assertEqual(latest["state"], "execute_unconfirmed")
        self.assertIsNone(latest["workiq_delivery_ref"])

    def test_calendar_execution_confirms_when_the_key_matches(self):
        _task, action = self._calendar_action()
        txn = structured_delivery.idempotency_key(
            {**action, "delivery_channel": "calendar"}
        )

        structured_delivery.finish_execute(
            action["id"],
            stdout=(
                f"{structured_delivery.RESULT_START}\n"
                + json.dumps({
                    "correlation_id": "corr-1",
                    "phase": "execute",
                    "ok": True,
                    "delivery_ref": "AAMkAD-event-id",
                    "idempotency_key": txn,
                })
                + f"\n{structured_delivery.RESULT_END}"
            ),
            stderr="",
            exit_code=0,
            correlation_id="corr-1",
            expected_idempotency_key=txn,
        )

        latest = get_latest_task_action(action["task_id"])
        self.assertEqual(latest["state"], "executed", latest.get("error"))
        self.assertEqual(latest["workiq_delivery_ref"], "AAMkAD-event-id")

    def test_teams_execution_does_not_require_a_key(self):
        task = create_task("Ping the chat", action_type="follow-up",
                           source_type="chat")
        action = create_task_action(
            task["id"],
            delivery_channel="teams",
            destination_ref="chat-1",
            destination_display="Project chat",
            structured_payload=json.dumps(
                {"schema_version": 1, "channel": "teams", "body": "Approved"}
            ),
        )
        update_task_action(action["id"], frozenset({"state"}), state="executing")

        structured_delivery.finish_execute(
            action["id"],
            stdout=(
                f"{structured_delivery.RESULT_START}\n"
                + json.dumps({
                    "correlation_id": "corr-1",
                    "phase": "execute",
                    "ok": True,
                    "delivery_ref": "1787429363102",
                })
                + f"\n{structured_delivery.RESULT_END}"
            ),
            stderr="",
            exit_code=0,
            correlation_id="corr-1",
            expected_idempotency_key=None,
        )

        latest = get_latest_task_action(task["id"])
        self.assertEqual(latest["state"], "executed", latest.get("error"))
        self.assertEqual(latest["workiq_delivery_ref"], "1787429363102")


class TestCalendarContentQuality(StructuredDeliveryTestBase):
    """The structured rewrite lost content guidance Cowork had earned.

    cowork_runner carries an explicit "Include the agenda" instruction, added
    after a real task produced a draft that "gave them nothing to prepare
    against". The structured calendar prompt shipped without it and produced a
    single run-on sentence as the invite body.
    """

    def test_calendar_preview_prompt_demands_a_real_agenda(self):
        task = create_task(
            "Schedule Project Whale kickoff",
            action_type="schedule-meeting",
            description="Decide pilot structure and ownership.",
        )
        payload = structured_delivery.initial_payload(task, "calendar")

        prompt = structured_delivery.preview_prompt(task, payload)

        lowered = prompt.lower()
        self.assertIn("agenda", lowered)
        # It must ask for structure, not just mention the word.
        self.assertIn("- ", prompt)
        self.assertTrue(
            "own line" in lowered or "bullet" in lowered,
            "the prompt must ask for itemised agenda lines",
        )
        # And it must not invite invention.
        self.assertTrue(
            "do not invent" in lowered or "only what the task" in lowered,
            "agenda guidance must forbid inventing agenda items",
        )

    def test_email_prompt_is_not_given_calendar_agenda_guidance(self):
        task = create_task("Reply to Sarah", action_type="respond-email",
                           source_type="email")
        payload = structured_delivery.initial_payload(task, "email")

        prompt = structured_delivery.preview_prompt(task, payload)

        self.assertNotIn("Agenda", prompt)

    def test_calendar_draft_does_not_repeat_the_end_timestamp(self):
        """The card rendered "2:05-2:30 PM ET - 2026-08-25T14:30:00-04:00"."""
        draft = structured_delivery._preview_draft({
            "channel": "calendar",
            "subject": "Project Whale kickoff",
            "body": "Decide the pilot structure.",
            "slots": [{
                "id": "0",
                "label": "Tuesday, August 25, 2:05\u20132:30 PM ET",
                "start": "2026-08-25T14:05:00-04:00",
                "end": "2026-08-25T14:30:00-04:00",
            }],
        })

        self.assertIn("Tuesday, August 25, 2:05\u20132:30 PM ET", draft)
        self.assertNotIn("2026-08-25T14:30:00-04:00", draft)

    def test_confirmed_meeting_summary_states_what_was_booked(self):
        summary = structured_delivery.calendar_event_summary(
            {
                "subject": "Project Whale kickoff",
                "body": "Decide the pilot structure and ownership.",
                "attendees": [
                    {"name": "Sally Shi", "email": "sally.shi@microsoft.com"},
                    {"name": "Azharullah Meer", "email": "ameer@microsoft.com"},
                ],
                "start": "2026-08-25T14:05:00-04:00",
                "end": "2026-08-25T14:30:00-04:00",
                "duration_minutes": 25,
            },
            label="Tuesday, August 25, 2:05\u20132:30 PM ET",
        )

        self.assertIn("Project Whale kickoff", summary)
        self.assertIn("Tuesday, August 25, 2:05\u20132:30 PM ET", summary)
        self.assertIn("Sally Shi", summary)
        self.assertIn("Azharullah Meer", summary)
        self.assertIn("Decide the pilot structure", summary)


class TestIdempotencyKeys(StructuredDeliveryTestBase):
    """One concept, two transports.

    Calendar carries the key as Graph's native `transactionId`; email carries it
    as an `x-riveter-correlation-id` internet message header (verified
    2026-08-22 to survive both /sendMail and /reply). Teams has no mechanism at
    all, which the key function states honestly by returning None.
    """

    def test_keys_are_stable_per_row_and_scoped_by_channel(self):
        cal = {"id": 238, "task_id": 2495, "delivery_channel": "calendar"}
        mail = {"id": 238, "task_id": 2495, "delivery_channel": "email"}
        other_row = {"id": 239, "task_id": 2495, "delivery_channel": "email"}
        other_task = {"id": 238, "task_id": 2496, "delivery_channel": "email"}

        self.assertEqual(
            structured_delivery.idempotency_key(cal),
            structured_delivery.idempotency_key(dict(cal)),
        )
        self.assertNotEqual(
            structured_delivery.idempotency_key(cal),
            structured_delivery.idempotency_key(mail),
        )
        self.assertNotEqual(
            structured_delivery.idempotency_key(mail),
            structured_delivery.idempotency_key(other_row),
        )
        # A key found on a real message must say which task produced it, not
        # just an opaque row id that means nothing outside the database.
        key = structured_delivery.idempotency_key(mail)
        self.assertIn("2495", key)
        self.assertIn("238", key)
        self.assertNotEqual(
            key, structured_delivery.idempotency_key(other_task)
        )
        # Teams cannot support this, and must not pretend to.
        self.assertIsNone(structured_delivery.idempotency_key(
            {"id": 238, "task_id": 2495, "delivery_channel": "teams"}
        ))

    def test_email_execution_may_read_but_still_writes_once(self):
        argv = structured_delivery.execute_command("prompt", "email")
        available = next(
            value for value in argv if value.startswith("--available-tools=")
        )
        # The lookup that produces real evidence needs a read tool; the write
        # surface must stay exactly one primitive.
        self.assertIn("workiq-do_action", available)
        self.assertIn("workiq-fetch", available)
        self.assertNotIn("workiq-create_entity", available)
        self.assertNotIn("workiq-update_entity", available)
        self.assertNotIn("workiq-delete_entity", available)
        self.assertNotIn("shell", " ".join(argv))

    def test_email_prompt_requires_a_looked_up_reference_not_a_made_up_one(self):
        payload = {
            "schema_version": 1, "channel": "email", "mode": "reply",
            "message_id": "message-1", "to": ["sarah@microsoft.com"],
            "subject": "Re: Project update", "body": "Approved body",
        }
        prompt = structured_delivery.execute_prompt(
            payload, "corr-1", "riveter-mail-77"
        )
        lowered = prompt.lower()

        self.assertIn("riveter-mail-77", prompt)
        self.assertIn("x-riveter-correlation-id", lowered)
        self.assertIn("internetmessageheaders", lowered)
        self.assertIn("sentitems", lowered)
        # The old prompt told the model to synthesise "email-reply:{id}".
        self.assertNotIn("email-reply:", lowered)
        self.assertTrue(
            "do not invent" in lowered or "never invent" in lowered,
            "the prompt must forbid inventing a delivery reference",
        )
        # It must not both require a lookup and forbid fetching.
        self.assertNotIn("do not\nsearch, fetch", lowered)
        self.assertNotIn("do not search or fetch", lowered)

    def test_write_only_channels_are_still_told_not_to_read(self):
        teams = structured_delivery.execute_prompt(
            {"schema_version": 1, "channel": "teams", "chat_id": "chat-1",
             "body": "hi"},
            "corr-1",
            None,
        )
        self.assertIn("Do not search or fetch anything.", teams)

    def test_email_execution_rejects_a_missing_idempotency_key(self):
        task = create_task("Reply to Sarah", action_type="respond-email",
                           source_type="email")
        action = create_task_action(
            task["id"],
            delivery_channel="email",
            destination_ref="sarah@microsoft.com",
            destination_display="Sarah Goodwin",
            structured_payload=json.dumps(
                {"schema_version": 1, "channel": "email", "body": "Approved"}
            ),
        )
        update_task_action(action["id"], frozenset({"state"}), state="executing")

        structured_delivery.finish_execute(
            action["id"],
            stdout=(
                f"{structured_delivery.RESULT_START}\n"
                + json.dumps({
                    "correlation_id": "corr-1", "phase": "execute", "ok": True,
                    "delivery_ref": "email-reply:message-1",
                })
                + f"\n{structured_delivery.RESULT_END}"
            ),
            stderr="",
            exit_code=0,
            correlation_id="corr-1",
            expected_idempotency_key="riveter-mail-77",
        )

        latest = get_latest_task_action(task["id"])
        self.assertEqual(latest["state"], "execute_unconfirmed")
        self.assertIsNone(latest["workiq_delivery_ref"])

    def test_email_execution_confirms_with_a_real_message_id(self):
        task = create_task("Reply to Sarah", action_type="respond-email",
                           source_type="email")
        action = create_task_action(
            task["id"],
            delivery_channel="email",
            destination_ref="sarah@microsoft.com",
            destination_display="Sarah Goodwin",
            structured_payload=json.dumps(
                {"schema_version": 1, "channel": "email", "body": "Approved"}
            ),
        )
        update_task_action(action["id"], frozenset({"state"}), state="executing")
        key = structured_delivery.idempotency_key(
            {**action, "delivery_channel": "email"}
        )

        structured_delivery.finish_execute(
            action["id"],
            stdout=(
                f"{structured_delivery.RESULT_START}\n"
                + json.dumps({
                    "correlation_id": "corr-1", "phase": "execute", "ok": True,
                    "delivery_ref": "AAMkADFkODcyODkwLT-real-sent-id",
                    "idempotency_key": key,
                })
                + f"\n{structured_delivery.RESULT_END}"
            ),
            stderr="",
            exit_code=0,
            correlation_id="corr-1",
            expected_idempotency_key=key,
        )

        latest = get_latest_task_action(task["id"])
        self.assertEqual(latest["state"], "executed", latest.get("error"))
        self.assertEqual(
            latest["workiq_delivery_ref"], "AAMkADFkODcyODkwLT-real-sent-id"
        )


class TestTeamsRecovery(StructuredDeliveryTestBase):
    """Teams has no idempotency key, so repeating a post is a real second post.

    Measured 2026-08-22: identical posts produced two distinct message ids. The
    chatMessage schema has no extended properties, internet headers or
    transaction id, and every field it does have is user-visible, so nothing can
    be stamped on the message. Recovery therefore has to LOOK before posting,
    and that look is only trustworthy while the message is still recent enough
    to be in view.
    """

    def _teams_action(self, minutes_old=5):
        task = create_task("Ping the project chat", action_type="follow-up",
                           source_type="chat")
        action = create_task_action(
            task["id"],
            delivery_channel="teams",
            destination_ref="19:chat-1@thread.v2",
            destination_display="Project chat",
            structured_payload=json.dumps({
                "schema_version": 1, "channel": "teams", "mode": "chat",
                "chat_id": "19:chat-1@thread.v2", "body": "Approved body",
            }),
        )
        stamp = (
            datetime.now(timezone.utc) - timedelta(minutes=minutes_old)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        update_task_action(
            action["id"],
            frozenset({"state", "error", "updated_at"}),
            state="execute_unconfirmed",
            error="Structured worker produced no readable output",
            updated_at=stamp,
        )
        return task, action

    def test_normal_teams_execution_stays_write_only(self):
        argv = structured_delivery.execute_command("prompt", "teams")
        self.assertNotIn("workiq-fetch", " ".join(argv))

    def test_recovery_teams_execution_may_read_before_posting(self):
        argv = structured_delivery.execute_command("prompt", "teams", recover=True)
        tools = next(
            value for value in argv if value.startswith("--available-tools=")
        ).split("=", 1)[1].split(",")
        self.assertEqual(
            sorted(tools), ["workiq-create_entity", "workiq-fetch"]
        )

    def test_recovery_prompt_requires_looking_before_posting(self):
        payload = {
            "schema_version": 1, "channel": "teams", "mode": "chat",
            "chat_id": "19:chat-1@thread.v2", "body": "Approved body",
        }
        prompt = structured_delivery.execute_prompt(
            payload, "corr-1", None, recover=True
        )
        lowered = prompt.lower()

        self.assertIn("already", lowered)
        self.assertIn("already_posted", prompt)
        self.assertTrue(
            "do not post" in lowered or "without posting" in lowered,
            "recovery must be able to conclude the message is already there",
        )

    def test_normal_prompt_does_not_mention_recovery(self):
        payload = {
            "schema_version": 1, "channel": "teams", "mode": "chat",
            "chat_id": "19:chat-1@thread.v2", "body": "Approved body",
        }
        prompt = structured_delivery.execute_prompt(payload, "corr-1", None)
        self.assertNotIn("already_posted", prompt)

    def test_recovery_result_must_state_whether_it_posted(self):
        _task, action = self._teams_action()
        update_task_action(action["id"], frozenset({"state"}), state="executing")

        structured_delivery.finish_execute(
            action["id"],
            stdout=(
                f"{structured_delivery.RESULT_START}\n"
                + json.dumps({
                    "correlation_id": "corr-1", "phase": "execute", "ok": True,
                    "delivery_ref": "1787429374269",
                })
                + f"\n{structured_delivery.RESULT_END}"
            ),
            stderr="",
            exit_code=0,
            correlation_id="corr-1",
            require_post_disposition=True,
        )

        latest = get_latest_task_action(action["task_id"])
        self.assertEqual(latest["state"], "execute_unconfirmed")
        self.assertIsNone(latest["workiq_delivery_ref"])

    def test_recovery_confirms_when_it_reports_an_existing_message(self):
        _task, action = self._teams_action()
        update_task_action(action["id"], frozenset({"state"}), state="executing")

        structured_delivery.finish_execute(
            action["id"],
            stdout=(
                f"{structured_delivery.RESULT_START}\n"
                + json.dumps({
                    "correlation_id": "corr-1", "phase": "execute", "ok": True,
                    "delivery_ref": "1787429363102",
                    "already_posted": True,
                })
                + f"\n{structured_delivery.RESULT_END}"
            ),
            stderr="",
            exit_code=0,
            correlation_id="corr-1",
            require_post_disposition=True,
        )

        latest = get_latest_task_action(action["task_id"])
        self.assertEqual(latest["state"], "executed", latest.get("error"))
        self.assertEqual(latest["workiq_delivery_ref"], "1787429363102")


class TestEvidenceProvenance(StructuredDeliveryTestBase):
    """Slot evidence must say where it actually came from.

    finish_preview stamped {"source": "FindMeetingTimes+interaction"} while the
    preview worker only ever had workiq-ask/retrieve/fetch. Graph's
    findMeetingTimes and getSchedule are POST actions needing workiq-do_action,
    which preview deliberately does not have, so that label could never be
    earned. Worse, schedule_interaction_is_certified() gated slot selection by
    checking for that exact string, so Riveter was certifying meetings by
    reading back a label it had written itself.
    """

    def _evidence_from_preview(self):
        task = create_task(
            "Schedule a 25-minute review",
            action_type="schedule-meeting",
            key_people=json.dumps(
                [{"name": "Rima Reyes", "email": "rima@microsoft.com"}]
            ),
        )
        envelope = structured_delivery.initial_payload(task, "calendar")
        action = create_task_action(
            task["id"],
            delivery_channel="calendar",
            structured_payload=json.dumps(envelope),
        )
        duration = structured_delivery._meeting_duration(task)
        start = datetime(2028, 8, 21, 9, 5, tzinfo=timezone(timedelta(hours=-7)))
        # These cases are about how the evidence is LABELLED, so give them a
        # probe that genuinely measures the slot free. Availability that was
        # never measured is covered separately.
        structured_delivery.fetch_availability = lambda attendees, slots: [
            {
                "schedules": [{
                    "scheduleId": "rima@microsoft.com",
                    "availabilityView": "0" * 288,
                }],
                "view_start": start.astimezone(timezone.utc).isoformat(),
            }
            for _ in slots
        ]
        structured_delivery.finish_preview(
            action["id"],
            stdout=(
                f"{structured_delivery.RESULT_START}\n"
                + json.dumps({
                    "correlation_id": envelope["correlation_id"],
                    "phase": "preview", "ok": True,
                    "payload": {
                        "schema_version": 1, "channel": "calendar",
                        "subject": "Quarterly review", "body": "Agree the plan.",
                        "duration_minutes": duration,
                        "attendees": [
                            {"name": "Rima Reyes", "email": "rima@microsoft.com"}
                        ],
                        "timezone": "America/Los_Angeles",
                        "slots": [{
                            "id": "0", "label": "Monday 9:05",
                            "start": start.isoformat(),
                            "end": (start + timedelta(minutes=duration)).isoformat(),
                            "timezone": "America/Los_Angeles",
                            "availability": {"rima@microsoft.com": "free"},
                        }],
                    },
                })
                + f"\n{structured_delivery.RESULT_END}"
            ),
            stderr="", exit_code=0,
            correlation_id=envelope["correlation_id"],
            expected_channel="calendar",
            expected_attendees={"rima@microsoft.com"},
            expected_duration=duration,
        )
        latest = get_latest_task_action(task["id"])
        interaction = json.loads(latest["blocked_question"])
        return task, interaction

    def test_preview_does_not_claim_a_scheduler_it_cannot_call(self):
        _task, interaction = self._evidence_from_preview()
        evidence = interaction["schedule_evidence"]

        self.assertNotEqual(
            evidence["source"], "FindMeetingTimes+interaction",
            "preview cannot call findMeetingTimes; it has no do_action",
        )
        # It was still a live M365 query, just a different class of one.
        self.assertTrue(evidence["query_backed"])
        self.assertTrue(str(evidence["source"]).strip())

    def test_certifier_accepts_the_honest_preview_source(self):
        from src.services.cowork_runner import (
            schedule_attendees, schedule_duration_minutes,
            schedule_interaction_is_certified,
        )

        task, interaction = self._evidence_from_preview()
        certified = schedule_interaction_is_certified(
            interaction,
            schedule_attendees(task),
            schedule_duration_minutes(task),
        )
        self.assertTrue(
            certified,
            "an honestly-labelled live query must still certify, otherwise "
            "telling the truth silently disables scheduling",
        )

    def test_certifier_does_not_gate_on_unmeasured_availability(self):
        """Measurement is best-effort, not a veto.

        Riveter checks the calendars when it can, but that check runs through a
        subprocess taking one to three minutes which frequently returns
        nothing. Refusing every unmeasured selection did not make scheduling
        safer, it made it unusable. The preview says plainly when the times
        were not checked instead.
        """
        from src.services.cowork_runner import (
            schedule_attendees, schedule_duration_minutes,
            schedule_interaction_is_certified,
        )

        task, interaction = self._evidence_from_preview()
        interaction["schedule_evidence"]["availability_verified"] = False

        self.assertTrue(schedule_interaction_is_certified(
            interaction,
            schedule_attendees(task),
            schedule_duration_minutes(task),
        ))

    def test_scheduler_evidence_does_not_need_riveter_to_remeasure(self):
        """FindMeetingTimes measured availability itself; do not gate it."""
        from src.services.cowork_runner import (
            schedule_attendees, schedule_duration_minutes,
            schedule_interaction_is_certified,
        )

        task, interaction = self._evidence_from_preview()
        interaction["schedule_evidence"]["source"] = "FindMeetingTimes+interaction"
        interaction["schedule_evidence"].pop("availability_verified", None)

        self.assertTrue(schedule_interaction_is_certified(
            interaction,
            schedule_attendees(task),
            schedule_duration_minutes(task),
        ))

    def test_structured_scheduler_source_is_certified_not_self_reported(self):
        from src.services.cowork_runner import (
            CERTIFIED_SCHEDULE_SOURCES,
            SELF_REPORTED_SCHEDULE_SOURCES,
            schedule_attendees,
            schedule_duration_minutes,
            schedule_interaction_is_certified,
        )

        task, interaction = self._evidence_from_preview()
        source = "FindMeetingTimes+structured"
        interaction["schedule_evidence"]["source"] = source
        interaction["schedule_evidence"]["availability_verified"] = True

        self.assertIn(source, CERTIFIED_SCHEDULE_SOURCES)
        self.assertNotIn(source, SELF_REPORTED_SCHEDULE_SOURCES)
        self.assertIs(
            schedule_interaction_is_certified(
                interaction,
                schedule_attendees(task),
                schedule_duration_minutes(task),
            ),
            True,
        )

    def test_certifier_still_accepts_cowork_scheduler_evidence(self):
        """Cowork really does call FindMeetingTimes; those rows stay valid."""
        from src.services.cowork_runner import (
            schedule_attendees, schedule_duration_minutes,
            schedule_interaction_is_certified,
        )

        task, interaction = self._evidence_from_preview()
        interaction["schedule_evidence"]["source"] = "FindMeetingTimes+interaction"

        self.assertTrue(schedule_interaction_is_certified(
            interaction,
            schedule_attendees(task),
            schedule_duration_minutes(task),
        ))

    def test_certifier_rejects_an_unknown_evidence_source(self):
        from src.services.cowork_runner import (
            schedule_attendees, schedule_duration_minutes,
            schedule_interaction_is_certified,
        )

        task, interaction = self._evidence_from_preview()
        interaction["schedule_evidence"]["source"] = "guessed-it"

        self.assertFalse(schedule_interaction_is_certified(
            interaction,
            schedule_attendees(task),
            schedule_duration_minutes(task),
        ))

    def test_raw_graph_scheduler_slots_would_fail_the_start_offset_rule(self):
        """Why Riveter does not simply relay findMeetingTimes output.

        A real /me/findMeetingTimes call on 2026-08-22 returned slots at 15:00,
        18:00 and 21:30 UTC - minute % 30 == 0. The user's standing rule puts
        meetings at :05/:35, and the certifier enforces that modulus, so Graph's
        own suggestions would be rejected by Riveter's own gate. Relaying them
        requires solving the offset first; this test pins that trap.
        """
        from src.services.cowork_runner import (
            schedule_attendees, schedule_duration_minutes,
            schedule_interaction_is_certified,
        )

        task, interaction = self._evidence_from_preview()
        evidence = interaction["schedule_evidence"]
        graph_start = datetime(
            2028, 8, 21, 9, 0, tzinfo=timezone(timedelta(hours=-7))
        )
        duration = schedule_duration_minutes(task)
        evidence["slots"] = [{
            "value": "0", "label": "Monday 9:00",
            "start": graph_start.isoformat(),
            "end": (graph_start + timedelta(minutes=duration)).isoformat(),
            "timezone": "America/Los_Angeles",
            "availability": {"rima@microsoft.com": "free"},
        }]

        # Production configures the :05 rule. Without this the certifier falls
        # back to the first slot's own minute, which makes the check
        # self-satisfying and hides the mismatch entirely.
        with mock.patch(
            "src.services.cowork_runner.meeting_preferences",
            return_value={"default_minutes": 25, "start_offset_minutes": 5},
        ):
            certified = schedule_interaction_is_certified(
                interaction, schedule_attendees(task), duration
            )
        self.assertFalse(certified)


class TestEmailBodyFidelity(StructuredDeliveryTestBase):
    """The delivered mail must look like the draft that was approved.

    Observed in production 2026-08-23 (task 2124): the approved draft had
    paragraph breaks, the model sent it as contentType "html" with the raw
    plain text inside, and every newline collapsed. The words survived, the
    structure did not. Riveter approves a specific artifact, so Riveter renders
    the wire format rather than leaving it to the worker.
    """

    def test_plain_body_becomes_html_with_preserved_breaks(self):
        html_body = structured_delivery.plain_text_to_html(
            "Hi Phil,\n\nKickstarter turns a use case into a plan.\n\n"
            "Thanks,\nPhil"
        )
        self.assertIn("Hi Phil,<br>", html_body)
        self.assertIn("Thanks,<br>\nPhil", html_body)
        # Every newline must survive as a break.
        self.assertEqual(html_body.count("<br>"), 5)

    def test_rendering_escapes_markup_so_content_cannot_inject(self):
        html_body = structured_delivery.plain_text_to_html(
            "5 < 6 & <script>alert('x')</script>"
        )
        self.assertNotIn("<script>", html_body)
        self.assertIn("&lt;script&gt;", html_body)
        self.assertIn("&amp;", html_body)

    def test_email_prompt_supplies_the_rendered_html_verbatim(self):
        payload = {
            "schema_version": 1, "channel": "email", "mode": "reply",
            "message_id": "message-1", "to": ["sarah@microsoft.com"],
            "subject": "Re: Project update",
            "body": "Hi Sarah,\n\nApproved body.\n\nThanks,\nPhil",
        }
        prompt = structured_delivery.execute_prompt(
            payload, "corr-1", "riveter-mail-t1-a1"
        )
        lowered = prompt.lower()

        self.assertIn("Approved body.<br>", prompt)
        self.assertIn('"contenttype":"html"', lowered.replace(" ", ""))
        # It must not leave the wire format to the worker's judgement.
        self.assertTrue(
            "do not reformat" in lowered or "verbatim" in lowered,
            "the prompt must pin the rendered body",
        )

    def test_single_newlines_are_not_lost_either(self):
        rendered = structured_delivery.plain_text_to_html("a\nb\nc")
        self.assertEqual(rendered, "a<br>\nb<br>\nc")

    def test_empty_body_renders_empty(self):
        self.assertEqual(structured_delivery.plain_text_to_html(""), "")
        self.assertEqual(structured_delivery.plain_text_to_html(None), "")


class TestEmailRecipientEnforcement(StructuredDeliveryTestBase):
    """The address you approved must be the address that receives it.

    Observed in production 2026-08-23 (task 2124): the approved destination was
    phil@topness.com, but the send used Graph's /reply, which addresses the
    thread and ignores the payload's `to` list entirely. The mail arrived at
    Phil.Topness@microsoft.com. Same human that time; a thread with other
    participants would have delivered to people who were never approved.
    """

    def _email_action(self, destination="sarah@microsoft.com"):
        task = create_task("Reply to Sarah", action_type="respond-email",
                           source_type="email")
        action = create_task_action(
            task["id"],
            delivery_channel="email",
            destination_ref=destination,
            destination_display=destination,
            structured_payload=json.dumps({
                "schema_version": 1, "channel": "email", "mode": "reply",
                "message_id": "message-1", "to": [destination],
                "subject": "Re: Project update", "body": "Approved body",
            }),
        )
        update_task_action(action["id"], frozenset({"state"}), state="executing")
        return task, action

    def test_preview_prompt_demands_the_real_reply_recipients(self):
        task = create_task("Reply to Sarah", action_type="respond-email",
                           source_type="email")
        payload = structured_delivery.initial_payload(task, "email")
        prompt = structured_delivery.preview_prompt(task, payload)
        lowered = prompt.lower()

        self.assertIn("reply", lowered)
        self.assertTrue(
            "actually" in lowered or "actual recipients" in lowered,
            "preview must record who the reply will really reach",
        )

    def test_execution_rejects_delivery_to_an_unapproved_address(self):
        _task, action = self._email_action()

        structured_delivery.finish_execute(
            action["id"],
            stdout=(
                f"{structured_delivery.RESULT_START}\n"
                + json.dumps({
                    "correlation_id": "corr-1", "phase": "execute", "ok": True,
                    "delivery_ref": "AAMkAD-sent",
                    "idempotency_key": "k1",
                    "recipients": ["someone.else@microsoft.com"],
                })
                + f"\n{structured_delivery.RESULT_END}"
            ),
            stderr="", exit_code=0, correlation_id="corr-1",
            expected_idempotency_key="k1",
            expected_recipients={"sarah@microsoft.com"},
        )

        latest = get_latest_task_action(action["task_id"])
        self.assertEqual(latest["state"], "execute_unconfirmed")
        self.assertIsNone(latest["workiq_delivery_ref"])
        self.assertIn("recipient", latest["error"].lower())

    def test_execution_confirms_when_recipients_match(self):
        _task, action = self._email_action()

        structured_delivery.finish_execute(
            action["id"],
            stdout=(
                f"{structured_delivery.RESULT_START}\n"
                + json.dumps({
                    "correlation_id": "corr-1", "phase": "execute", "ok": True,
                    "delivery_ref": "AAMkAD-sent",
                    "idempotency_key": "k1",
                    # Case and display differences must not trip it.
                    "recipients": ["Sarah@Microsoft.com"],
                })
                + f"\n{structured_delivery.RESULT_END}"
            ),
            stderr="", exit_code=0, correlation_id="corr-1",
            expected_idempotency_key="k1",
            expected_recipients={"sarah@microsoft.com"},
        )

        latest = get_latest_task_action(action["task_id"])
        self.assertEqual(latest["state"], "executed", latest.get("error"))
        self.assertEqual(latest["workiq_delivery_ref"], "AAMkAD-sent")

    def test_execution_requires_recipients_to_be_reported_at_all(self):
        """Silence is not proof; an unreported recipient set is unverified."""
        _task, action = self._email_action()

        structured_delivery.finish_execute(
            action["id"],
            stdout=(
                f"{structured_delivery.RESULT_START}\n"
                + json.dumps({
                    "correlation_id": "corr-1", "phase": "execute", "ok": True,
                    "delivery_ref": "AAMkAD-sent", "idempotency_key": "k1",
                })
                + f"\n{structured_delivery.RESULT_END}"
            ),
            stderr="", exit_code=0, correlation_id="corr-1",
            expected_idempotency_key="k1",
            expected_recipients={"sarah@microsoft.com"},
        )

        latest = get_latest_task_action(action["task_id"])
        self.assertEqual(latest["state"], "execute_unconfirmed")

    def test_execute_prompt_asks_for_the_sent_recipients(self):
        payload = {
            "schema_version": 1, "channel": "email", "mode": "reply",
            "message_id": "message-1", "to": ["sarah@microsoft.com"],
            "subject": "Re: Project update", "body": "Approved body",
        }
        prompt = structured_delivery.execute_prompt(
            payload, "corr-1", "riveter-mail-t1-a1"
        )
        self.assertIn("recipients", prompt.lower())
        self.assertIn("torecipients", prompt.lower().replace(" ", ""))


class TestSchedulerFirstPreview(StructuredDeliveryTestBase):
    def _task(self, count=3):
        people = [
            {
                "name": f"Person {index}",
                "email": f"person-{index}@x.com",
            }
            for index in range(count)
        ]
        return create_task(
            "Schedule a 25-minute review in the week of August 31",
            action_type="schedule-meeting",
            key_people=json.dumps(people),
        )

    def _payload(self, task, count=3):
        people = [
            {
                "name": f"Person {index}",
                "email": f"person-{index}@x.com",
            }
            for index in range(count)
        ]
        return {
            "schema_version": 1,
            "channel": "calendar",
            "subject": "Review",
            "body": "Agree the plan.",
            "duration_minutes": 25,
            "attendees": people,
            "timezone": "Eastern Standard Time",
            # Phase 1 keeps provisional slots only for infrastructure fallback.
            "slots": [{
                "id": "fallback",
                "label": "Fallback",
                "start": "2099-08-31T13:05:00-04:00",
                "end": "2099-08-31T13:30:00-04:00",
                "timezone": "Eastern Standard Time",
                "availability": {
                    person["email"]: "free" for person in people
                },
            }],
            "scheduling_constraints": {
                "search_window": {
                    "start": "2099-08-31T09:00:00",
                    "end": "2099-09-04T17:00:00",
                    "timezone": "Eastern Standard Time",
                },
            },
        }

    @staticmethod
    def _scheduler_stdout(suggestions, reason="", timezones=None):
        return (
            structured_delivery.RESULT_START
            + json.dumps({
                "result": {
                    "emptySuggestionsReason": reason,
                    "meetingTimeSuggestions": suggestions,
                },
                "attendeeTimezones": timezones or {},
            })
            + structured_delivery.RESULT_END
        )

    @staticmethod
    def _suggestion(count=3, *, organizer="free", unavailable=None):
        statuses = {
            f"person-{index}@x.com": (
                "busy" if unavailable == index else "free"
            )
            for index in range(count)
        }
        return {
            "confidence": 100 if unavailable is None else (count - 1) / count * 100,
            "organizerAvailability": organizer,
            "attendeeAvailability": [
                {
                    "availability": status,
                    "attendee": {
                        "emailAddress": {"address": email},
                    },
                }
                for email, status in statuses.items()
            ],
            "meetingTimeSlot": {
                "start": {
                    "dateTime": "2099-08-31T17:00:00.0000000",
                    "timeZone": "UTC",
                },
                "end": {
                    "dateTime": "2099-08-31T17:30:00.0000000",
                    "timeZone": "UTC",
                },
            },
        }

    def test_calendar_phase_one_resolves_window_but_does_not_query_calendars(self):
        task = self._task()
        envelope = structured_delivery.initial_payload(task, "calendar")
        prompt = structured_delivery.preview_prompt(task, envelope)
        self.assertIn("scheduling_constraints", prompt)
        self.assertIn("Do not query calendar availability", prompt)

    def test_attendee_injection_is_rejected_before_scheduler_run(self):
        task = self._task()
        payload = self._payload(task)
        payload["attendees"].append({
            "name": "Injected",
            "email": "injected@x.com",
        })
        calls = []
        original = structured_delivery._run
        structured_delivery._run = lambda *a, **k: calls.append((a, k))
        try:
            with self.assertRaisesRegex(ValueError, "attendees"):
                structured_delivery._find_meeting_slots(task, payload)
        finally:
            structured_delivery._run = original
        self.assertEqual(calls, [])

    def test_same_day_search_window_is_clamped_past_now(self):
        task = self._task()
        payload = self._payload(task)
        payload["scheduling_constraints"]["search_window"] = {
            "start": "2099-08-31T09:00:00",
            "end": "2099-08-31T17:00:00",
            "timezone": "UTC",
        }
        now = datetime(2099, 8, 31, 12, 0, tzinfo=timezone.utc)
        body, _duration, _offset, _zone = structured_delivery._scheduler_request(
            task, payload, 100, now=now
        )
        queried = datetime.fromisoformat(
            body["timeConstraint"]["timeSlots"][0]["start"]["dateTime"]
        ).replace(tzinfo=timezone.utc)
        self.assertGreater(queried, now)

    def test_explicit_evening_or_weekend_window_can_be_unrestricted(self):
        task = self._task()
        payload = self._payload(task)
        payload["scheduling_constraints"]["activity_domain"] = "unrestricted"
        body, _duration, _offset, _zone = structured_delivery._scheduler_request(
            task, payload, 100
        )
        self.assertEqual(
            body["timeConstraint"]["activityDomain"], "unrestricted"
        )

    def test_scheduler_mints_findmeetingtimes_body_and_shifts_to_offset(self):
        task = self._task()
        payload = self._payload(task)
        calls = []
        original = structured_delivery._run

        def fake_run(argv, timeout=300):
            calls.append((argv, timeout))
            return subprocess.CompletedProcess(
                argv, 0,
                stdout=self._scheduler_stdout([self._suggestion()]),
                stderr="",
            )

        structured_delivery._run = fake_run
        try:
            with mock.patch(
                "src.services.cowork_runner.meeting_preferences",
                return_value={
                    "default_minutes": 25,
                    "start_offset_minutes": 5,
                },
            ):
                slots, evidence = structured_delivery._find_meeting_slots(
                    task, payload
                )
        finally:
            structured_delivery._run = original

        self.assertEqual(len(calls), 1)
        joined = " ".join(calls[0][0])
        self.assertIn("/me/findMeetingTimes", joined)
        self.assertIn("/me/calendar/getSchedule", joined)
        self.assertIn("attendeeTimezones", joined)
        self.assertIn("If and only if meetingTimeSuggestions is nonempty", joined)
        self.assertIn('"meetingDuration":"PT30M"', joined.replace(" ", ""))
        self.assertIn('"minimumAttendeePercentage":100', joined.replace(" ", ""))
        self.assertEqual(calls[0][1], structured_delivery.SCHEDULER_TIMEOUT_SECONDS)
        self.assertGreaterEqual(
            structured_delivery.SCHEDULER_TIMEOUT_SECONDS, 180
        )
        self.assertEqual(slots[0]["start"], "2099-08-31T13:05:00-04:00")
        self.assertEqual(slots[0]["end"], "2099-08-31T13:30:00-04:00")
        self.assertEqual(evidence["source"], "FindMeetingTimes+structured")
        self.assertTrue(evidence["availability_verified"])

    def test_scheduler_retries_once_allowing_exactly_one_busy_attendee(self):
        task = self._task()
        payload = self._payload(task)
        outputs = [
            self._scheduler_stdout([], "AttendeesUnavailable"),
            self._scheduler_stdout([self._suggestion(unavailable=2)]),
        ]
        calls = []
        original = structured_delivery._run

        def fake_run(argv, timeout=300):
            calls.append(argv)
            return subprocess.CompletedProcess(
                argv, 0, stdout=outputs[len(calls) - 1], stderr=""
            )

        structured_delivery._run = fake_run
        try:
            slots, evidence = structured_delivery._find_meeting_slots(
                task, payload
            )
        finally:
            structured_delivery._run = original

        self.assertEqual(len(calls), 2)
        self.assertIn(
            '"minimumAttendeePercentage":66',
            " ".join(calls[1]).replace(" ", ""),
        )
        self.assertEqual(slots[0]["availability"]["person-2@x.com"], "busy")
        self.assertEqual(evidence["graph_minimum_attendee_percentage"], 66)

    def test_scheduler_six_attendees_retries_at_eighty_three(self):
        task = self._task(count=6)
        payload = self._payload(task, count=6)
        outputs = [
            self._scheduler_stdout([], "AttendeesUnavailable"),
            self._scheduler_stdout([self._suggestion(count=6, unavailable=5)]),
        ]
        calls = []
        original = structured_delivery._run

        def fake_run(argv, timeout=300):
            calls.append(argv)
            return subprocess.CompletedProcess(
                argv, 0, stdout=outputs[len(calls) - 1], stderr=""
            )

        structured_delivery._run = fake_run
        try:
            structured_delivery._find_meeting_slots(task, payload)
        finally:
            structured_delivery._run = original

        self.assertEqual(len(calls), 2)
        self.assertIn(
            '"minimumAttendeePercentage":83',
            " ".join(calls[1]).replace(" ", ""),
        )

    def test_scheduler_never_offers_oof_or_organizer_busy_suggestion(self):
        task = self._task()
        payload = self._payload(task)
        oof = self._suggestion(unavailable=2)
        oof["attendeeAvailability"][2]["availability"] = "oof"
        organizer_busy = self._suggestion()
        organizer_busy["organizerAvailability"] = "busy"
        outputs = [
            self._scheduler_stdout([oof, organizer_busy]),
            self._scheduler_stdout([], "AttendeesUnavailable"),
        ]
        original = structured_delivery._run
        call_index = 0

        def fake_run(argv, timeout=300):
            nonlocal call_index
            output = outputs[call_index]
            call_index += 1
            return subprocess.CompletedProcess(
                argv, 0, stdout=output, stderr=""
            )

        structured_delivery._run = fake_run
        try:
            with self.assertRaisesRegex(ValueError, "unsafe"):
                structured_delivery._find_meeting_slots(task, payload)
        finally:
            structured_delivery._run = original

    def test_incomplete_graph_suggestion_triggers_fallback_not_no_mutual(self):
        task = self._task()
        payload = self._payload(task)
        incomplete = self._suggestion()
        incomplete["attendeeAvailability"][2]["availability"] = "unknown"
        output = self._scheduler_stdout([incomplete])
        original = structured_delivery._run
        structured_delivery._run = lambda argv, timeout=300: subprocess.CompletedProcess(
            argv, 0, stdout=output, stderr=""
        )
        try:
            with self.assertRaisesRegex(ValueError, "suggestion"):
                structured_delivery._find_meeting_slots(task, payload)
        finally:
            structured_delivery._run = original

    def test_flat_graph_response_is_accepted_without_prompt_wrapper(self):
        flat = (
            structured_delivery.RESULT_START
            + json.dumps({
                "emptySuggestionsReason": "",
                "meetingTimeSuggestions": [self._suggestion()],
            })
            + structured_delivery.RESULT_END
        )
        parsed = structured_delivery._parse_find_times(flat)
        self.assertEqual(len(parsed["meetingTimeSuggestions"]), 1)

    def test_timezone_labels_are_relative_to_organizer(self):
        slots = [{
            "start": "2026-07-15T13:05:00-04:00",
            "end": "2026-07-15T13:30:00-04:00",
            "timezone": "America/New_York",
        }]
        labels = structured_delivery._attendee_timezone_labels(
            {
                "east@x.com": "Eastern Standard Time",
                "west@x.com": "Pacific Standard Time",
            },
            slots,
        )
        self.assertEqual(labels["east@x.com"], "same TZ")
        self.assertEqual(labels["west@x.com"], "-3h")

        winter = [dict(
            slots[0],
            start="2026-12-15T13:05:00-05:00",
            end="2026-12-15T13:30:00-05:00",
        )]
        labels = structured_delivery._attendee_timezone_labels(
            {"east@x.com": "Eastern Standard Time"},
            winter,
        )
        self.assertEqual(labels["east@x.com"], "same TZ")

    def test_timezone_label_shows_range_across_dst_mismatch(self):
        slots = [
            {
                "start": "2026-10-20T13:05:00-04:00",
                "timezone": "America/New_York",
            },
            {
                "start": "2026-10-28T13:05:00-04:00",
                "timezone": "America/New_York",
            },
        ]
        labels = structured_delivery._attendee_timezone_labels(
            {"london@x.com": "GMT Standard Time"},
            slots,
        )
        self.assertEqual(labels["london@x.com"], "+4h/+5h")

    def test_scheduler_timezone_map_is_validated_and_stored_in_evidence(self):
        task = self._task()
        payload = self._payload(task)
        output = self._scheduler_stdout(
            [self._suggestion()],
            timezones={
                "person-0@x.com": "Eastern Standard Time",
                "person-1@x.com": "Pacific Standard Time",
            },
        )
        original = structured_delivery._run
        structured_delivery._run = lambda argv, timeout=300: subprocess.CompletedProcess(
            argv, 0, stdout=output, stderr=""
        )
        try:
            slots, evidence = structured_delivery._find_meeting_slots(
                task, payload
            )
        finally:
            structured_delivery._run = original

        self.assertEqual(
            evidence["attendee_timezones"]["person-1@x.com"],
            "Pacific Standard Time",
        )
        self.assertEqual(
            evidence["attendee_timezone_labels"]["person-1@x.com"],
            "-3h",
        )
        self.assertTrue(slots)

    def test_unknown_timezone_attendee_is_rejected(self):
        task = self._task()
        payload = self._payload(task)
        output = self._scheduler_stdout(
            [self._suggestion()],
            timezones={"injected@x.com": "Pacific Standard Time"},
        )
        original = structured_delivery._run
        structured_delivery._run = lambda argv, timeout=300: subprocess.CompletedProcess(
            argv, 0, stdout=output, stderr=""
        )
        try:
            with self.assertRaisesRegex(ValueError, "timezone"):
                structured_delivery._find_meeting_slots(task, payload)
        finally:
            structured_delivery._run = original

    def test_unresolvable_optional_timezone_is_omitted_not_fatal(self):
        task = self._task()
        payload = self._payload(task)
        output = self._scheduler_stdout(
            [self._suggestion()],
            timezones={"person-0@x.com": "Customized Time Zone"},
        )
        original = structured_delivery._run
        structured_delivery._run = lambda argv, timeout=300: subprocess.CompletedProcess(
            argv, 0, stdout=output, stderr=""
        )
        try:
            slots, evidence = structured_delivery._find_meeting_slots(
                task, payload
            )
        finally:
            structured_delivery._run = original

        self.assertTrue(slots)
        self.assertEqual(evidence["attendee_timezones"], {})
        self.assertEqual(evidence["attendee_timezone_labels"], {})

    def test_fallback_measurements_expose_working_hours_timezones(self):
        measured = [{
            "schedules": [{
                "scheduleId": "a@x.com",
                "scheduleItems": [],
                "workingHours": {
                    "timeZone": {"name": "Eastern Standard Time"},
                },
            }],
        }]
        self.assertEqual(
            structured_delivery._timezones_from_measurements(measured),
            {"a@x.com": "Eastern Standard Time"},
        )

    def test_no_mutual_time_is_distinct_from_infrastructure_failure(self):
        task = self._task()
        payload = self._payload(task)
        output = self._scheduler_stdout([], "AttendeesUnavailable")
        original = structured_delivery._run
        calls = []

        def fake_run(argv, timeout=300):
            calls.append(argv)
            return subprocess.CompletedProcess(
                argv, 0, stdout=output, stderr=""
            )

        structured_delivery._run = fake_run
        try:
            with self.assertRaises(
                structured_delivery.NoMutualFreeTime
            ):
                structured_delivery._find_meeting_slots(task, payload)
        finally:
            structured_delivery._run = original
        self.assertEqual(len(calls), 2)

    def test_graph_evidence_bypasses_getschedule_but_keeps_finish_validators(self):
        task = self._task()
        envelope = structured_delivery.initial_payload(task, "calendar")
        action = create_task_action(
            task["id"],
            delivery_channel="calendar",
            structured_payload=json.dumps(envelope),
        )
        payload = self._payload(task)
        payload["slots"] = [{
            "id": "0",
            "label": "Monday, August 31, 2099, 1:05-1:30 PM ET",
            "start": "2099-08-31T13:05:00-04:00",
            "end": "2099-08-31T13:30:00-04:00",
            "timezone": "Eastern Standard Time",
            "availability": {
                f"person-{index}@x.com": "free" for index in range(3)
            },
        }]
        graph_evidence = {
            "source": "FindMeetingTimes+structured",
            "query_backed": True,
            "availability_verified": True,
            "graph_confidence": 100,
            "graph_suggestion_count": 1,
            "graph_minimum_attendee_percentage": 100,
        }
        stdout = (
            structured_delivery.RESULT_START
            + json.dumps({
                "correlation_id": envelope["correlation_id"],
                "phase": "preview",
                "ok": True,
                "payload": payload,
            })
            + structured_delivery.RESULT_END
        )

        structured_delivery.finish_preview(
            action["id"],
            stdout=stdout,
            stderr="",
            exit_code=0,
            correlation_id=envelope["correlation_id"],
            expected_channel="calendar",
            expected_attendees={
                f"person-{index}@x.com" for index in range(3)
            },
            expected_duration=25,
            _graph_evidence=graph_evidence,
        )

        self.assertEqual(self.availability_probe_calls, [])
        latest = get_latest_task_action(task["id"])
        interaction = json.loads(latest["blocked_question"])
        evidence = interaction["schedule_evidence"]
        self.assertEqual(evidence["source"], "FindMeetingTimes+structured")
        self.assertTrue(evidence["availability_verified"])
        self.assertEqual(evidence["availability_coverage"], "full")

    def _phase_one_stdout(self, envelope, payload):
        return (
            structured_delivery.RESULT_START
            + json.dumps({
                "correlation_id": envelope["correlation_id"],
                "phase": "preview",
                "ok": True,
                "payload": payload,
            })
            + structured_delivery.RESULT_END
        )

    def test_preview_worker_runs_scheduler_then_persists_graph_chooser(self):
        task = self._task()
        envelope = structured_delivery.initial_payload(task, "calendar")
        action = create_task_action(
            task["id"],
            delivery_channel="calendar",
            structured_payload=json.dumps(envelope),
        )
        outputs = [
            self._phase_one_stdout(envelope, self._payload(task)),
            self._scheduler_stdout(
                [self._suggestion()],
                timezones={
                    "person-0@x.com": "Eastern Standard Time",
                    "person-1@x.com": "Pacific Standard Time",
                },
            ),
        ]
        calls = []
        original = structured_delivery._run

        def fake_run(argv, timeout=300):
            calls.append(argv)
            return subprocess.CompletedProcess(
                argv, 0, stdout=outputs[len(calls) - 1], stderr=""
            )

        structured_delivery._run = fake_run
        try:
            with mock.patch(
                "src.services.cowork_runner.meeting_preferences",
                return_value={
                    "default_minutes": 25,
                    "start_offset_minutes": 5,
                },
            ):
                structured_delivery._preview_worker(task, action)
        finally:
            structured_delivery._run = original

        self.assertEqual(len(calls), 2)
        self.assertEqual(self.availability_probe_calls, [])
        latest = get_latest_task_action(task["id"])
        self.assertEqual(latest["state"], "previewing")
        evidence = json.loads(latest["blocked_question"])["schedule_evidence"]
        self.assertEqual(evidence["source"], "FindMeetingTimes+structured")
        self.assertTrue(evidence["availability_verified"])
        self.assertEqual(
            evidence["attendee_timezone_labels"]["person-1@x.com"],
            "-3h",
        )

    def test_working_elsewhere_survives_scheduler_into_chooser(self):
        task = self._task()
        envelope = structured_delivery.initial_payload(task, "calendar")
        action = create_task_action(
            task["id"],
            delivery_channel="calendar",
            structured_payload=json.dumps(envelope),
        )
        suggestion = self._suggestion()
        suggestion["attendeeAvailability"][0]["availability"] = "workingElsewhere"
        outputs = [
            self._phase_one_stdout(envelope, self._payload(task)),
            self._scheduler_stdout([suggestion]),
        ]
        calls = []
        original = structured_delivery._run

        def fake_run(argv, timeout=300):
            calls.append(argv)
            return subprocess.CompletedProcess(
                argv, 0, stdout=outputs[len(calls) - 1], stderr=""
            )

        structured_delivery._run = fake_run
        try:
            structured_delivery._preview_worker(task, action)
        finally:
            structured_delivery._run = original

        latest = get_latest_task_action(task["id"])
        self.assertEqual(latest["state"], "previewing", latest.get("error"))
        evidence = json.loads(latest["blocked_question"])["schedule_evidence"]
        self.assertEqual(
            evidence["slots"][0]["availability"]["person-0@x.com"],
            "workingElsewhere",
        )

    def test_no_mutual_time_fails_closed_without_getschedule_fallback(self):
        task = self._task()
        envelope = structured_delivery.initial_payload(task, "calendar")
        action = create_task_action(
            task["id"],
            delivery_channel="calendar",
            structured_payload=json.dumps(envelope),
        )
        outputs = [
            self._phase_one_stdout(envelope, self._payload(task)),
            self._scheduler_stdout([], "AttendeesUnavailable"),
            self._scheduler_stdout([], "AttendeesUnavailable"),
        ]
        calls = []
        original = structured_delivery._run

        def fake_run(argv, timeout=300):
            calls.append(argv)
            return subprocess.CompletedProcess(
                argv, 0, stdout=outputs[len(calls) - 1], stderr=""
            )

        structured_delivery._run = fake_run
        try:
            structured_delivery._preview_worker(task, action)
        finally:
            structured_delivery._run = original

        self.assertEqual(len(calls), 3)
        self.assertEqual(self.availability_probe_calls, [])
        latest = get_latest_task_action(task["id"])
        self.assertEqual(latest["state"], "failed")
        self.assertIn("No mutual free time", latest["error"])

    def test_unreadable_scheduler_falls_back_to_existing_getschedule_path(self):
        task = self._task()
        envelope = structured_delivery.initial_payload(task, "calendar")
        action = create_task_action(
            task["id"],
            delivery_channel="calendar",
            structured_payload=json.dumps(envelope),
        )
        outputs = [
            self._phase_one_stdout(envelope, self._payload(task)),
            "scheduler returned no result marker",
        ]
        calls = []
        original = structured_delivery._run

        def fake_run(argv, timeout=300):
            calls.append(argv)
            return subprocess.CompletedProcess(
                argv, 0, stdout=outputs[len(calls) - 1], stderr=""
            )

        structured_delivery._run = fake_run
        try:
            structured_delivery._preview_worker(task, action)
        finally:
            structured_delivery._run = original

        self.assertEqual(len(calls), 2)
        self.assertTrue(self.availability_probe_calls)
        latest = get_latest_task_action(task["id"])
        self.assertEqual(latest["state"], "previewing")
        evidence = json.loads(latest["blocked_question"])["schedule_evidence"]
        self.assertEqual(evidence["source"], "copilot-ask")
        self.assertFalse(evidence["availability_verified"])

    def test_nonzero_scheduler_process_falls_back_to_existing_getschedule_path(self):
        task = self._task()
        envelope = structured_delivery.initial_payload(task, "calendar")
        action = create_task_action(
            task["id"],
            delivery_channel="calendar",
            structured_payload=json.dumps(envelope),
        )
        phase_one = self._phase_one_stdout(envelope, self._payload(task))
        calls = []
        original = structured_delivery._run

        def fake_run(argv, timeout=300):
            calls.append(argv)
            if len(calls) == 1:
                return subprocess.CompletedProcess(
                    argv, 0, stdout=phase_one, stderr=""
                )
            return subprocess.CompletedProcess(
                argv, 1, stdout="", stderr="scheduler auth failed"
            )

        structured_delivery._run = fake_run
        try:
            structured_delivery._preview_worker(task, action)
        finally:
            structured_delivery._run = original

        self.assertEqual(len(calls), 2)
        self.assertTrue(self.availability_probe_calls)
        latest = get_latest_task_action(task["id"])
        self.assertEqual(latest["state"], "previewing")
        evidence = json.loads(latest["blocked_question"])["schedule_evidence"]
        self.assertEqual(evidence["source"], "copilot-ask")

    def test_scheduler_fallback_fills_missing_provisional_attendee_as_unknown(self):
        task = self._task()
        envelope = structured_delivery.initial_payload(task, "calendar")
        action = create_task_action(
            task["id"],
            delivery_channel="calendar",
            structured_payload=json.dumps(envelope),
        )
        payload = self._payload(task)
        del payload["slots"][0]["availability"]["person-2@x.com"]
        phase_one = self._phase_one_stdout(envelope, payload)
        outputs = [phase_one, "scheduler returned no result marker"]
        calls = []
        original = structured_delivery._run

        def fake_run(argv, timeout=300):
            calls.append(argv)
            return subprocess.CompletedProcess(
                argv, 0, stdout=outputs[len(calls) - 1], stderr=""
            )

        structured_delivery._run = fake_run
        try:
            structured_delivery._preview_worker(task, action)
        finally:
            structured_delivery._run = original

        latest = get_latest_task_action(task["id"])
        self.assertEqual(latest["state"], "previewing", latest.get("error"))
        evidence = json.loads(latest["blocked_question"])["schedule_evidence"]
        self.assertEqual(
            evidence["slots"][0]["availability"]["person-2@x.com"],
            "unknown",
        )


class TestAvailabilityVerification(StructuredDeliveryTestBase):
    """Riveter must check attendee availability instead of believing it.

    Production 2026-08-23 (task 2478, action 258): the preview offered
    Wednesday 26 August 13:05 ET recording every attendee "free", under a
    heading reading "Choose one verified time". Graph reports
    jabali@microsoft.com out of office for the whole of that day.

    The preview worker holds read-only tools and getSchedule is POST, so it
    cannot measure availability -- yet the payload schema has an availability
    field, and finish_preview additionally rejected any value other than
    free/tentative. Honesty was structurally impossible, so the worker guessed
    and the guess certified.

    Availability is read from Graph's scheduleItems: exact instants rather
    than buckets, and a size that follows the number of meetings rather than
    the length of the window. The overlap arithmetic is deliberately Riveter's
    own -- the subprocess that fetches the response is a transport, never the
    judge of whether a slot is free.
    """

    @staticmethod
    def _item(status, start, end):
        """A Graph schedule entry, in the shape getSchedule really returns."""
        return {
            "status": status,
            "start": {"dateTime": start, "timeZone": "UTC"},
            "end": {"dateTime": end, "timeZone": "UTC"},
        }

    @staticmethod
    def _measure(slots, schedules):
        return [{"schedules": schedules} for _ in slots]

    # ---- the half-hour a candidate really occupies ----------------------
    def test_candidate_is_judged_over_its_containing_half_hour(self):
        """1:05-1:30 is booked at 1:05 but consumes the 1:00 half-hour."""
        start, end = structured_delivery._judged_bounds(
            "2026-08-26T13:05:00-04:00", "2026-08-26T13:30:00-04:00"
        )
        self.assertEqual(start.strftime("%H:%M"), "13:00")
        self.assertEqual(end.strftime("%H:%M"), "13:30")

    def test_second_half_hour_snaps_to_the_half(self):
        start, _end = structured_delivery._judged_bounds(
            "2026-08-26T13:35:00-04:00", "2026-08-26T14:00:00-04:00"
        )
        self.assertEqual(start.strftime("%H:%M"), "13:30")

    def test_a_clash_in_the_first_five_minutes_still_counts(self):
        """The reason for snapping: 13:00-13:15 collides with a 13:05 start."""
        items = [self._item(
            "busy", "2026-08-26T17:00:00.0000000", "2026-08-26T17:15:00.0000000"
        )]
        self.assertEqual(structured_delivery._status_from_items(
            items, "2026-08-26T13:05:00-04:00", "2026-08-26T13:30:00-04:00"
        ), "busy")

    # ---- reading Graph's schedule items ---------------------------------
    def test_empty_calendar_reads_as_free(self):
        self.assertEqual(structured_delivery._status_from_items(
            [], "2026-08-26T13:05:00-04:00", "2026-08-26T13:30:00-04:00"
        ), "free")

    def test_out_of_office_is_detected(self):
        """The production case: an all-day absence reported as free."""
        items = [self._item(
            "oof", "2026-08-25T05:00:00.0000000", "2026-08-28T05:00:00.0000000"
        )]
        self.assertEqual(structured_delivery._status_from_items(
            items, "2026-08-26T13:05:00-04:00", "2026-08-26T13:30:00-04:00"
        ), "oof")

    def test_worst_status_across_the_slot_wins(self):
        items = [
            self._item("tentative", "2026-08-26T17:00:00.0000000",
                       "2026-08-26T17:15:00.0000000"),
            self._item("busy", "2026-08-26T17:15:00.0000000",
                       "2026-08-26T17:30:00.0000000"),
        ]
        self.assertEqual(structured_delivery._status_from_items(
            items, "2026-08-26T13:05:00-04:00", "2026-08-26T13:30:00-04:00"
        ), "busy")

    def test_a_meeting_ending_on_the_boundary_does_not_collide(self):
        """Touching at an edge is not an overlap."""
        items = [self._item(
            "busy", "2026-08-26T16:30:00.0000000", "2026-08-26T17:00:00.0000000"
        )]
        self.assertEqual(structured_delivery._status_from_items(
            items, "2026-08-26T13:05:00-04:00", "2026-08-26T13:30:00-04:00"
        ), "free")

    def test_unmeasured_is_not_free(self):
        self.assertIsNone(structured_delivery._status_from_items(
            None, "2026-08-26T13:05:00-04:00", "2026-08-26T13:30:00-04:00"
        ))

    def test_graph_fractional_seconds_are_readable(self):
        """Graph pads to seven digits, which fromisoformat rejects."""
        parsed = structured_delivery._parse_graph_instant(
            {"dateTime": "2026-08-26T17:00:00.0000000", "timeZone": "UTC"}
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.hour, 17)

    # ---- ranking ---------------------------------------------------------
    def test_conflicted_slot_is_offered_rather_than_withdrawn(self):
        """A meeting with three of five is often still the right call."""
        slots = [
            {"value": "0", "label": "Wed", "start": "2026-08-26T13:05:00-04:00",
             "end": "2026-08-26T13:30:00-04:00", "availability": {}},
            {"value": "1", "label": "Later", "start": "2026-08-26T15:05:00-04:00",
             "end": "2026-08-26T15:30:00-04:00", "availability": {}},
        ]
        schedules = [{
            "scheduleId": "jabali@microsoft.com",
            "scheduleItems": [self._item(
                "oof", "2026-08-26T17:00:00.0000000",
                "2026-08-26T17:30:00.0000000",
            )],
        }]
        ranked = structured_delivery._apply_verified_availability(
            ["jabali@microsoft.com"], slots, self._measure(slots, schedules)
        )

        self.assertEqual(len(ranked), 2, "no slot may be silently withdrawn")
        self.assertEqual(ranked[0]["value"], "1")
        self.assertEqual(
            ranked[1]["conflicts"], {"jabali@microsoft.com": "oof"}
        )

    def test_slots_rank_by_how_many_cannot_attend(self):
        slots = [
            {"value": "two-out", "label": "two-out",
             "start": "2026-08-26T12:05:00-04:00",
             "end": "2026-08-26T12:30:00-04:00", "availability": {}},
            {"value": "one-out", "label": "one-out",
             "start": "2026-08-26T13:05:00-04:00",
             "end": "2026-08-26T13:30:00-04:00", "availability": {}},
            {"value": "none-out", "label": "none-out",
             "start": "2026-08-26T14:05:00-04:00",
             "end": "2026-08-26T14:30:00-04:00", "availability": {}},
        ]
        schedules = [
            {"scheduleId": "a@x.com", "scheduleItems": [self._item(
                "busy", "2026-08-26T16:00:00.0000000",
                "2026-08-26T16:30:00.0000000")]},
            {"scheduleId": "b@x.com", "scheduleItems": [self._item(
                "busy", "2026-08-26T16:00:00.0000000",
                "2026-08-26T17:30:00.0000000")]},
        ]
        ranked = structured_delivery._apply_verified_availability(
            ["a@x.com", "b@x.com"], slots, self._measure(slots, schedules)
        )
        self.assertEqual(
            [entry["value"] for entry in ranked],
            ["none-out", "one-out", "two-out"],
        )

    def test_out_of_office_ranks_below_merely_busy(self):
        slots = [
            {"value": "busy", "label": "b",
             "start": "2026-08-26T12:05:00-04:00",
             "end": "2026-08-26T12:30:00-04:00", "availability": {}},
            {"value": "oof", "label": "o",
             "start": "2026-08-26T13:05:00-04:00",
             "end": "2026-08-26T13:30:00-04:00", "availability": {}},
        ]
        schedules = [{"scheduleId": "a@x.com", "scheduleItems": [
            self._item("busy", "2026-08-26T16:00:00.0000000",
                       "2026-08-26T16:30:00.0000000"),
            self._item("oof", "2026-08-26T17:00:00.0000000",
                       "2026-08-26T17:30:00.0000000"),
        ]}]
        ranked = structured_delivery._apply_verified_availability(
            ["a@x.com"], slots, self._measure(slots, schedules)
        )
        self.assertEqual([entry["value"] for entry in ranked], ["busy", "oof"])

    def test_tentative_is_not_a_conflict(self):
        """The standing rule treats tentative as bookable."""
        slots = [{
            "value": "0", "label": "t", "start": "2026-08-26T13:05:00-04:00",
            "end": "2026-08-26T13:30:00-04:00", "availability": {},
        }]
        schedules = [{"scheduleId": "a@x.com", "scheduleItems": [self._item(
            "tentative", "2026-08-26T17:00:00.0000000",
            "2026-08-26T17:30:00.0000000")]}]
        ranked = structured_delivery._apply_verified_availability(
            ["a@x.com"], slots, self._measure(slots, schedules)
        )
        self.assertFalse(ranked[0].get("conflicts"))
        self.assertEqual(ranked[0]["availability"]["a@x.com"], "tentative")

    def test_description_names_who_cannot_attend(self):
        slot = {
            "value": "0",
            "availability": {"a@x.com": "oof", "b@x.com": "busy",
                             "c@x.com": "free"},
            "conflicts": {"a@x.com": "oof", "b@x.com": "busy"},
        }
        names = {"a@x.com": "Jason Balingit", "b@x.com": "Ed Ryan"}
        text = structured_delivery._availability_description(slot, names)
        self.assertIn("Jason Balingit", text)
        self.assertIn("out of office", text)
        self.assertIn("Ed Ryan", text)
        self.assertNotIn("All confirmed attendees are available", text)

    def test_description_may_claim_availability_when_all_are_free(self):
        slot = {"value": "0", "availability": {"a@x.com": "free"}}
        self.assertIn(
            "available", structured_delivery._availability_description(slot)
        )

    # ---- working hours ---------------------------------------------------
    CENTRAL = {
        "daysOfWeek": ["monday", "tuesday", "wednesday", "thursday", "friday"],
        "startTime": "08:00:00.0000000",
        "endTime": "17:00:00.0000000",
        "timeZone": {"name": "Central Standard Time"},
    }
    EASTERN = dict(CENTRAL, timeZone={"name": "Eastern Standard Time"})

    def test_slot_inside_working_hours_is_fine(self):
        self.assertIsNone(structured_delivery._working_hours_status(
            self.CENTRAL,
            "2026-08-26T13:05:00-04:00", "2026-08-26T13:30:00-04:00",
        ))

    def test_evening_slot_just_past_an_eastern_day_is_a_reasonable_ask(self):
        self.assertEqual(structured_delivery._working_hours_status(
            self.EASTERN,
            "2026-08-26T17:05:00-04:00", "2026-08-26T17:30:00-04:00",
        ), "nearWorkingHours")

    def test_slot_well_past_the_day_is_outside_working_hours(self):
        self.assertEqual(structured_delivery._working_hours_status(
            self.EASTERN,
            "2026-08-26T19:05:00-04:00", "2026-08-26T19:30:00-04:00",
        ), "outsideWorkingHours")

    def test_same_evening_slot_is_inside_a_central_day(self):
        """The same instant is 16:05 Central, still a working hour."""
        self.assertIsNone(structured_delivery._working_hours_status(
            self.CENTRAL,
            "2026-08-26T17:05:00-04:00", "2026-08-26T17:30:00-04:00",
        ))

    def test_weekend_is_outside_the_working_week(self):
        self.assertEqual(structured_delivery._working_hours_status(
            self.CENTRAL,
            "2026-08-29T13:05:00-04:00", "2026-08-29T13:30:00-04:00",
        ), "outsideWorkingHours")

    def test_missing_working_hours_is_not_treated_as_a_conflict(self):
        self.assertIsNone(
            structured_delivery._working_hours_status(None, "x", "y")
        )
        self.assertIsNone(structured_delivery._working_hours_status(
            {"timeZone": {"name": "Nowhere Standard Time"}},
            "2026-08-26T13:05:00-04:00", "2026-08-26T13:30:00-04:00",
        ))

    def test_a_near_miss_is_noted_but_never_blocks(self):
        slots = [{
            "value": "0", "label": "evening",
            "start": "2026-08-26T17:05:00-04:00",
            "end": "2026-08-26T17:30:00-04:00", "availability": {},
        }]
        schedules = [{
            "scheduleId": "ryanedward@microsoft.com",
            "scheduleItems": [],
            "workingHours": self.EASTERN,
        }]
        ranked = structured_delivery._apply_verified_availability(
            ["ryanedward@microsoft.com"], slots, self._measure(slots, schedules)
        )
        self.assertFalse(ranked[0].get("conflicts"), "a near miss is not a veto")
        text = structured_delivery._availability_description(
            ranked[0], {"ryanedward@microsoft.com": "Ed Ryan"}
        )
        self.assertIn("just outside working hours", text)

    def test_a_clean_slot_still_outranks_a_near_miss(self):
        slots = [
            {"value": "near", "label": "n",
             "start": "2026-08-26T17:05:00-04:00",
             "end": "2026-08-26T17:30:00-04:00", "availability": {}},
            {"value": "clean", "label": "c",
             "start": "2026-08-26T13:05:00-04:00",
             "end": "2026-08-26T13:30:00-04:00", "availability": {}},
        ]
        schedules = [{"scheduleId": "a@x.com", "scheduleItems": [],
                      "workingHours": self.EASTERN}]
        ranked = structured_delivery._apply_verified_availability(
            ["a@x.com"], slots, self._measure(slots, schedules)
        )
        self.assertEqual([entry["value"] for entry in ranked], ["clean", "near"])

    def test_being_busy_outranks_being_well_out_of_hours(self):
        """An evening ask is smaller than a double booking."""
        slots = [
            {"value": "busy", "label": "b",
             "start": "2026-08-26T13:05:00-04:00",
             "end": "2026-08-26T13:30:00-04:00", "availability": {}},
            {"value": "late", "label": "l",
             "start": "2026-08-26T20:05:00-04:00",
             "end": "2026-08-26T20:30:00-04:00", "availability": {}},
        ]
        schedules = [{
            "scheduleId": "a@x.com",
            "scheduleItems": [self._item(
                "busy", "2026-08-26T17:00:00.0000000",
                "2026-08-26T17:30:00.0000000")],
            "workingHours": self.EASTERN,
        }]
        ranked = structured_delivery._apply_verified_availability(
            ["a@x.com"], slots, self._measure(slots, schedules)
        )
        self.assertEqual([entry["value"] for entry in ranked], ["late", "busy"])

    # ---- the probe -------------------------------------------------------
    def test_probe_asks_for_schedule_items_not_a_verdict(self):
        prompt = structured_delivery.availability_prompt(
            ["a@x.com", "b@x.com"],
            [("2026-08-26T17:00:00", "2026-08-26T17:30:00")],
        )
        self.assertIn("/me/calendar/getSchedule", prompt)
        self.assertIn("a@x.com", prompt)
        self.assertIn("scheduleItems", prompt)
        self.assertIn("workingHours", prompt)
        lowered = prompt.lower()
        self.assertTrue(
            "do not interpret" in lowered or "verbatim" in lowered,
            "the worker must return the raw response, not a verdict",
        )

    def test_probe_omits_the_unused_availability_view(self):
        prompt = structured_delivery.availability_prompt(
            ["a@x.com"],
            [("2026-08-26T17:00:00", "2026-08-26T17:30:00")],
        )
        self.assertNotIn("availabilityViewInterval", prompt)
        self.assertIn("scheduleItems", prompt)

    def test_prompt_numbers_every_window_in_one_probe(self):
        prompt = structured_delivery.availability_prompt(
            ["a@x.com"],
            [
                ("2026-08-26T17:00:00", "2026-08-26T17:30:00"),
                ("2026-08-27T18:00:00", "2026-08-27T18:30:00"),
            ],
        )
        self.assertIn("0. actionUrl", prompt)
        self.assertIn("1. actionUrl", prompt)

    def test_probe_cannot_send_anything(self):
        argv = structured_delivery.availability_command("prompt")
        joined = " ".join(argv)
        self.assertIn("workiq-do_action", joined)
        self.assertNotIn("workiq-create_entity", joined)

    def test_parses_windows_out_of_a_result_block(self):
        raw = (
            "noise before\n"
            + structured_delivery.RESULT_START
            + '{"windows":[{"index":0,"schedules":[{"scheduleId":"a@x.com",'
            '"scheduleItems":[]}]}]}'
            + structured_delivery.RESULT_END
            + "\nnoise after"
        )
        windows = structured_delivery._parse_windows(raw)
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0][0]["scheduleId"], "a@x.com")

    def test_parses_distinct_numbered_windows(self):
        raw = (
            structured_delivery.RESULT_START
            + '{"windows":['
            '{"index":0,"schedules":[{"scheduleId":"a@x.com","scheduleItems":[]}]},'
            '{"index":1,"schedules":[{"scheduleId":"b@x.com","scheduleItems":[]}]}'
            "]}"
            + structured_delivery.RESULT_END
        )
        windows = structured_delivery._parse_windows(raw)
        self.assertEqual(windows[0][0]["scheduleId"], "a@x.com")
        self.assertEqual(windows[1][0]["scheduleId"], "b@x.com")

    def test_unreadable_response_yields_nothing(self):
        self.assertIsNone(structured_delivery._parse_windows(""))
        self.assertIsNone(structured_delivery._parse_windows("no block here"))

    @staticmethod
    def _probe_stdout(entries):
        return (
            structured_delivery.RESULT_START
            + json.dumps({"windows": entries})
            + structured_delivery.RESULT_END
        )

    @staticmethod
    def _two_day_slots():
        return [
            {
                "value": "0",
                "start": "2099-08-26T13:05:00-04:00",
                "end": "2099-08-26T13:30:00-04:00",
            },
            {
                "value": "1",
                "start": "2099-08-27T14:05:00-04:00",
                "end": "2099-08-27T14:30:00-04:00",
            },
        ]

    def test_small_multi_day_probe_uses_one_subprocess_and_maps_indices(self):
        """Day two must receive window index 1, never index 0 again."""
        calls = []
        stdout = self._probe_stdout([
            {
                "index": 0,
                "schedules": [
                    {
                        "scheduleId": email,
                        "scheduleItems": [],
                        "marker": "day-one",
                    }
                    for email in ("a@x.com", "b@x.com", "c@x.com")
                ],
            },
            {
                "index": 1,
                "schedules": [
                    {
                        "scheduleId": email,
                        "scheduleItems": [],
                        "marker": "day-two",
                    }
                    for email in ("a@x.com", "b@x.com", "c@x.com")
                ],
            },
        ])
        original = structured_delivery._run

        def fake_run(argv, timeout=300):
            calls.append((argv, timeout))
            return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

        structured_delivery._run = fake_run
        try:
            measured = self._real_fetch_availability(
                ["a@x.com", "b@x.com", "c@x.com"],
                self._two_day_slots(),
            )
        finally:
            structured_delivery._run = original

        self.assertEqual(len(calls), 1)
        self.assertIn("1. actionUrl", " ".join(calls[0][0]))
        self.assertEqual(
            measured[0]["schedules"][0]["marker"], "day-one"
        )
        self.assertEqual(
            measured[1]["schedules"][0]["marker"], "day-two"
        )

    def test_batch_retry_preserves_first_attempt_success_and_fills_gap(self):
        """Retrying the whole batch must not discard a window already measured."""
        outputs = [
            self._probe_stdout([{
                "index": 0,
                "schedules": [
                    {
                        "scheduleId": email,
                        "scheduleItems": [],
                        "marker": "first",
                    }
                    for email in ("a@x.com", "b@x.com", "c@x.com")
                ],
            }]),
            self._probe_stdout([{
                "index": 1,
                "schedules": [
                    {
                        "scheduleId": email,
                        "scheduleItems": [],
                        "marker": "second",
                    }
                    for email in ("a@x.com", "b@x.com", "c@x.com")
                ],
            }]),
        ]
        calls = []
        original = structured_delivery._run

        def fake_run(argv, timeout=300):
            calls.append(argv)
            return subprocess.CompletedProcess(
                argv, 0, stdout=outputs[len(calls) - 1], stderr=""
            )

        structured_delivery._run = fake_run
        try:
            measured = self._real_fetch_availability(
                ["a@x.com", "b@x.com", "c@x.com"],
                self._two_day_slots(),
            )
        finally:
            structured_delivery._run = original

        self.assertEqual(len(calls), 2)
        self.assertEqual(measured[0]["schedules"][0]["marker"], "first")
        self.assertEqual(measured[1]["schedules"][0]["marker"], "second")

    def test_batch_retry_merges_attendees_within_the_same_window(self):
        """A nonempty window is not complete until every attendee is present."""
        outputs = [
            self._probe_stdout([{
                "index": 0,
                "schedules": [{"scheduleId": "a@x.com", "scheduleItems": []}],
            }]),
            self._probe_stdout([{
                "index": 0,
                "schedules": [{"scheduleId": "b@x.com", "scheduleItems": []}],
            }]),
        ]
        calls = []
        original = structured_delivery._run

        def fake_run(argv, timeout=300):
            calls.append(argv)
            return subprocess.CompletedProcess(
                argv, 0, stdout=outputs[len(calls) - 1], stderr=""
            )

        structured_delivery._run = fake_run
        try:
            measured = self._real_fetch_availability(
                ["a@x.com", "b@x.com"],
                [self._two_day_slots()[0]],
            )
        finally:
            structured_delivery._run = original

        self.assertEqual(len(calls), 2)
        self.assertEqual(
            {
                entry["scheduleId"]
                for entry in measured[0]["schedules"]
            },
            {"a@x.com", "b@x.com"},
        )

    def test_batch_stops_after_two_unreadable_attempts(self):
        calls = []
        original = structured_delivery._run

        def fake_run(argv, timeout=300):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        structured_delivery._run = fake_run
        try:
            measured = self._real_fetch_availability(
                ["a@x.com", "b@x.com", "c@x.com"],
                self._two_day_slots(),
            )
        finally:
            structured_delivery._run = original

        self.assertIsNone(measured)
        self.assertEqual(len(calls), 2)

    def test_batch_retries_real_timeout_then_returns_none(self):
        calls = []
        original = structured_delivery._run

        def fake_run(argv, timeout=300):
            calls.append(argv)
            raise subprocess.TimeoutExpired(argv, timeout)

        structured_delivery._run = fake_run
        try:
            measured = self._real_fetch_availability(
                ["a@x.com", "b@x.com"],
                self._two_day_slots(),
            )
        finally:
            structured_delivery._run = original

        self.assertIsNone(measured)
        self.assertEqual(len(calls), 2)

    def test_batch_keeps_partial_coverage_after_retry_exhausts(self):
        first = self._probe_stdout([{
            "index": 0,
            "schedules": [
                {"scheduleId": email, "scheduleItems": []}
                for email in ("a@x.com", "b@x.com")
            ],
        }])
        outputs = [first, first]
        calls = []
        original = structured_delivery._run

        def fake_run(argv, timeout=300):
            calls.append(argv)
            return subprocess.CompletedProcess(
                argv, 0, stdout=outputs[len(calls) - 1], stderr=""
            )

        structured_delivery._run = fake_run
        try:
            measured = self._real_fetch_availability(
                ["a@x.com", "b@x.com"],
                self._two_day_slots(),
            )
        finally:
            structured_delivery._run = original

        self.assertEqual(len(calls), 2)
        self.assertIsNotNone(measured[0]["schedules"])
        self.assertIsNone(measured[1]["schedules"])

    def test_twelve_attendee_days_use_per_day_fallback(self):
        """Task 2478's 6 attendees x 2 days is the fallback boundary."""
        self.assertEqual(
            structured_delivery.BATCH_THRESHOLD_ATTENDEE_DAYS, 12
        )
        calls = []
        original = structured_delivery._run
        attendees = [f"person-{index}@x.com" for index in range(6)]

        def fake_run(argv, timeout=300):
            calls.append(argv)
            joined = " ".join(argv)
            marker = "day-one" if "2099-08-26" in joined else "day-two"
            stdout = self._probe_stdout([{
                "index": 0,
                "schedules": [
                    {
                        "scheduleId": email,
                        "scheduleItems": [],
                        "marker": marker,
                    }
                    for email in attendees
                ],
            }])
            return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

        structured_delivery._run = fake_run
        try:
            measured = self._real_fetch_availability(
                attendees,
                self._two_day_slots(),
            )
        finally:
            structured_delivery._run = original

        self.assertEqual(len(calls), 2)
        self.assertEqual(len(measured), 2)
        self.assertEqual(measured[0]["schedules"][0]["marker"], "day-one")
        self.assertEqual(measured[1]["schedules"][0]["marker"], "day-two")

    def test_same_day_slots_share_one_measured_window(self):
        slots = [
            {
                "start": "2099-08-26T13:05:00-04:00",
                "end": "2099-08-26T13:30:00-04:00",
            },
            {
                "start": "2099-08-26T15:05:00-04:00",
                "end": "2099-08-26T15:30:00-04:00",
            },
        ]
        calls = []
        original = structured_delivery._run

        def fake_run(argv, timeout=300):
            calls.append(argv)
            stdout = self._probe_stdout([{
                "index": 0,
                "schedules": [{"scheduleId": "a@x.com", "scheduleItems": []}],
            }])
            return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

        structured_delivery._run = fake_run
        try:
            measured = self._real_fetch_availability(["a@x.com"], slots)
        finally:
            structured_delivery._run = original

        self.assertEqual(len(calls), 1)
        self.assertEqual(measured[0], measured[1])

    # ---- wiring into preview --------------------------------------------
    def _calendar_preview(self, probe):
        """Drive a real finish_preview with a controllable availability probe."""
        task = create_task("Schedule the kickoff", action_type="schedule-meeting")
        envelope = structured_delivery.initial_payload(task, "calendar")
        action = create_task_action(
            task["id"],
            delivery_channel="calendar",
            structured_payload=json.dumps(envelope),
        )
        structured_delivery.fetch_availability = probe
        duration = structured_delivery._meeting_duration(task)
        payload = {
            "schema_version": 1,
            "channel": "calendar",
            "subject": "Kickoff",
            "body": "Agenda",
            "duration_minutes": duration,
            "timezone": "Eastern Standard Time",
            "attendees": [{"name": "A", "email": "a@x.com"}],
            "slots": [
                {"id": "0", "label": "Wed",
                 "start": "2099-08-26T13:05:00-04:00",
                 "timezone": "Eastern Standard Time",
                 "availability": {"a@x.com": "free"}},
                {"id": "1", "label": "Thu",
                 "start": "2099-08-27T13:05:00-04:00",
                 "timezone": "Eastern Standard Time",
                 "availability": {"a@x.com": "free"}},
            ],
        }
        for slot in payload["slots"]:
            begin = datetime.fromisoformat(slot["start"])
            slot["end"] = (begin + timedelta(minutes=duration)).isoformat()
        stdout = (
            structured_delivery.RESULT_START + "\n"
            + json.dumps({
                "correlation_id": envelope["correlation_id"],
                "phase": "preview",
                "ok": True,
                "payload": payload,
            })
            + "\n" + structured_delivery.RESULT_END
        )
        return action, structured_delivery.finish_preview(
            action["id"],
            stdout=stdout,
            stderr="",
            exit_code=0,
            correlation_id=envelope["correlation_id"],
        )

    def test_preview_consults_the_probe(self):
        seen = []

        def probe(attendees, slots):
            seen.append((attendees, slots))
            return None

        self._calendar_preview(probe)
        self.assertEqual(len(seen), 1, "preview must measure availability")
        self.assertEqual(seen[0][0], ["a@x.com"])
        self.assertEqual(len(seen[0][1]), 2)

    def test_preview_ranks_a_conflicted_slot_below_a_clear_one(self):
        def probe(attendees, slots):
            return [
                {"schedules": [{"scheduleId": "a@x.com", "scheduleItems": [{
                    "status": "oof",
                    "start": {"dateTime": "2099-08-26T17:00:00.0000000",
                              "timeZone": "UTC"},
                    "end": {"dateTime": "2099-08-26T17:30:00.0000000",
                            "timeZone": "UTC"},
                }]}]},
                {"schedules": [{"scheduleId": "a@x.com", "scheduleItems": []}]},
            ]

        _action, updated = self._calendar_preview(probe)
        interaction = json.loads(updated["blocked_question"])
        evidence = interaction["schedule_evidence"]

        self.assertTrue(evidence["availability_verified"])
        self.assertEqual([s["value"] for s in evidence["slots"]], ["1", "0"])
        options = interaction["questions"][0]["options"]
        self.assertEqual(options[0]["description"],
                         "All confirmed attendees are available.")
        self.assertIn("out of office", options[1]["description"])

    def test_preview_records_when_availability_could_not_be_measured(self):
        _action, updated = self._calendar_preview(lambda a, s: None)
        interaction = json.loads(updated["blocked_question"])
        evidence = interaction["schedule_evidence"]

        self.assertFalse(evidence["availability_verified"])
        self.assertEqual(len(evidence["slots"]), 2)
        # It must not imply these times were checked.
        question = interaction["questions"][0]["question"]
        self.assertIn("unchecked", question)
        self.assertNotIn("verified time", question)

    def test_an_unverified_preview_can_still_be_booked(self):
        """Measurement is best-effort, not a gate.

        The probe runs through a subprocess that takes minutes and often
        returns nothing. Refusing every unmeasured selection turned a useful
        check into a broken feature, so an unverified preview still books --
        it just says plainly that the times were not checked.
        """
        from src.services.cowork_runner import schedule_interaction_is_certified

        _action, updated = self._calendar_preview(lambda a, s: None)
        interaction = json.loads(updated["blocked_question"])
        self.assertFalse(
            interaction["schedule_evidence"]["availability_verified"]
        )
        self.assertTrue(schedule_interaction_is_certified(
            interaction,
            [{"name": "A", "email": "a@x.com"}],
            interaction["schedule_evidence"]["duration_minutes"],
        ))

    def test_unmeasured_availability_is_never_reported_as_free(self):
        """A claim the probe could not confirm must not be dressed as measurement.

        Task 2558 asked for a Pega meeting, the probe returned nothing, and
        the card still showed both attendees in green marked "free" --
        beside its own sentence saying the calendars could not be read. The
        worker's claim is precisely the thing that was never checked, so it
        is precisely what must not survive into evidence the UI renders as
        fact.
        """
        _action, updated = self._calendar_preview(lambda a, s: None)
        interaction = json.loads(updated["blocked_question"])
        evidence = interaction["schedule_evidence"]

        self.assertFalse(evidence["availability_verified"])
        for slot in evidence["slots"]:
            self.assertEqual(
                sorted({str(v) for v in slot["availability"].values()}),
                ["unknown"],
                "unmeasured availability must read unknown, never free",
            )

    def test_unmeasured_slots_do_not_claim_everyone_is_available(self):
        """The option text is read as the answer, so it must not assert one."""
        _action, updated = self._calendar_preview(lambda a, s: None)
        interaction = json.loads(updated["blocked_question"])

        for option in interaction["questions"][0]["options"]:
            self.assertNotIn("are available", option["description"])
            self.assertIn("not checked", option["description"].lower())

    def test_only_the_unmeasured_slot_loses_its_claim(self):
        """Partial measurement must not punish the slot that was measured."""
        def probe(attendees, slots):
            return [
                {"schedules": [{"scheduleId": "a@x.com", "scheduleItems": []}]},
                {"schedules": None},
            ]

        _action, updated = self._calendar_preview(probe)
        evidence = json.loads(updated["blocked_question"])["schedule_evidence"]
        by_value = {s["value"]: s["availability"] for s in evidence["slots"]}

        self.assertEqual(by_value["0"]["a@x.com"], "free")
        self.assertEqual(by_value["1"]["a@x.com"], "unknown")

    def test_workiq_drafts_get_the_same_voice_as_cowork_drafts(self):
        """The voice settings are read by one engine and ignored by the other.

        cowork_voice names a skill per channel and cowork_runner renders it
        into every draft it writes. structured_delivery never called it, so a
        Teams message or email drafted by WorkIQ came out with no voice
        guidance at all -- and routing keeps moving work onto that path.
        """
        from src.services import cowork_runner

        task = create_task("Reply to Sally", action_type="respond-email")
        original = cowork_runner.voice_skill
        try:
            cowork_runner.voice_skill = lambda channel: (
                "work-email-voice" if channel == "email" else "work-teams-voice"
            )
            for channel, skill in (
                ("email", "work-email-voice"),
                ("teams", "work-teams-voice"),
            ):
                payload = structured_delivery.initial_payload(task, channel)
                prompt = structured_delivery.preview_prompt(task, payload)
                self.assertIn(skill, prompt, f"{channel} draft has no voice")
        finally:
            cowork_runner.voice_skill = original

    def test_a_calendar_preview_carries_the_standing_meeting_notes(self):
        """meeting_preferences.notes is validated, capped, and then dropped.

        cowork_runner renders it; the structured path read only duration and
        offset, so a standing instruction written into notes never reached
        the worker that now books most meetings.
        """
        from src.services import cowork_runner

        task = create_task("Schedule a review", action_type="schedule-meeting")
        original = cowork_runner.meeting_preferences
        try:
            cowork_runner.meeting_preferences = lambda: {
                "default_minutes": 25,
                "start_offset_minutes": 5,
                "notes": "never book me before 9am",
            }
            payload = structured_delivery.initial_payload(task, "calendar")
            prompt = structured_delivery.preview_prompt(task, payload)
            self.assertIn("never book me before 9am", prompt)
        finally:
            cowork_runner.meeting_preferences = original

    def test_an_unset_offset_is_not_stated_as_a_rule(self):
        """A missing settings file must not become an invented preference.

        settings.json was lost in a checkout migration. meeting_preferences()
        returned nothing, the offset fell back to 0, and the prompt asserted
        "Start suggestions at :00 or :30" as though Phil had chosen it -- so
        the times came back on the hour and the real :05 rule looked
        forgotten. Saying nothing is the honest fallback; the worker can pick
        sensible times without being handed a rule nobody set.
        """
        from src.services import cowork_runner

        task = create_task("Schedule a review", action_type="schedule-meeting")
        original = cowork_runner.meeting_preferences
        try:
            cowork_runner.meeting_preferences = lambda: None
            payload = structured_delivery.initial_payload(task, "calendar")
            prompt = structured_delivery.preview_prompt(task, payload)
            self.assertNotIn("Start suggestions at", prompt)
            # And it must not claim a standing duration Phil never set.
            self.assertNotIn("standing meeting duration", prompt)
            self.assertIn("25 minutes", prompt)

            cowork_runner.meeting_preferences = lambda: {
                "default_minutes": 25, "start_offset_minutes": 5,
            }
            prompt = structured_delivery.preview_prompt(task, payload)
            self.assertIn("Start suggestions at :05 or :35", prompt)
            self.assertIn("standing meeting duration is 25", prompt)
        finally:
            cowork_runner.meeting_preferences = original

    def test_an_offset_of_zero_is_a_choice_not_an_absence(self):
        """`or 0` made a configured on-the-hour rule indistinguishable
        from no rule at all."""
        from src.services import cowork_runner

        task = create_task("Schedule a review", action_type="schedule-meeting")
        original = cowork_runner.meeting_preferences
        try:
            cowork_runner.meeting_preferences = lambda: {
                "start_offset_minutes": 0,
            }
            payload = structured_delivery.initial_payload(task, "calendar")
            prompt = structured_delivery.preview_prompt(task, payload)
            self.assertIn("Start suggestions at :00 or :30", prompt)
        finally:
            cowork_runner.meeting_preferences = original

    def test_calendar_preview_gets_a_longer_budget_than_a_message(self):
        """Two calendar previews died at exactly 301s on the 300s default.

        Asking WorkIQ to find candidate times across several attendees'
        calendars is a slower question than drafting a message, and a run
        killed on the budget leaves nothing to show for the minutes it spent
        -- task 2558 burned two of them before anyone looked.
        """
        self.assertGreater(
            structured_delivery._preview_timeout("calendar"),
            structured_delivery._preview_timeout("teams"),
        )
        self.assertGreaterEqual(
            structured_delivery._preview_timeout("calendar"), 420
        )

    def test_preview_worker_applies_the_calendar_budget(self):
        """The budget only matters if the worker actually passes it."""
        task = create_task("Schedule a review", action_type="schedule-meeting")
        envelope = structured_delivery.initial_payload(task, "calendar")
        action = create_task_action(
            task["id"],
            delivery_channel="calendar",
            structured_payload=json.dumps(envelope),
        )
        seen = {}
        original = structured_delivery._run

        def fake_run(argv, timeout=300):
            seen["timeout"] = timeout
            raise RuntimeError("stop before the real subprocess")

        structured_delivery._run = fake_run
        try:
            structured_delivery._preview_worker(task, action)
        finally:
            structured_delivery._run = original

        self.assertEqual(
            seen.get("timeout"),
            structured_delivery._preview_timeout("calendar"),
        )

    def test_coverage_distinguishes_partly_measured_from_not_measured(self):
        """"Not all measured" and "none measured" are different facts.

        availability_verified is one boolean over a set of slots that can each
        have a different measurement state. Task 2610 measured two slots free
        and failed to measure a third, and the card announced that the
        calendars "could not be read" -- overstating the failure exactly as
        claiming "free" overstated the success.
        """
        def partial(attendees, slots):
            return [
                {"schedules": [{"scheduleId": "a@x.com", "scheduleItems": []}]},
                {"schedules": None},
            ]

        _action, updated = self._calendar_preview(partial)
        evidence = json.loads(updated["blocked_question"])["schedule_evidence"]
        self.assertEqual(evidence["availability_coverage"], "partial")

        _action, updated = self._calendar_preview(lambda a, s: None)
        evidence = json.loads(updated["blocked_question"])["schedule_evidence"]
        self.assertEqual(evidence["availability_coverage"], "none")

        def full(attendees, slots):
            return [
                {"schedules": [{"scheduleId": "a@x.com", "scheduleItems": []}]},
                {"schedules": [{"scheduleId": "a@x.com", "scheduleItems": []}]},
            ]

        _action, updated = self._calendar_preview(full)
        evidence = json.loads(updated["blocked_question"])["schedule_evidence"]
        self.assertEqual(evidence["availability_coverage"], "full")

    def test_a_partly_measured_preview_does_not_claim_nothing_was_read(self):
        """The question text must match which slots were actually measured."""
        def partial(attendees, slots):
            return [
                {"schedules": [{"scheduleId": "a@x.com", "scheduleItems": []}]},
                {"schedules": None},
            ]

        _action, updated = self._calendar_preview(partial)
        question = json.loads(
            updated["blocked_question"]
        )["questions"][0]["question"]
        self.assertNotIn("could not read the attendees' calendars", question)
        self.assertIn("some", question.lower())

    def test_partly_measured_is_not_verified(self):
        """One day's window can succeed while another fails.

        Verified must mean every candidate was measured, not that the call
        returned something -- otherwise an unmeasured slot rides along on its
        neighbour's certificate.
        """
        def probe(attendees, slots):
            return [
                {"schedules": [{"scheduleId": "a@x.com", "scheduleItems": []}]},
                {"schedules": None},
            ]

        _action, updated = self._calendar_preview(probe)
        interaction = json.loads(updated["blocked_question"])
        evidence = interaction["schedule_evidence"]

        self.assertFalse(evidence["availability_verified"])
        # The measured slot still gets its real answer.
        self.assertEqual(len(evidence["slots"]), 2)

    def test_preview_offers_the_best_of_a_bad_set_and_invites_steering(self):
        """Nobody is free. Offer the least-bad times and say who is missing."""
        def probe(attendees, slots):
            blocked = [{"scheduleId": "a@x.com", "scheduleItems": [{
                "status": "oof",
                "start": {"dateTime": "2099-08-01T00:00:00.0000000",
                          "timeZone": "UTC"},
                "end": {"dateTime": "2099-09-01T00:00:00.0000000",
                        "timeZone": "UTC"},
            }]}]
            return [{"schedules": blocked} for _ in slots]

        _action, updated = self._calendar_preview(probe)
        interaction = json.loads(updated["blocked_question"])

        self.assertEqual(updated["state"], "previewing")
        options = interaction["questions"][0]["options"]
        self.assertEqual(len(options), 2, "the user must still have a choice")
        for option in options:
            self.assertIn("out of office", option["description"])
        question = interaction["questions"][0]["question"]
        self.assertIn("No time suits everyone", question)
        self.assertIn("matters most", question)


class TestTeamsDestinationDiscovery(StructuredDeliveryTestBase):
    """The Teams preview worker must be told where chats live.

    Production 2026-08-23 (task 2125): the worker returned "The exact Teams
    chat ID is not exposed by the available read-only WorkIQ metadata" and
    gave up. That is false -- PREVIEW_TOOLS includes workiq-fetch, and
    /me/chats returns ids. Calendar gets a standing-duration rule and agenda
    guidance, email gets reply-recipient guidance, and Teams got an empty
    string: only "resolve every delivery identifier" and a schema with a
    chat_id field, with nothing saying where to look. The endpoints were
    already named in the EXECUTE prompt, so the knowledge existed in the
    codebase -- it just never reached the worker that had to do the lookup.
    """

    def _teams_prompt(self):
        task = create_task(
            "Message the project chat about Lighthouse",
            action_type="teams-message",
            source_type="manual",
        )
        payload = structured_delivery.initial_payload(task, "teams")
        return structured_delivery.preview_prompt(task, payload)

    def test_prompt_names_the_chat_listing_endpoint(self):
        prompt = self._teams_prompt()
        self.assertIn("/me/chats", prompt)

    def test_prompt_says_how_to_match_a_chat(self):
        prompt = self._teams_prompt()
        lowered = self._teams_prompt().lower()
        self.assertIn("topic", lowered)
        self.assertTrue(
            "member" in lowered or "participant" in lowered,
            "the worker must be told it can match a chat by who is in it",
        )

    def test_prompt_does_not_leave_teams_without_channel_guidance(self):
        """Teams was the one channel whose guidance was an empty string."""
        teams = self._teams_prompt()
        self.assertIn("workiq-fetch", teams)
        self.assertTrue(
            any(word in teams.lower() for word in ("chat", "channel")),
            "teams guidance must talk about chats or channels",
        )

    def test_prompt_refuses_invented_identifiers(self):
        prompt = self._teams_prompt().lower()
        self.assertTrue(
            "never invent" in prompt or "do not invent" in prompt,
            "resolution guidance must not licence guessing an id",
        )


class TestTeamsBodyFidelity(StructuredDeliveryTestBase):
    """Graph wants an itemBody object, not a string.

    Observed in production 2026-08-23 (tasks 2592 and 2593): the Teams execute
    prompt said only "post the exact body from the payload", the worker passed
    the payload's plain string straight into chatMessage.body, and Graph
    rejected every send with "Property body in payload has a value that does
    not match schema". 2593's body had no newlines at all and still failed, so
    this is the body's *type*, not its whitespace. A spike against real Graph
    confirmed {"contentType":"html","content": ...} returns 201 and preserves
    <br> breaks. Riveter approves a specific artifact, so Riveter renders the
    wire format rather than leaving the shape to the worker -- the same rule
    already applied to email.
    """

    TEAMS_PAYLOAD = {
        "schema_version": 1,
        "channel": "teams",
        "destination_kind": "chat",
        "chat_id": "19:abc@thread.v2",
        "destination_display": "Project chat",
        "body": "Structured Teams delivery test.\nLine two.\nLine three.",
    }

    def test_prompt_dictates_the_itembody_shape(self):
        prompt = structured_delivery.execute_prompt(self.TEAMS_PAYLOAD, "corr-1")
        compact = prompt.lower().replace(" ", "")
        self.assertIn('"contenttype":"html"', compact)
        self.assertIn('"content"', compact)

    def test_prompt_carries_the_rendered_html_body(self):
        prompt = structured_delivery.execute_prompt(self.TEAMS_PAYLOAD, "corr-1")
        rendered = structured_delivery.plain_text_to_html(
            self.TEAMS_PAYLOAD["body"]
        )
        self.assertIn(rendered, prompt)
        # The breaks the user approved must reach the wire format.
        self.assertIn("Line two.<br>", rendered)

    def test_prompt_does_not_ask_for_the_bare_payload_body(self):
        """The old wording is what produced the BadRequest."""
        prompt = structured_delivery.execute_prompt(self.TEAMS_PAYLOAD, "corr-1")
        self.assertNotIn("Post the exact body from the payload", prompt)

    def test_recovery_matches_against_the_rendered_body(self):
        """Look-before-write compares to what was actually posted, i.e. HTML."""
        prompt = structured_delivery.execute_prompt(
            self.TEAMS_PAYLOAD, "corr-1", recover=True
        )
        rendered = structured_delivery.plain_text_to_html(
            self.TEAMS_PAYLOAD["body"]
        )
        self.assertIn(rendered, prompt)


class TestTeamsMessageRouting(StructuredDeliveryTestBase):
    """A manually written Teams task must be able to reach the Teams channel.

    Production 2026-08-23 (task 2125): "Send phil topness a teams message
    about the lighthouse program" parsed to action_type "general" and so fell
    through to Cowork, which reported terminal_status ok with unnamed tool
    calls and no delivery evidence. Teams routing keyed only on source_type,
    which assumes the task came FROM a Teams thread; a typed instruction never
    can. `teams-message` already existed as a skill, a UI label and a valid
    skill name - only the schema and the router had never learned it.
    """

    def test_teams_message_action_routes_to_teams_regardless_of_source(self):
        for source in ("manual", "chat", "email", "meeting"):
            task = {
                "action_type": "teams-message",
                "source_type": source,
            }
            self.assertEqual(
                structured_delivery.channel_for_task(task), "teams",
                f"teams-message must route to teams from source {source!r}",
            )

    def test_a_resolved_teams_conversation_routes_to_teams(self):
        """The same failure, arriving through a different door.

        Production 2026-08-25 (task 2521): a follow-up whose source_url is a
        Teams chat link, so the destination resolved to a real one-to-one
        conversation - and it still went to Cowork, because routing asked
        source_type, which is "manual" for anything the user typed themselves.
        Cowork's direct-action path is off by default, so pressing Send
        returned 409 "Direct actions require the Cowork API transport." and the
        message could not be sent at all.

        source_type records where the task CAME FROM. A pasted Teams link says
        where it is GOING, which is the question routing is actually asking.
        """
        task = {
            "action_type": "follow-up",
            "source_type": "manual",
            # The exact shape Teams produces for a 1:1 chat link, taken from
            # task 2521. An invented "/l/chat/<id>/<msg>" does NOT parse - the
            # trailing /conversations segment is part of the format.
            "source_url": (
                "https://teams.microsoft.com/l/chat/"
                "19:aaa_bbb@unq.gbl.spaces/conversations"
                "?context=%7B%22contextType%22%3A%22chat%22%7D"
            ),
        }
        self.assertEqual(structured_delivery.channel_for_task(task), "teams")

    def test_a_group_thread_link_routes_to_teams(self):
        task = {
            "action_type": "awaiting-response",
            "source_type": "manual",
            "source_url": (
                "https://teams.microsoft.com/l/message/"
                "19:ccc@thread.v2/1756000000001"
            ),
        }
        self.assertEqual(structured_delivery.channel_for_task(task), "teams")

    def test_a_readable_meeting_source_routes_follow_up_to_workiq_teams(self):
        task = {
            "action_type": "follow-up",
            "source_type": "meeting",
            "source_url": (
                "https://teams.microsoft.com/l/meeting/details"
                "?eventId=AAMkMeeting%3d"
            ),
        }
        self.assertEqual(structured_delivery.channel_for_task(task), "teams")

    def test_an_unreadable_meeting_source_does_not_imply_teams_delivery(self):
        task = {
            "action_type": "follow-up",
            "source_type": "meeting",
            "source_url": "https://teams.microsoft.com/l/meeting/details",
        }
        self.assertIsNone(structured_delivery.channel_for_task(task))

    def test_a_captured_meeting_locator_routes_without_a_source_url(self):
        task = {
            "action_type": "follow-up",
            "source_type": "meeting",
            "source_url": None,
            "source_locator": json.dumps({
                "version": 1,
                "kind": "meeting",
                "event_id": "AAMkCaptured=",
                "source": "captured",
            }),
        }
        self.assertEqual(structured_delivery.channel_for_task(task), "teams")
        envelope = structured_delivery.initial_payload(task, "teams")
        prompt = structured_delivery.preview_prompt(task, envelope)
        self.assertIn("AAMkCaptured=", prompt)
        self.assertIn("/me/events/AAMkCaptured=", prompt)

    def test_an_arbitrary_event_id_query_is_not_a_teams_meeting(self):
        task = {
            "action_type": "follow-up",
            "source_type": "manual",
            "source_url": "https://example.com/page?eventId=not-a-meeting",
        }
        self.assertIsNone(structured_delivery.channel_for_task(task))

    def test_a_non_teams_link_does_not_route_to_teams(self):
        # An Outlook link is a mail thread, not a Teams destination. Routing it
        # to Teams would send the message somewhere it was never drafted for.
        task = {
            "action_type": "follow-up",
            "source_type": "manual",
            "source_url": "https://outlook.office365.com/owa/?ItemID=AAMk123",
        }
        self.assertIsNone(structured_delivery.channel_for_task(task))

    def test_a_link_does_not_override_an_explicit_action_type(self):
        # respond-email means email even when a Teams link is attached; the
        # action type is what the user asked for.
        task = {
            "action_type": "respond-email",
            "source_type": "manual",
            "source_url": (
                "https://teams.microsoft.com/l/chat/"
                "19:aaa_bbb@unq.gbl.spaces/conversations"
            ),
        }
        self.assertEqual(structured_delivery.channel_for_task(task), "email")

    def test_an_unrelated_action_type_is_not_dragged_into_teams(self):
        # Only the two types that already route on source do so on a link.
        task = {
            "action_type": "review-document",
            "source_type": "manual",
            "source_url": (
                "https://teams.microsoft.com/l/chat/"
                "19:aaa_bbb@unq.gbl.spaces/conversations"
            ),
        }
        self.assertIsNone(structured_delivery.channel_for_task(task))

    def test_a_malformed_url_is_not_a_destination(self):
        for url in (None, "", "   ", "not a url", "https://example.com/x"):
            with self.subTest(url=url):
                task = {
                    "action_type": "follow-up",
                    "source_type": "manual",
                    "source_url": url,
                }
                self.assertIsNone(structured_delivery.channel_for_task(task))

    def test_existing_teams_routing_still_works(self):
        self.assertEqual(
            structured_delivery.channel_for_task(
                {"action_type": "follow-up", "source_type": "chat"}
            ),
            "teams",
        )
        self.assertEqual(
            structured_delivery.channel_for_task(
                {"action_type": "awaiting-response", "source_type": "teams"}
            ),
            "teams",
        )

    def test_a_plain_general_task_still_falls_back_to_cowork(self):
        self.assertIsNone(
            structured_delivery.channel_for_task(
                {"action_type": "general", "source_type": "manual"}
            )
        )

    def test_database_accepts_the_teams_message_action_type(self):
        task = create_task(
            "Message Phil about the Lighthouse program",
            action_type="teams-message",
        )
        stored = get_task(task["id"])
        self.assertEqual(stored["action_type"], "teams-message")
        self.assertEqual(
            structured_delivery.channel_for_task(stored), "teams"
        )

    def test_legacy_database_is_widened_for_teams_message(self):
        """An existing db predates the value; init_db must widen it in place."""
        conn = db_module.sqlite3.connect(":memory:")
        conn.row_factory = db_module.sqlite3.Row
        legacy = db_module.SCHEMA_SQL.replace(
            "'awaiting-response','prepare','general','teams-message'",
            "'awaiting-response','prepare','general'",
        ).replace(
            "'awaiting-response','prepare','teams-message','general'",
            "'awaiting-response','prepare','general'",
        )
        conn.executescript(legacy)
        conn.execute("INSERT INTO tasks (title) VALUES ('legacy row')")
        conn.commit()

        db_module.init_db(conn)

        conn.execute(
            "INSERT INTO tasks (title, action_type) VALUES (?,?)",
            ("new teams task", "teams-message"),
        )
        conn.commit()
        kept = conn.execute(
            "SELECT title FROM tasks WHERE id=1"
        ).fetchone()["title"]
        self.assertEqual(kept, "legacy row")
        conn.close()


class TestStructuredDeliveryMigration(unittest.TestCase):
    def test_expression_default_survives_a_constraint_rebuild(self):
        """PRAGMA strips the parens SQLite demands around expression defaults.

        Re-emitting one bare aborts the rebuild with a syntax error, which would
        take init_db -- and therefore startup -- down with it.
        """
        conn = db_module.sqlite3.connect(":memory:")
        conn.row_factory = db_module.sqlite3.Row
        conn.executescript(db_module.SCHEMA_SQL)
        row = next(
            r for r in conn.execute("PRAGMA table_info(tasks)")
            if r["name"] == "created_at"
        )

        definition = db_module._task_column_definition(row)

        self.assertIn("strftime", definition, "default should be carried over")
        conn.execute(f"CREATE TABLE probe ({definition})")
        conn.execute("INSERT INTO probe DEFAULT VALUES")
        stamped = conn.execute("SELECT created_at FROM probe").fetchone()[0]
        self.assertRegex(stamped, r"^\d{4}-\d{2}-\d{2}T")
        conn.close()

    def test_partial_legacy_schema_rebuild_preserves_rows(self):
        conn = db_module.sqlite3.connect(":memory:")
        conn.row_factory = db_module.sqlite3.Row
        legacy_schema = (
            db_module.SCHEMA_SQL
            .replace(",'calendar'", "")
            .replace("    structured_payload TEXT,\n", "")
            .replace("    workiq_delivery_ref TEXT,\n", "")
        )
        conn.executescript(legacy_schema)
        conn.execute("INSERT INTO tasks (title) VALUES ('Legacy action')")
        conn.execute(
            "INSERT INTO task_actions (task_id, state, draft) "
            "VALUES (1, 'ready', 'Keep me')"
        )
        conn.commit()

        db_module.init_db(conn)

        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(task_actions)")
        }
        row = conn.execute(
            "SELECT state, draft, structured_payload, workiq_delivery_ref "
            "FROM task_actions WHERE id=1"
        ).fetchone()
        self.assertIn("structured_payload", columns)
        self.assertIn("workiq_delivery_ref", columns)
        self.assertEqual(dict(row), {
            "state": "ready",
            "draft": "Keep me",
            "structured_payload": None,
            "workiq_delivery_ref": None,
        })
        conn.execute(
            "INSERT INTO task_actions (task_id, delivery_channel) "
            "VALUES (1, 'calendar')"
        )
        conn.close()


class TestStructuredDeliveryRoutes(tornado.testing.AsyncHTTPTestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.original_db_path = db_module.DB_PATH
        db_module.DB_PATH = self.tmp.name
        conn = db_module.get_connection()
        db_module.init_db(conn)
        conn.close()
        self.preview_started = []
        self.execute_started = []
        # Same rule as StructuredDeliveryTestBase: no test may reach WorkIQ.
        # These cases drive slot selection, so the stand-in reports clear
        # calendars rather than a failed measurement -- "not measured" is a
        # separate case, covered by TestAvailabilityVerification.
        self._real_fetch_availability = structured_delivery.fetch_availability

        def _all_free(attendees, slots):
            from datetime import timezone as _timezone

            measurements = []
            for slot in slots:
                start = structured_delivery._parse_offset_datetime(
                    slot.get("start")
                )
                if not start:
                    return None
                measurements.append({
                    "schedules": [
                        {"scheduleId": str(email), "availabilityView": "0" * 288}
                        for email in attendees
                    ],
                    "view_start": start.astimezone(_timezone.utc).isoformat(),
                })
            return measurements

        structured_delivery.fetch_availability = _all_free
        super().setUp()

    def tearDown(self):
        structured_delivery.fetch_availability = self._real_fetch_availability
        super().tearDown()
        db_module.DB_PATH = self.original_db_path
        os.unlink(self.tmp.name)

    def get_app(self):
        return make_app()

    def _post(self, path, body=None, headers=None):
        return self.fetch(
            path,
            method="POST",
            body=json.dumps(body or {}),
            headers={"Content-Type": "application/json", **(headers or {})},
        )

    def _put(self, path, body=None):
        return self.fetch(
            path,
            method="PUT",
            body=json.dumps(body or {}),
            headers={"Content-Type": "application/json"},
        )

    def test_three_structured_modes_bypass_cowork_preview(self):
        from src.handlers import cowork as handler

        tasks = [
            create_task(
                "Schedule a 25-minute review",
                action_type="schedule-meeting",
                key_people=json.dumps(
                    [{"name": "Rima Reyes", "email": "rima@microsoft.com"}]
                ),
            ),
            create_task(
                "Reply to Sarah",
                action_type="respond-email",
                source_type="email",
                key_people=json.dumps(
                    [{"name": "Sarah Goodwin", "email": "sarah@microsoft.com"}]
                ),
            ),
            create_task(
                "Ping Sarah",
                action_type="follow-up",
                source_type="chat",
                key_people=json.dumps(
                    [{"name": "Sarah Goodwin", "email": "sarah@microsoft.com"}]
                ),
            ),
        ]
        original = handler.STRUCTURED_PREVIEW_FN
        handler.STRUCTURED_PREVIEW_FN = (
            lambda task, action: self.preview_started.append(
                (task["id"], action["id"], json.loads(action["structured_payload"]))
            )
        )
        handler.SPAWN = lambda *_args, **_kwargs: self.fail("Cowork was launched")
        try:
            responses = [
                self._post(f"/api/tasks/{task['id']}/cowork") for task in tasks
            ]
        finally:
            handler.STRUCTURED_PREVIEW_FN = original

        self.assertEqual([response.code for response in responses], [202, 202, 202])
        self.assertEqual(
            [item[2]["channel"] for item in self.preview_started],
            ["calendar", "email", "teams"],
        )
        for task, started in zip(tasks, self.preview_started):
            action = get_latest_task_action(task["id"])
            self.assertEqual(action["id"], started[1])
            self.assertIsNone(action["conversation_id"])

    def test_other_modes_keep_the_cowork_path(self):
        from src.handlers import cowork as handler

        task = create_task("Prepare briefing", action_type="prepare")
        original = handler.STRUCTURED_PREVIEW_FN
        handler.STRUCTURED_PREVIEW_FN = (
            lambda *_args: self.fail("Structured preview was launched")
        )
        with mock.patch.object(handler, "start_preview") as start_preview:
            try:
                response = self._post(f"/api/tasks/{task['id']}/cowork")
            finally:
                handler.STRUCTURED_PREVIEW_FN = original

        self.assertEqual(response.code, 202)
        start_preview.assert_called_once()

    def test_structured_execute_bypasses_cowork_guards(self):
        from src.handlers import cowork as handler

        task = create_task(
            "Reply to Sarah",
            action_type="respond-email",
            source_type="email",
            key_people=json.dumps(
                [{"name": "Sarah Goodwin", "email": "sarah@microsoft.com"}]
            ),
        )
        payload = {
            "schema_version": 1,
            "channel": "email",
            "mode": "reply",
            "message_id": "message-1",
            "to": ["sarah@microsoft.com"],
            "subject": "Re: Project update",
            "body": "Approved body",
        }
        parent = create_task_action(
            task["id"],
            delivery_channel="email",
            destination_ref="sarah@microsoft.com",
            destination_display="Sarah Goodwin",
            destination_confirmed_at="2026-08-20T12:00:00Z",
            structured_payload=json.dumps(payload, separators=(",", ":"), sort_keys=True),
        )
        parent = update_task_action(
            parent["id"],
            frozenset({"state", "draft"}),
            state="ready",
            draft="Subject: Re: Project update\n\nApproved body",
        )
        snapshot = {
            "parent_action_id": parent["id"],
            "draft": parent["draft"],
            "destination_ref": parent["destination_ref"],
            "destination_display": parent["destination_display"],
            "delivery_channel": parent["delivery_channel"],
            "destination_confirmed_at": parent["destination_confirmed_at"],
        }
        original_execute = handler.STRUCTURED_EXECUTE_FN
        original_transport = handler.EXECUTE_TRANSPORT_ENABLED_FN
        handler.STRUCTURED_EXECUTE_FN = (
            lambda action: self.execute_started.append(action)
        )
        handler.EXECUTE_TRANSPORT_ENABLED_FN = lambda: False
        try:
            response = self._post(
                f"/api/tasks/{task['id']}/cowork/execute",
                {"approved_snapshot": snapshot},
                {"X-Riveter-Action": "confirm"},
            )
        finally:
            handler.STRUCTURED_EXECUTE_FN = original_execute
            handler.EXECUTE_TRANSPORT_ENABLED_FN = original_transport

        self.assertEqual(response.code, 202, response.body)
        self.assertEqual(len(self.execute_started), 1)
        self.assertEqual(
            json.loads(self.execute_started[0]["structured_payload"]), payload
        )
        self.assertIsNone(self.execute_started[0]["conversation_id"])

    def test_calendar_selection_creates_exact_verified_slot_without_cowork(self):
        from src.handlers import cowork as handler

        task = create_task(
            "Schedule a 25-minute review",
            action_type="schedule-meeting",
            key_people=json.dumps(
                [{"name": "Rima Reyes", "email": "rima@microsoft.com"}]
            ),
        )
        envelope = structured_delivery.initial_payload(task, "calendar")
        action = create_task_action(
            task["id"],
            delivery_channel="calendar",
            structured_payload=json.dumps(envelope),
        )
        preview = {
            "schema_version": 1,
            "channel": "calendar",
            "subject": "Project review",
            "body": "Review decisions and next steps.",
            "duration_minutes": 25,
            "attendees": [
                {"name": "Rima Reyes", "email": "rima@microsoft.com"}
            ],
            "timezone": "America/Los_Angeles",
            "slots": [
                {
                    "id": "0",
                    "label": "Friday, August 21 at 9:05 AM",
                    "start": "2028-08-21T09:05:00-07:00",
                    "end": "2028-08-21T09:30:00-07:00",
                    "timezone": "America/Los_Angeles",
                    "availability": {"rima@microsoft.com": "free"},
                }
            ],
        }
        structured_delivery.finish_preview(
            action["id"],
            stdout=(
                "<<<RIVETER_RESULT>>>\n"
                + json.dumps(
                    {
                        "correlation_id": envelope["correlation_id"],
                        "phase": "preview",
                        "ok": True,
                        "payload": preview,
                    }
                )
                + "\n<<<END_RIVETER_RESULT>>>"
            ),
            stderr="",
            exit_code=0,
            correlation_id=envelope["correlation_id"],
        )
        waiting = get_latest_task_action(task["id"])
        interaction = json.loads(waiting["blocked_question"])
        original_execute = handler.STRUCTURED_EXECUTE_FN
        original_now = handler.NOW_FN
        handler.STRUCTURED_EXECUTE_FN = (
            lambda execution: self.execute_started.append(execution)
        )
        handler.NOW_FN = lambda: datetime(
            2028, 8, 20, 12, 0, tzinfo=timezone.utc
        )
        try:
            response = self._post(
                f"/api/tasks/{task['id']}/cowork/answer",
                {
                    "invocation_id": interaction["invocation_id"],
                    "answers": {"0": "0"},
                },
            )
        finally:
            handler.STRUCTURED_EXECUTE_FN = original_execute
            handler.NOW_FN = original_now

        self.assertEqual(response.code, 202, response.body)
        self.assertEqual(len(self.execute_started), 1)
        child = self.execute_started[0]
        self.assertEqual(child["state"], "executing")
        self.assertIsNone(child["conversation_id"])
        event = json.loads(child["structured_payload"])
        self.assertEqual(event, {
            "schema_version": 1,
            "channel": "calendar",
            "subject": "Project review",
            "body": "Review decisions and next steps.",
            "attendees": [
                {"name": "Rima Reyes", "email": "rima@microsoft.com"}
            ],
            "duration_minutes": 25,
            "start": "2028-08-21T09:05:00-07:00",
            "end": "2028-08-21T09:30:00-07:00",
            "time_zone": "America/Los_Angeles",
        })

    def test_structured_execution_never_surfaces_waiting_for_user(self):
        task = create_task(
            "Schedule a 25-minute review",
            action_type="schedule-meeting",
        )
        action = create_task_action(
            task["id"],
            delivery_channel="calendar",
            destination_ref="rima@microsoft.com",
            destination_display="Rima Reyes",
            structured_payload=json.dumps({
                "schema_version": 1,
                "channel": "calendar",
                "subject": "Project review",
            }),
        )
        update_task_action(
            action["id"],
            frozenset({"state"}),
            state="executing",
        )

        response = self.fetch(f"/api/tasks/{task['id']}/cowork")

        self.assertEqual(response.code, 200, response.body)
        returned = json.loads(response.body)["action"]
        self.assertFalse(returned["waiting_on_user"])
        self.assertIsNone(returned.get("interaction_request"))
        self.assertEqual(returned["progress"], [])

    def test_structured_destination_cannot_change_after_preview(self):
        task = create_task(
            "Reply to Sarah",
            action_type="respond-email",
            source_type="email",
        )
        payload = {
            "schema_version": 1,
            "channel": "email",
            "mode": "reply",
            "message_id": "message-1",
            "to": ["sarah@microsoft.com"],
            "subject": "Re: Project update",
            "body": "Approved body",
        }
        action = create_task_action(
            task["id"],
            delivery_channel="email",
            destination_ref="sarah@microsoft.com",
            destination_display="Sarah Goodwin",
            structured_payload=json.dumps(payload),
        )
        update_task_action(
            action["id"],
            frozenset({"state"}),
            state="ready",
        )

        response = self._post(
            f"/api/tasks/{task['id']}/cowork/destination",
            {
                "delivery_channel": "email",
                "destination_ref": "other@microsoft.com",
                "destination_display": "Other Person",
            },
        )

        self.assertEqual(response.code, 409, response.body)
        latest = get_latest_task_action(task["id"])
        self.assertEqual(latest["destination_ref"], "sarah@microsoft.com")
        self.assertEqual(latest["destination_display"], "Sarah Goodwin")

    def test_structured_email_destination_confirms_despite_address_case(self):
        """The preview resolves display-cased addresses; confirming must work.

        Production 2026-08-23 (task 2591): clicking Send was a silent no-op.
        The handler lowercases the incoming address and then compared that
        against the un-normalised stored ref, so any address with a capital
        letter -- which is what Graph returns -- tripped the immutability guard
        and returned 409. The same class of bug as the attendee 409 fixed in
        bf8622b: two representations of one value compared directly.
        """
        task = create_task(
            "Email Phil about the harness",
            action_type="respond-email",
            source_type="manual",
        )
        payload = {
            "schema_version": 1,
            "channel": "email",
            "mode": "new",
            "to": ["Phil.Topness@microsoft.com"],
            "subject": "Harness update",
            "body": "Approved body",
        }
        action = create_task_action(
            task["id"],
            delivery_channel="email",
            destination_ref="Phil.Topness@microsoft.com",
            destination_display="Phil.Topness@microsoft.com",
            structured_payload=json.dumps(payload),
        )
        update_task_action(action["id"], frozenset({"state"}), state="ready")

        response = self._post(
            f"/api/tasks/{task['id']}/cowork/destination",
            {
                "delivery_channel": "email",
                "destination_ref": "Phil.Topness@microsoft.com",
                "destination_display": "Phil.Topness@microsoft.com",
            },
        )

        self.assertEqual(response.code, 200, response.body)
        latest = get_latest_task_action(task["id"])
        self.assertTrue(latest["destination_confirmed_at"])
        # The address the user approved is preserved, not silently recased.
        self.assertEqual(
            latest["destination_ref"], "Phil.Topness@microsoft.com"
        )

    def test_structured_email_destination_still_rejects_a_different_address(self):
        """Relaxing the case comparison must not relax the guard itself."""
        task = create_task(
            "Email Phil about the harness",
            action_type="respond-email",
            source_type="manual",
        )
        payload = {
            "schema_version": 1,
            "channel": "email",
            "mode": "new",
            "to": ["Phil.Topness@microsoft.com"],
            "subject": "Harness update",
            "body": "Approved body",
        }
        action = create_task_action(
            task["id"],
            delivery_channel="email",
            destination_ref="Phil.Topness@microsoft.com",
            destination_display="Phil.Topness@microsoft.com",
            structured_payload=json.dumps(payload),
        )
        update_task_action(action["id"], frozenset({"state"}), state="ready")

        response = self._post(
            f"/api/tasks/{task['id']}/cowork/destination",
            {
                "delivery_channel": "email",
                "destination_ref": "Someone.Else@microsoft.com",
                "destination_display": "Someone Else",
            },
        )

        self.assertEqual(response.code, 409, response.body)
        latest = get_latest_task_action(task["id"])
        self.assertEqual(
            latest["destination_ref"], "Phil.Topness@microsoft.com"
        )

    def test_structured_teams_edit_rejects_whitespace_only_message(self):
        task = create_task(
            "Ping the project chat",
            action_type="follow-up",
            source_type="chat",
        )
        payload = {
            "schema_version": 1,
            "channel": "teams",
            "mode": "chat",
            "chat_id": "chat-1",
            "body": "Approved body",
        }
        action = create_task_action(
            task["id"],
            delivery_channel="teams",
            destination_ref="chat-1",
            destination_display="Project chat",
            structured_payload=json.dumps(payload),
        )
        update_task_action(
            action["id"],
            frozenset({"state", "draft"}),
            state="ready",
            draft="Approved body",
        )

        response = self.fetch(
            f"/api/tasks/{task['id']}/cowork",
            method="PUT",
            body=json.dumps({"draft_edited": " \n\t "}),
            headers={"Content-Type": "application/json"},
        )

        self.assertEqual(response.code, 400, response.body)
        latest = get_latest_task_action(task["id"])
        self.assertEqual(json.loads(latest["structured_payload"]), payload)

    def _structured_calendar_chooser(self, steer=None):
        """A structured calendar action parked on a slot chooser."""
        task = create_task(
            "Coordinate the Pega meeting",
            action_type="schedule-meeting",
            key_people=json.dumps(
                [{"name": "Rima Reyes", "email": "rima@microsoft.com"}]
            ),
        )
        envelope = structured_delivery.initial_payload(task, "calendar")
        envelope["subject"] = "Pega sync"
        envelope["attendees"] = [
            {"name": "Rima Reyes", "email": "rima@microsoft.com"}
        ]
        envelope["duration_minutes"] = 25
        if steer:
            envelope["steer"] = steer
        interaction = {
            "invocation_id": "structured-calendar-test",
            "questions": [{
                "id": "0",
                "header": "Select & create meeting",
                "question": "Choose one.",
                "multi_select": False,
                "options": [
                    {"value": "0", "label": "Wed 12:00 PM",
                     "description": "Availability not checked."},
                ],
            }],
            "schedule_evidence": {
                "valid": True,
                "source": "copilot-ask",
                "query_backed": True,
                "availability_verified": False,
                "duration_minutes": 25,
                "attendees": ["rima@microsoft.com"],
                "slots": [{
                    "value": "0",
                    "label": "Wed 12:00 PM",
                    "start": "2099-08-26T12:00:00-04:00",
                    "end": "2099-08-26T12:25:00-04:00",
                    "timezone": "Eastern Standard Time",
                    "availability": {"rima@microsoft.com": "unknown"},
                }],
            },
        }
        action = create_task_action(
            task["id"],
            action_type="schedule-meeting",
            delivery_channel="calendar",
            structured_payload=json.dumps(envelope),
            state="previewing",
            blocked_question=json.dumps(interaction),
            had_interaction=1,
        )
        return task, interaction, action

    def test_a_steer_starts_a_fresh_preview_instead_of_refusing(self):
        """The card invites steering; this path had nowhere to put it.

        The chooser renders a "Need a different option?" box, but a structured
        calendar answer was matched against the offered slots and anything
        else was refused with "Select exactly one verified meeting time." Phil
        typed "start at 5 minute after" and could not submit it. Free text is
        a steer, not a bad slot id.
        """
        from src.handlers import cowork as handler

        task, interaction, _action = self._structured_calendar_chooser()
        started = []
        original = handler.STRUCTURED_PREVIEW_FN
        handler.STRUCTURED_PREVIEW_FN = (
            lambda t, a: started.append((t, a))
        )
        try:
            response = self._post(
                f"/api/tasks/{task['id']}/cowork/answer",
                {
                    "invocation_id": interaction["invocation_id"],
                    "answers": {"0": "start at 5 minutes after the hour"},
                },
            )
        finally:
            handler.STRUCTURED_PREVIEW_FN = original

        self.assertEqual(response.code, 202, response.body)
        self.assertEqual(len(started), 1, "a steer must start a new preview")
        fresh = started[0][1]
        self.assertEqual(fresh["state"], "previewing")
        payload = json.loads(fresh["structured_payload"])
        self.assertEqual(
            payload.get("steer"), "start at 5 minutes after the hour"
        )
        # The card must say what is being re-checked, so the wait does not
        # look like the steer was dropped.
        self.assertIn(
            "5 minutes after the hour", fresh.get("redirect_text") or ""
        )

    def test_a_steer_replaces_rather_than_accumulates(self):
        """Two steers at once are contradictory, not cumulative.

        "later in the day" then "actually Thursday" cannot both be honoured;
        the live prompt carries the latest ask, and the earlier one stays
        readable in the action chain.
        """
        from src.handlers import cowork as handler

        task, interaction, _action = self._structured_calendar_chooser(
            steer="find something later in the day"
        )
        started = []
        original = handler.STRUCTURED_PREVIEW_FN
        handler.STRUCTURED_PREVIEW_FN = lambda t, a: started.append((t, a))
        try:
            self._post(
                f"/api/tasks/{task['id']}/cowork/answer",
                {
                    "invocation_id": interaction["invocation_id"],
                    "answers": {"0": "actually make it Thursday"},
                },
            )
        finally:
            handler.STRUCTURED_PREVIEW_FN = original

        payload = json.loads(started[0][1]["structured_payload"])
        self.assertEqual(payload.get("steer"), "actually make it Thursday")
        self.assertNotIn("later in the day", payload.get("steer") or "")

    def test_preview_prompt_carries_the_steer(self):
        """A steer nobody renders is a steer nobody honours."""
        task = create_task(
            "Schedule a review",
            action_type="schedule-meeting",
            key_people=json.dumps(
                [{"name": "Rima Reyes", "email": "rima@microsoft.com"}]
            ),
        )
        payload = structured_delivery.initial_payload(task, "calendar")
        self.assertNotIn(
            "5 minutes after",
            structured_delivery.preview_prompt(task, payload),
        )
        payload["steer"] = "start at 5 minutes after the hour"
        prompt = structured_delivery.preview_prompt(task, payload)
        self.assertIn("start at 5 minutes after the hour", prompt)

    def test_windows_timezone_slot_selection_still_creates_the_exact_meeting(self):
        """Production returns Windows zone names, not IANA ones.

        Every earlier calendar test used "America/Los_Angeles", but the live
        WorkIQ preview emits "Eastern Standard Time" with an en-dash label, so
        the selection path was only ever exercised against a shape production
        does not produce.
        """
        from src.handlers import cowork as handler

        task = create_task(
            "Schedule Project Whale kickoff call",
            action_type="schedule-meeting",
            key_people=json.dumps([
                {"name": "Sally Shi", "email": "sally.shi@microsoft.com"},
                {"name": "Azharullah Meer", "email": "ameer@microsoft.com"},
            ]),
        )
        envelope = structured_delivery.initial_payload(task, "calendar")
        action = create_task_action(
            task["id"],
            delivery_channel="calendar",
            structured_payload=json.dumps(envelope),
        )
        # Derive the duration exactly as the worker does. Hardcoding it here
        # let the preview and the selection certifier disagree, which is the
        # shape of the bug this test exists to catch.
        duration = structured_delivery._meeting_duration(task)
        start = datetime(2028, 8, 25, 14, 5, tzinfo=timezone(timedelta(hours=-4)))
        end = start + timedelta(minutes=duration)
        preview = {
            "schema_version": 1,
            "channel": "calendar",
            "subject": "Project Whale kickoff",
            "body": "Align on outreach and telemetry targeting.",
            "duration_minutes": duration,
            # Mixed case, exactly as the live directory returned it.
            "attendees": [
                {"name": "Sally Shi", "email": "Sally.Shi@microsoft.com"},
                {"name": "Azharullah Meer", "email": "ameer@microsoft.com"},
            ],
            "timezone": "Eastern Standard Time",
            "slots": [
                {
                    "id": "0",
                    "label": "Tuesday, August 25, 2026, 2:05\u20132:30 PM ET",
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "timezone": "Eastern Standard Time",
                    "availability": {
                        "sally.shi@microsoft.com": "free",
                        "ameer@microsoft.com": "free",
                    },
                }
            ],
        }
        structured_delivery.finish_preview(
            action["id"],
            stdout=(
                f"{structured_delivery.RESULT_START}\n"
                + json.dumps({
                    "correlation_id": envelope["correlation_id"],
                    "phase": "preview",
                    "ok": True,
                    "payload": preview,
                })
                + f"\n{structured_delivery.RESULT_END}"
            ),
            stderr="",
            exit_code=0,
            correlation_id=envelope["correlation_id"],
            expected_channel="calendar",
            expected_attendees={
                "sally.shi@microsoft.com", "ameer@microsoft.com"
            },
            expected_duration=duration,
        )

        waiting = get_latest_task_action(task["id"])
        self.assertEqual(waiting["state"], "previewing", waiting.get("error"))
        interaction = json.loads(waiting["blocked_question"])
        slots = (interaction.get("schedule_evidence") or {}).get("slots") or []
        self.assertTrue(slots, "no selectable slot was offered")
        # The handler matches the user's answer against this key; without it the
        # selection fails with "Select exactly one verified meeting time."
        self.assertIn("value", slots[0])

        edited = self._put(
            f"/api/tasks/{task['id']}/cowork",
            {
                "calendar_subject": "Edited Project Whale kickoff",
                "calendar_body": "Edited agenda\n- Decide the launch owner.",
            },
        )
        self.assertEqual(edited.code, 200, edited.body)

        original_execute = handler.STRUCTURED_EXECUTE_FN
        original_now = handler.NOW_FN
        handler.STRUCTURED_EXECUTE_FN = (
            lambda execution: self.execute_started.append(execution)
        )
        handler.NOW_FN = lambda: datetime(2028, 8, 24, 12, 0, tzinfo=timezone.utc)
        try:
            response = self._post(
                f"/api/tasks/{task['id']}/cowork/answer",
                {
                    "invocation_id": interaction["invocation_id"],
                    "answers": {"0": str(slots[0]["value"])},
                },
            )
        finally:
            handler.STRUCTURED_EXECUTE_FN = original_execute
            handler.NOW_FN = original_now

        self.assertEqual(response.code, 202, response.body)
        self.assertEqual(len(self.execute_started), 1)
        event = json.loads(self.execute_started[0]["structured_payload"])
        self.assertEqual(event["start"], start.isoformat())
        self.assertEqual(event["end"], end.isoformat())
        self.assertEqual(event["duration_minutes"], duration)
        self.assertEqual(event["time_zone"], "Eastern Standard Time")
        self.assertEqual(event["subject"], "Edited Project Whale kickoff")
        self.assertEqual(
            event["body"], "Edited agenda\n- Decide the launch owner."
        )
        # The card must report what was booked. It previously carried the
        # preview draft, so a finished meeting still listed all the times that
        # had merely been offered.
        child_draft = self.execute_started[0]["draft"]
        self.assertIn("Project Whale kickoff", child_draft)
        self.assertIn("2:05", child_draft)
        self.assertNotIn("slots", child_draft.lower())

    def test_selected_meeting_draft_drops_the_options_not_chosen(self):
        from src.handlers import cowork as handler

        task = create_task(
            "Schedule a 25-minute review",
            action_type="schedule-meeting",
            key_people=json.dumps(
                [{"name": "Rima Reyes", "email": "rima@microsoft.com"}]
            ),
        )
        envelope = structured_delivery.initial_payload(task, "calendar")
        action = create_task_action(
            task["id"],
            delivery_channel="calendar",
            structured_payload=json.dumps(envelope),
        )
        duration = structured_delivery._meeting_duration(task)
        offered = []
        for index, hour in enumerate((9, 13, 15)):
            begin = datetime(
                2028, 8, 21, hour, 5, tzinfo=timezone(timedelta(hours=-7))
            )
            offered.append({
                "id": str(index),
                "label": f"Monday, August 21 at {hour}:05",
                "start": begin.isoformat(),
                "end": (begin + timedelta(minutes=duration)).isoformat(),
                "timezone": "America/Los_Angeles",
                "availability": {"rima@microsoft.com": "free"},
            })
        structured_delivery.finish_preview(
            action["id"],
            stdout=(
                f"{structured_delivery.RESULT_START}\n"
                + json.dumps({
                    "correlation_id": envelope["correlation_id"],
                    "phase": "preview",
                    "ok": True,
                    "payload": {
                        "schema_version": 1,
                        "channel": "calendar",
                        "subject": "Quarterly review",
                        "body": "Agree the rollout plan.",
                        "duration_minutes": duration,
                        "attendees": [
                            {"name": "Rima Reyes", "email": "rima@microsoft.com"}
                        ],
                        "timezone": "America/Los_Angeles",
                        "slots": offered,
                    },
                })
                + f"\n{structured_delivery.RESULT_END}"
            ),
            stderr="",
            exit_code=0,
            correlation_id=envelope["correlation_id"],
            expected_channel="calendar",
            expected_attendees={"rima@microsoft.com"},
            expected_duration=duration,
        )
        waiting = get_latest_task_action(task["id"])
        interaction = json.loads(waiting["blocked_question"])

        original_execute = handler.STRUCTURED_EXECUTE_FN
        original_now = handler.NOW_FN
        handler.STRUCTURED_EXECUTE_FN = (
            lambda execution: self.execute_started.append(execution)
        )
        handler.NOW_FN = lambda: datetime(2028, 8, 20, 12, 0, tzinfo=timezone.utc)
        try:
            response = self._post(
                f"/api/tasks/{task['id']}/cowork/answer",
                {
                    "invocation_id": interaction["invocation_id"],
                    "answers": {"0": "1"},
                },
            )
        finally:
            handler.STRUCTURED_EXECUTE_FN = original_execute
            handler.NOW_FN = original_now

        self.assertEqual(response.code, 202, response.body)
        draft = self.execute_started[0]["draft"]
        self.assertIn("Monday, August 21 at 13:05", draft)
        self.assertNotIn("Monday, August 21 at 9:05", draft)
        self.assertNotIn("Monday, August 21 at 15:05", draft)

    def test_unconfirmed_calendar_execution_can_be_retried_on_the_same_row(self):
        from src.handlers import cowork as handler

        task = create_task(
            "Schedule a 25-minute review",
            action_type="schedule-meeting",
            key_people=json.dumps(
                [{"name": "Rima Reyes", "email": "rima@microsoft.com"}]
            ),
        )
        action = create_task_action(
            task["id"],
            delivery_channel="calendar",
            destination_ref="rima@microsoft.com",
            destination_display="Rima Reyes",
            structured_payload=json.dumps({
                "schema_version": 1, "channel": "calendar",
                "subject": "Project review",
                "start": "2028-08-21T09:05:00-07:00",
                "end": "2028-08-21T09:30:00-07:00",
            }),
        )
        update_task_action(
            action["id"],
            frozenset({"state", "error"}),
            state="execute_unconfirmed",
            error="Structured worker produced no readable output",
        )
        expected_txn = structured_delivery.idempotency_key(
            {**action, "delivery_channel": "calendar"}
        )

        original = handler.STRUCTURED_EXECUTE_FN
        handler.STRUCTURED_EXECUTE_FN = (
            lambda execution, recover=False: self.execute_started.append(execution)
        )
        try:
            response = self._post(f"/api/tasks/{task['id']}/cowork/retry")
        finally:
            handler.STRUCTURED_EXECUTE_FN = original

        self.assertEqual(response.code, 202, response.body)
        self.assertEqual(len(self.execute_started), 1)
        # Same row means the same Graph transactionId, which is what makes the
        # retry safe rather than a second booking.
        self.assertEqual(self.execute_started[0]["id"], action["id"])
        self.assertEqual(
            structured_delivery.idempotency_key(
                {**self.execute_started[0], "delivery_channel": "calendar"}
            ),
            expected_txn,
        )
        latest = get_latest_task_action(task["id"])
        self.assertEqual(latest["state"], "executing")
        self.assertIsNone(latest["error"])

    def _backdate(self, action_id: int, minutes: int) -> None:
        """Age a row's updated_at directly.

        update_task_action owns updated_at, so a test cannot make a row look
        old through the normal path.
        """
        stamp = (
            datetime.now(timezone.utc) - timedelta(minutes=minutes)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn = db_module.get_connection()
        conn.execute(
            "UPDATE task_actions SET updated_at=? WHERE id=?", (stamp, action_id)
        )
        conn.commit()
        conn.close()

    def test_unconfirmed_teams_execution_is_not_retryable(self):
        """Superseded: Teams retry is now allowed, but only while it can look."""
        from src.handlers import cowork as handler

        task = create_task("Ping the chat", action_type="follow-up",
                           source_type="chat")
        action = create_task_action(
            task["id"],
            delivery_channel="teams",
            destination_ref="chat-1",
            destination_display="Project chat",
            structured_payload=json.dumps(
                {"schema_version": 1, "channel": "teams", "body": "Approved"}
            ),
        )
        update_task_action(
            action["id"],
            frozenset({"state", "error"}),
            state="execute_unconfirmed",
            error="no readable output",
        )
        self._backdate(
            action["id"],
            structured_delivery.TEAMS_RECOVERY_WINDOW_MINUTES + 30,
        )

        original = handler.STRUCTURED_EXECUTE_FN
        handler.STRUCTURED_EXECUTE_FN = (
            lambda execution, recover=False: self.execute_started.append(execution)
        )
        try:
            response = self._post(f"/api/tasks/{task['id']}/cowork/retry")
        finally:
            handler.STRUCTURED_EXECUTE_FN = original

        # Too old to see the message, so retrying could post a second time.
        self.assertEqual(response.code, 409, response.body)
        self.assertEqual(self.execute_started, [])
        latest = get_latest_task_action(task["id"])
        self.assertEqual(latest["state"], "execute_unconfirmed")

    def test_recent_unconfirmed_teams_execution_retries_in_recovery_mode(self):
        from src.handlers import cowork as handler

        task = create_task("Ping the chat", action_type="follow-up",
                           source_type="chat")
        action = create_task_action(
            task["id"],
            delivery_channel="teams",
            destination_ref="19:chat-1@thread.v2",
            destination_display="Project chat",
            structured_payload=json.dumps(
                {"schema_version": 1, "channel": "teams", "body": "Approved"}
            ),
        )
        recent = (
            datetime.now(timezone.utc) - timedelta(minutes=3)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        update_task_action(
            action["id"],
            frozenset({"state", "error"}),
            state="execute_unconfirmed",
            error="no readable output",
        )

        seen = []
        original = handler.STRUCTURED_EXECUTE_FN
        handler.STRUCTURED_EXECUTE_FN = (
            lambda execution, recover=False: seen.append((execution, recover))
        )
        try:
            response = self._post(f"/api/tasks/{task['id']}/cowork/retry")
        finally:
            handler.STRUCTURED_EXECUTE_FN = original

        self.assertEqual(response.code, 202, response.body)
        self.assertEqual(len(seen), 1)
        # Teams must never be re-run blindly; it has to look first.
        self.assertTrue(seen[0][1], "teams retry must run in recovery mode")
        self.assertEqual(seen[0][0]["id"], action["id"])
        self.assertEqual(
            get_latest_task_action(task["id"])["state"], "executing"
        )

    def test_retry_is_refused_unless_the_action_is_unconfirmed(self):
        from src.handlers import cowork as handler

        task = create_task("Schedule a 25-minute review",
                           action_type="schedule-meeting")
        action = create_task_action(
            task["id"],
            delivery_channel="calendar",
            structured_payload=json.dumps(
                {"schema_version": 1, "channel": "calendar"}
            ),
        )
        update_task_action(
            action["id"],
            frozenset({"state", "workiq_delivery_ref"}),
            state="executed",
            workiq_delivery_ref="AAMkAD-event",
        )

        original = handler.STRUCTURED_EXECUTE_FN
        handler.STRUCTURED_EXECUTE_FN = (
            lambda execution: self.execute_started.append(execution)
        )
        try:
            response = self._post(f"/api/tasks/{task['id']}/cowork/retry")
        finally:
            handler.STRUCTURED_EXECUTE_FN = original

        self.assertEqual(response.code, 409, response.body)
        self.assertEqual(self.execute_started, [])
        self.assertEqual(
            get_latest_task_action(task["id"])["state"], "executed"
        )

    def test_structured_email_edit_updates_the_sealed_payload(self):
        task = create_task(
            "Reply to Sarah",
            action_type="respond-email",
            source_type="email",
        )
        payload = {
            "schema_version": 1,
            "channel": "email",
            "mode": "reply",
            "message_id": "message-1",
            "to": ["sarah@microsoft.com"],
            "subject": "Old subject",
            "body": "Old body",
        }
        action = create_task_action(
            task["id"],
            delivery_channel="email",
            destination_ref="sarah@microsoft.com",
            destination_display="sarah@microsoft.com",
            structured_payload=json.dumps(payload),
        )
        update_task_action(
            action["id"],
            frozenset({"state", "draft"}),
            state="ready",
            draft="Subject: Old subject\n\nOld body",
        )

        response = self.fetch(
            f"/api/tasks/{task['id']}/cowork",
            method="PUT",
            body=json.dumps(
                {"draft_edited": "Subject: New subject\n\nApproved new body"}
            ),
            headers={"Content-Type": "application/json"},
        )

        self.assertEqual(response.code, 200, response.body)
        updated = get_latest_task_action(task["id"])
        sealed = json.loads(updated["structured_payload"])
        self.assertEqual(sealed["subject"], "New subject")
        self.assertEqual(sealed["body"], "Approved new body")
