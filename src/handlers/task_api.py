"""REST API handlers for task CRUD: /api/tasks, /api/tasks/<id>."""

import json
import tornado.web

from ..models import (
    create_task, get_task, list_tasks, update_task, delete_task,
    get_contexts, get_stats, get_last_sync,
)
from ..services.claude_runner import run_claude
from .ws import broadcast


class TaskListHandler(tornado.web.RequestHandler):
    """GET /api/tasks — list tasks; POST /api/tasks — create task."""

    def set_default_headers(self):
        self.set_header("Content-Type", "application/json")

    def get(self):
        status = self.get_argument("status", None)
        parse_status = self.get_argument("parse_status", None)
        exclude_status = self.get_argument("exclude_status", None)
        exclude_statuses = [s.strip() for s in exclude_status.split(",") if s.strip()] if exclude_status else None
        tasks = list_tasks(status=status, parse_status=parse_status, exclude_statuses=exclude_statuses)
        self.write(json.dumps({"tasks": tasks}))

    def post(self):
        try:
            body = json.loads(self.request.body)
        except (json.JSONDecodeError, TypeError):
            self.set_status(400)
            self.write(json.dumps({"error": "Invalid JSON"}))
            return

        title = body.get("title", "").strip()
        raw_input = body.get("raw_input", "").strip()

        # If raw_input is provided but no title, save as unparsed for Claude to parse
        if raw_input and not title:
            task = create_task(
                title=raw_input,
                raw_input=raw_input,
                status="active",
                parse_status="unparsed",
            )
            # Auto-trigger parsing
            run_claude("/todo-parse", label="parse")
        elif title:
            task = create_task(
                title=title,
                description=body.get("description", ""),
                status=body.get("status", "active"),
                parse_status=body.get("parse_status", "unparsed"),
                priority=int(body.get("priority", 3)),
                due_date=body.get("due_date"),
                source_type=body.get("source_type", "manual"),
                source_id=body.get("source_id"),
                source_snippet=body.get("source_snippet"),
                source_url=body.get("source_url"),
                action_type=body.get("action_type", "general"),
                key_people=body.get("key_people"),
                user_notes=body.get("user_notes", ""),
            )
        else:
            self.set_status(400)
            self.write(json.dumps({"error": "title or raw_input required"}))
            return

        self.set_status(201)
        self.write(json.dumps({"task": task}))
        broadcast({"type": "task_created", "task": task})


class TaskDetailHandler(tornado.web.RequestHandler):
    """GET/PUT/DELETE /api/tasks/<id>."""

    def set_default_headers(self):
        self.set_header("Content-Type", "application/json")

    def get(self, task_id):
        task = get_task(int(task_id))
        if not task:
            self.set_status(404)
            self.write(json.dumps({"error": "Not found"}))
            return
        contexts = get_contexts(int(task_id))
        self.write(json.dumps({"task": task, "contexts": contexts}))

    def put(self, task_id):
        task = get_task(int(task_id))
        if not task:
            self.set_status(404)
            self.write(json.dumps({"error": "Not found"}))
            return
        try:
            body = json.loads(self.request.body)
        except (json.JSONDecodeError, TypeError):
            self.set_status(400)
            self.write(json.dumps({"error": "Invalid JSON"}))
            return

        # Filter to allowed fields
        allowed = {
            "title", "description", "priority", "due_date", "committed_date",
            "user_notes", "coaching_text", "skill_output", "key_people",
            "related_meeting", "source_type", "source_url", "source_snippet",
            "action_type", "is_quick_hit",
        }
        updates = {k: v for k, v in body.items() if k in allowed}

        # Validate action_type if provided
        valid_action_types = {
            "schedule-meeting", "respond-email", "review-document",
            "follow-up", "awaiting-response", "prepare", "general",
        }
        if "action_type" in updates and updates["action_type"] not in valid_action_types:
            self.set_status(400)
            self.write(json.dumps({"error": f"Invalid action_type. Must be one of: {', '.join(sorted(valid_action_types))}"}))
            return
        if not updates:
            self.write(json.dumps({"task": task}))
            return

        updated = update_task(int(task_id), **updates)
        self.write(json.dumps({"task": updated}))
        broadcast({"type": "task_updated", "task": updated})

    def delete(self, task_id):
        if delete_task(int(task_id)):
            self.write(json.dumps({"ok": True}))
            broadcast({"type": "task_deleted", "task_id": int(task_id)})
        else:
            self.set_status(404)
            self.write(json.dumps({"error": "Not found"}))


class StatsHandler(tornado.web.RequestHandler):
    """GET /api/stats — task count statistics + last sync info."""

    def set_default_headers(self):
        self.set_header("Content-Type", "application/json")

    def get(self):
        stats = get_stats()
        last_sync = get_last_sync()
        self.write(json.dumps({"stats": stats, "last_sync": last_sync}))
