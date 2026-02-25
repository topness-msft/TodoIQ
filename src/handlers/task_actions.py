"""Action handlers: promote, dismiss, status transitions."""

import json
import tornado.web

from ..models import (
    promote_task, dismiss_task, complete_task, start_task,
    snooze_task, transition_task, get_task, update_task,
)
from ..services.claude_runner import run_claude
from .ws import broadcast


class TaskActionHandler(tornado.web.RequestHandler):
    """POST /api/tasks/<id>/action — perform a lifecycle action."""

    def set_default_headers(self):
        self.set_header("Content-Type", "application/json")

    def post(self, task_id):
        try:
            body = json.loads(self.request.body)
        except (json.JSONDecodeError, TypeError):
            self.set_status(400)
            self.write(json.dumps({"error": "Invalid JSON"}))
            return

        action = body.get("action", "")
        tid = int(task_id)

        # Capture pre-transition status for coaching trigger
        pre_task = get_task(tid)
        pre_status = pre_task["status"] if pre_task else None

        action_map = {
            "promote": promote_task,
            "dismiss": dismiss_task,
            "complete": complete_task,
            "start": start_task,
        }

        if action in action_map:
            try:
                task = action_map[action](tid)
            except ValueError as e:
                self.set_status(400)
                self.write(json.dumps({"error": str(e)}))
                return
        elif action == "snooze":
            duration = body.get("duration_minutes")
            until = body.get("snoozed_until")
            try:
                task = snooze_task(tid, minutes=duration, until=until)
            except ValueError as e:
                self.set_status(400)
                self.write(json.dumps({"error": str(e)}))
                return
        elif action == "transition":
            new_status = body.get("status")
            if not new_status:
                self.set_status(400)
                self.write(json.dumps({"error": "status required for transition"}))
                return
            try:
                task = transition_task(tid, new_status)
            except ValueError as e:
                self.set_status(400)
                self.write(json.dumps({"error": str(e)}))
                return
        else:
            self.set_status(400)
            self.write(json.dumps({
                "error": f"Unknown action '{action}'",
                "valid": list(action_map.keys()) + ["transition"],
            }))
            return

        if task is None:
            self.set_status(404)
            self.write(json.dumps({"error": "Task not found"}))
            return

        # Auto-trigger coaching parse when accepting a suggested task
        # (promote to active, or any transition out of suggested)
        if pre_status == "suggested" and task["status"] != "dismissed" and not task.get("coaching_text"):
            task = update_task(tid, parse_status="queued")
            run_claude("/todo-parse", label="parse")

        self.write(json.dumps({"task": task}))
        broadcast({"type": "task_updated", "task": task})


class TaskRefreshHandler(tornado.web.RequestHandler):
    """POST /api/tasks/<id>/refresh — queue task for re-parsing by Claude."""

    def set_default_headers(self):
        self.set_header("Content-Type", "application/json")

    def post(self, task_id):
        tid = int(task_id)
        task = get_task(tid)
        if not task:
            self.set_status(404)
            self.write(json.dumps({"error": "Task not found"}))
            return

        # Set to 'queued' so UI shows progression; the Stop hook / todo-parse
        # will move it to 'parsing' then 'parsed'
        updated = update_task(tid, parse_status="queued")
        self.write(json.dumps({"task": updated}))
        broadcast({"type": "task_updated", "task": updated})
        # Auto-trigger parsing
        run_claude("/todo-parse", label="parse")


_VALID_SKILLS = {"respond-email", "schedule-meeting", "follow-up", "prepare", "teams-message"}


class TaskSkillHandler(tornado.web.RequestHandler):
    """POST /api/tasks/<id>/skill — run a Claude skill on a task."""

    def set_default_headers(self):
        self.set_header("Content-Type", "application/json")

    def post(self, task_id):
        try:
            body = json.loads(self.request.body)
        except (json.JSONDecodeError, TypeError):
            self.set_status(400)
            self.write(json.dumps({"error": "Invalid JSON"}))
            return

        skill = body.get("skill", "")
        tid = int(task_id)

        if skill not in _VALID_SKILLS:
            self.set_status(400)
            self.write(json.dumps({
                "error": f"Unknown skill '{skill}'",
                "valid": sorted(_VALID_SKILLS),
            }))
            return

        task = get_task(tid)
        if not task:
            self.set_status(404)
            self.write(json.dumps({"error": "Task not found"}))
            return

        label = f"skill:{skill}:{tid}"
        result = run_claude(f"/{skill} {tid}", label=label)
        broadcast({"type": "skill_running", "task_id": tid, "skill": skill})
        self.write(json.dumps(result))
