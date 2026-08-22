import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
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
        self.assertIn("--available-tools=workiq-do_action", email)
        self.assertIn("--available-tools=workiq-create_entity", teams)
        for argv in (calendar, email, teams):
            self.assertNotIn("workiq-fetch", " ".join(argv))
            self.assertNotIn("shell", " ".join(argv))

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


class TestStructuredDeliveryMigration(unittest.TestCase):
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
