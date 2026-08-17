"""Tests for REST API handlers using Tornado's AsyncHTTPTestCase."""

import unittest
import json
import sys
import os
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import tornado.testing
from src.app import make_app


class TestTaskAPI(tornado.testing.AsyncHTTPTestCase):
    """Test the task REST API endpoints."""

    def setUp(self):
        import src.db as db_module
        self.db_module = db_module
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        db_module.DB_PATH = self.tmp.name
        conn = db_module.get_connection()
        db_module.init_db(conn)
        conn.close()
        super().setUp()

    def tearDown(self):
        super().tearDown()
        os.unlink(self.tmp.name)

    def get_app(self):
        return make_app()

    # ── Helper ──

    def _create_task(self, title="Test task", **extra):
        body = {"title": title, **extra}
        resp = self.fetch(
            "/api/tasks",
            method="POST",
            body=json.dumps(body),
            headers={"Content-Type": "application/json"},
        )
        return resp

    # ── Tests ──

    def test_list_tasks_empty(self):
        resp = self.fetch("/api/tasks")
        self.assertEqual(resp.code, 200)
        data = json.loads(resp.body)
        self.assertEqual(data["tasks"], [])

    def test_create_task(self):
        resp = self._create_task(title="Buy milk", description="2% organic")
        self.assertEqual(resp.code, 201)
        data = json.loads(resp.body)
        task = data["task"]
        self.assertEqual(task["title"], "Buy milk")
        self.assertEqual(task["description"], "2% organic")
        self.assertEqual(task["status"], "active")

    def test_create_task_raw_input(self):
        teams_url = (
            "https://teams.microsoft.com/l/chat/"
            "19:08b7be88-37ac-4e2b-82af-f8bb67e5f2f7_"
            "d87be1b4-816c-4eff-9edd-7a5823986db1@unq.gbl.spaces/conversations"
            "?context=%7B%22contextType%22:%22chat%22%7D"
        )
        body = {
            "raw_input": (
                "Agenda: https://example.com/meeting-notes "
                f"Schedule this meeting {teams_url}."
            )
        }
        resp = self.fetch(
            "/api/tasks",
            method="POST",
            body=json.dumps(body),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.code, 201)
        data = json.loads(resp.body)
        task = data["task"]
        self.assertEqual(task["parse_status"], "unparsed")
        self.assertIsNotNone(task["raw_input"])
        self.assertEqual(task["source_url"], teams_url)

    def test_create_task_raw_input_does_not_promote_non_teams_url(self):
        body = {"raw_input": "Review https://example.com/meeting-notes tomorrow"}
        resp = self.fetch(
            "/api/tasks",
            method="POST",
            body=json.dumps(body),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.code, 201)
        task = json.loads(resp.body)["task"]
        self.assertIsNone(task["source_url"])

    def test_create_task_no_title_or_raw_input(self):
        body = {"description": "No title"}
        resp = self.fetch(
            "/api/tasks",
            method="POST",
            body=json.dumps(body),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.code, 400)

    def test_create_task_invalid_json(self):
        resp = self.fetch(
            "/api/tasks",
            method="POST",
            body="not json",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.code, 400)

    def test_get_task(self):
        create_resp = self._create_task(title="Get me")
        task_id = json.loads(create_resp.body)["task"]["id"]

        resp = self.fetch(f"/api/tasks/{task_id}")
        self.assertEqual(resp.code, 200)
        data = json.loads(resp.body)
        self.assertEqual(data["task"]["title"], "Get me")
        self.assertIn("contexts", data)

    def test_get_task_not_found(self):
        resp = self.fetch("/api/tasks/99999")
        self.assertEqual(resp.code, 404)

    def test_update_task(self):
        create_resp = self._create_task(title="Original")
        task_id = json.loads(create_resp.body)["task"]["id"]

        resp = self.fetch(
            f"/api/tasks/{task_id}",
            method="PUT",
            body=json.dumps({"title": "Updated", "priority": 1}),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.code, 200)
        data = json.loads(resp.body)
        self.assertEqual(data["task"]["title"], "Updated")
        self.assertEqual(data["task"]["priority"], 1)

    def test_action_type_change_increments_cowork_revision_and_retires_preview(self):
        create_resp = self._create_task(
            title="Prepare meeting",
            action_type="prepare",
            key_people='[{"name":"Freada","email":"freada@example.com"}]',
            user_notes="Keep this note",
        )
        task = json.loads(create_resp.body)["task"]
        import src.models as models
        old_action = models.create_task_action(
            task["id"], action_type="prepare", intent="Prepare"
        )

        resp = self.fetch(
            f"/api/tasks/{task['id']}",
            method="PUT",
            body=json.dumps({"action_type": "schedule-meeting"}),
            headers={"Content-Type": "application/json"},
        )

        self.assertEqual(resp.code, 200)
        updated = json.loads(resp.body)["task"]
        self.assertEqual(updated["cowork_revision"], 1)
        self.assertEqual(updated["user_notes"], "Keep this note")
        self.assertEqual(updated["key_people"], task["key_people"])
        self.assertIsNone(models.get_latest_task_action(task["id"]))
        history = models.list_task_actions(task["id"])
        self.assertEqual(history[-1]["id"], old_action["id"])

    def test_same_action_type_does_not_increment_cowork_revision(self):
        task = json.loads(self._create_task(
            title="Meeting", action_type="prepare"
        ).body)["task"]
        resp = self.fetch(
            f"/api/tasks/{task['id']}",
            method="PUT",
            body=json.dumps({"action_type": "prepare"}),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(json.loads(resp.body)["task"]["cowork_revision"], 0)

    def test_update_task_not_found(self):
        resp = self.fetch(
            "/api/tasks/99999",
            method="PUT",
            body=json.dumps({"title": "Updated"}),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.code, 404)

    def test_update_task_no_valid_fields(self):
        create_resp = self._create_task(title="No change")
        task_id = json.loads(create_resp.body)["task"]["id"]

        resp = self.fetch(
            f"/api/tasks/{task_id}",
            method="PUT",
            body=json.dumps({"status": "completed"}),  # status not in allowed fields
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.code, 200)
        data = json.loads(resp.body)
        self.assertEqual(data["task"]["title"], "No change")

    def test_delete_task(self):
        create_resp = self._create_task(title="Delete me")
        task_id = json.loads(create_resp.body)["task"]["id"]

        resp = self.fetch(f"/api/tasks/{task_id}", method="DELETE")
        self.assertEqual(resp.code, 200)
        data = json.loads(resp.body)
        self.assertTrue(data["ok"])

        # Verify gone
        resp2 = self.fetch(f"/api/tasks/{task_id}")
        self.assertEqual(resp2.code, 404)

    def test_delete_task_not_found(self):
        resp = self.fetch("/api/tasks/99999", method="DELETE")
        self.assertEqual(resp.code, 404)

    def test_promote_action(self):
        create_resp = self._create_task(title="Suggest me")
        task_id = json.loads(create_resp.body)["task"]["id"]

        # Move to suggested first via direct DB update
        import src.models as models
        models.update_task(task_id, status="suggested")

        resp = self.fetch(
            f"/api/tasks/{task_id}/action",
            method="POST",
            body=json.dumps({"action": "promote"}),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.code, 200)
        data = json.loads(resp.body)
        self.assertEqual(data["task"]["status"], "active")

    def test_dismiss_action(self):
        create_resp = self._create_task(title="Dismiss me")
        task_id = json.loads(create_resp.body)["task"]["id"]

        resp = self.fetch(
            f"/api/tasks/{task_id}/action",
            method="POST",
            body=json.dumps({"action": "dismiss"}),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.code, 200)
        data = json.loads(resp.body)
        self.assertEqual(data["task"]["status"], "dismissed")

    def test_complete_action(self):
        create_resp = self._create_task(title="Complete me")
        task_id = json.loads(create_resp.body)["task"]["id"]

        resp = self.fetch(
            f"/api/tasks/{task_id}/action",
            method="POST",
            body=json.dumps({"action": "complete"}),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.code, 200)
        data = json.loads(resp.body)
        self.assertEqual(data["task"]["status"], "completed")

    def test_start_action(self):
        create_resp = self._create_task(title="Start me")
        task_id = json.loads(create_resp.body)["task"]["id"]

        resp = self.fetch(
            f"/api/tasks/{task_id}/action",
            method="POST",
            body=json.dumps({"action": "start"}),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.code, 200)
        data = json.loads(resp.body)
        self.assertEqual(data["task"]["status"], "in_progress")

    def test_invalid_action(self):
        create_resp = self._create_task(title="Bad action")
        task_id = json.loads(create_resp.body)["task"]["id"]

        resp = self.fetch(
            f"/api/tasks/{task_id}/action",
            method="POST",
            body=json.dumps({"action": "fly"}),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.code, 400)

    def test_action_invalid_transition(self):
        create_resp = self._create_task(title="Bad transition")
        task_id = json.loads(create_resp.body)["task"]["id"]

        # Try to promote an active task (active -> active is not promote)
        resp = self.fetch(
            f"/api/tasks/{task_id}/action",
            method="POST",
            body=json.dumps({"action": "promote"}),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.code, 400)

    def test_action_not_found(self):
        resp = self.fetch(
            "/api/tasks/99999/action",
            method="POST",
            body=json.dumps({"action": "promote"}),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.code, 404)

    def test_stats(self):
        self._create_task(title="A")
        self._create_task(title="B")
        resp = self.fetch("/api/stats")
        self.assertEqual(resp.code, 200)
        data = json.loads(resp.body)
        self.assertIn("stats", data)
        self.assertEqual(data["stats"]["total"], 2)
        self.assertIn("last_sync", data)

    def test_stats_empty(self):
        resp = self.fetch("/api/stats")
        self.assertEqual(resp.code, 200)
        data = json.loads(resp.body)
        self.assertEqual(data["stats"]["total"], 0)

    def test_list_tasks_filter_by_status(self):
        self._create_task(title="Active 1")
        self._create_task(title="Active 2")
        # Create a suggested task via raw API
        import src.models as models
        models.create_task(title="Suggested", status="suggested")

        resp = self.fetch("/api/tasks?status=active")
        self.assertEqual(resp.code, 200)
        data = json.loads(resp.body)
        self.assertEqual(len(data["tasks"]), 2)

        resp2 = self.fetch("/api/tasks?status=suggested")
        data2 = json.loads(resp2.body)
        self.assertEqual(len(data2["tasks"]), 1)


if __name__ == "__main__":
    unittest.main()
