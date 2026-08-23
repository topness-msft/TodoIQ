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


class StructuredDeliveryTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.original_db_path = db_module.DB_PATH
        db_module.DB_PATH = self.tmp.name
        conn = db_module.get_connection()
        db_module.init_db(conn)
        conn.close()

    def tearDown(self):
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
        super().setUp()

    def tearDown(self):
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
