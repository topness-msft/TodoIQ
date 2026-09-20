"""Action handlers: promote, dismiss, status transitions."""

import json
import tornado.web

from ..models import (
    DELIVERY_CONFLICT_MESSAGE,
    promote_task, dismiss_task, complete_task, start_task,
    snooze_task, transition_task, get_task, request_parse,
)
from ..services.skills import VALID_SKILLS, get_skill_service
from ..services.parsing import get_parse_service
from ..services.runtime_mode import DEMO_DISABLED_MESSAGE, demo_mode, todo_parse_enabled
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
                self.set_status(
                    409 if str(e) == DELIVERY_CONFLICT_MESSAGE else 400
                )
                self.write(json.dumps({"error": str(e)}))
                return
        elif action == "snooze":
            duration = body.get("duration_minutes")
            until = body.get("snoozed_until")
            try:
                task = snooze_task(tid, minutes=duration, until=until)
            except ValueError as e:
                self.set_status(
                    409 if str(e) == DELIVERY_CONFLICT_MESSAGE else 400
                )
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
                self.set_status(
                    409 if str(e) == DELIVERY_CONFLICT_MESSAGE else 400
                )
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
            request_parse(tid, "coaching_only")
            task = get_task(tid)
            get_parse_service().launch((tid,))
        self.write(json.dumps({"task": task}))
        broadcast({"type": "task_updated", "task": task})


class TaskRefreshHandler(tornado.web.RequestHandler):
    """POST /api/tasks/<id>/refresh — queue coaching without rebuilding the task."""

    def set_default_headers(self):
        self.set_header("Content-Type", "application/json")

    def post(self, task_id):
        if not todo_parse_enabled():
            self.set_status(403)
            self.write(json.dumps({"error": DEMO_DISABLED_MESSAGE}))
            return
        tid = int(task_id)
        task = get_task(tid)
        if not task:
            self.set_status(404)
            self.write(json.dumps({"error": "Task not found"}))
            return

        request_parse(tid, "coaching_only")
        updated = get_task(tid)
        self.write(json.dumps({"task": updated}))
        broadcast({"type": "task_updated", "task": updated})
        get_parse_service().launch((tid,))

_VALID_SKILLS = VALID_SKILLS


class TaskSkillHandler(tornado.web.RequestHandler):
    """POST /api/tasks/<id>/skill — start a direct read-only skill request."""

    def set_default_headers(self):
        self.set_header("Content-Type", "application/json")

    def post(self, task_id):
        if demo_mode():
            self.set_status(403)
            self.write(json.dumps({"error": DEMO_DISABLED_MESSAGE}))
            return
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

        result = get_skill_service().launch(tid, skill)
        if result["ok"]:
            broadcast({"type": "skill_running", "task_id": tid, "skill": skill,
                       "run_id": result["run_id"], "started_at": result["started_at"]})
        self.write(json.dumps(result))
