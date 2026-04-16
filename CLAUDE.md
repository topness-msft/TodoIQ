# TodoNess - Project Notes

## Overview
TodoNess is a local AI-powered task manager that integrates with Microsoft 365 via WorkIQ MCP. It reads M365 context (flagged emails, meetings, calendar) to surface actionable suggestions, lets users manage tasks through a web dashboard, and provides AI coaching on each task.

## Technology Stack
- **Runtime:** Python 3.11
- **Web Framework:** Tornado 6.4+
- **Templating:** Jinja2 3.1+
- **Database:** SQLite3 (WAL mode)
- **Dependencies:** `tornado`, `jinja2` (see `requirements.txt`)

## Architecture
```
Claude Code Commands (/todo-refresh, /todo-add, /todo-parse, skills)
  ↕ calls WorkIQ MCP    ↕ writes SQLite
SQLite DB (data/claudetodo.db)
  tasks | task_context | refresh_schedule | sync_log
Tornado Web Server (localhost:8766)
  Reads SQLite → serves dashboard + REST API + WebSocket
```

WorkIQ queries happen inside Claude Code commands only. The Tornado server never calls WorkIQ directly.

## Email Integration Policy

**All email scanning as a task source is disabled.**

### Why email scanning is disabled (April 2026)
WorkIQ cannot reliably scope email queries — neither flagged nor unflagged — to the Inbox folder only. The "flagged inbox" query (Step 2c) was returning unflagged emails from all folders, producing noisy, irrelevant suggestions. Step 2b (awaiting-response) was also restricted to Teams-only to prevent email leakage.

### Where email is also used as read-only context
Email serves as context to enrich tasks that already exist:
- `/waiting-check` — cross-channel queries include email to detect if a key person replied via email even when the task originated from Teams or meetings
- `/todo-review` — coaching context refresh includes email to build a fuller picture of task history and communication

### Why person-scoped email queries work
These queries succeed where broad scanning fails because they ask a narrower question: "did [specific person] email about [known topic] in [recent window]?" Even archived replies are valid signal — a response is a response regardless of where Outlook filed it.

### Re-evaluation
Re-evaluate the broad email policy when Graph MCP or improved WorkIQ unflagged email folder access becomes available.

## Key Files
- `src/db.py` — SQLite schema, connection management
- `src/models.py` — Task CRUD, lifecycle (promote/dismiss/complete/start), context, sync log
- `src/app.py` — Tornado routes, entry point (port 8766), `start_server()` for embedded use
- `src/handlers/task_api.py` — REST API: /api/tasks, /api/tasks/<id>, /api/stats
- `src/handlers/task_actions.py` — POST /api/tasks/<id>/action
- `src/handlers/ws.py` — WebSocket at /ws for live updates
- `src/handlers/dashboard.py` — GET / renders dashboard
- `src/services/workiq_queries.py` — M365 query templates (Teams/meetings scan + awaiting-response)
- `src/services/claude_runner.py` — Shared `copilot -p` subprocess manager (label-based dedup)
- `src/services/refresh_scheduler.py` — Adaptive refresh intervals
- `scripts/todoness_tray.pyw` — System tray launcher (runs server in background thread)
- `scripts/install_startup.py` — Register TodoNess as a Windows logon startup task
- `scripts/uninstall_startup.py` — Remove startup task and stop running tray process

## Commands
| Command | Purpose | Uses WorkIQ? |
|---------|---------|-------------|
| `/todo` | Status summary + dashboard URL | No |
| `/todo-add "text"` | Add task via NL, Claude parses + WorkIQ resolves | Yes |
| `/todo-parse` | Parse unparsed tasks from dashboard | Yes |
| `/todo-refresh` | Full M365 scan: Teams/meetings + awaiting-response → suggested tasks | Yes |
| `/todo-review` | Interactive review of tasks needing attention | Yes |
| `/waiting-check` | Check for activity on waiting tasks | Yes |
| `/suggestion-check` | Check if suggested tasks are already resolved | Yes |

## Skills
- `respond-email` — Draft email response for email-sourced tasks
- `schedule-meeting` — Suggest meeting times based on calendar availability
- `teams-message` — Draft a Teams message for chat-based tasks
- `follow-up` — Draft a follow-up message for tasks needing a check-in
- `prepare` — Build preparation notes for meetings/presentations
- `suggestion-check` — Check suggested tasks for resolution status (runs every 3 hr)

## Database Schema
Four tables: `tasks`, `task_context`, `refresh_schedule`, `sync_log`. See `src/db.py` for full schema.

## Task Status Flow
- **suggested** → active, waiting, snoozed, dismissed, deleted
- **active** → in_progress, waiting, snoozed, completed, dismissed, deleted
- **in_progress** → active, waiting, snoozed, completed, deleted
- **waiting** → active, in_progress, snoozed, completed, deleted
- **snoozed** → active, completed, dismissed, deleted
- **completed** → active, deleted
- **dismissed** → active, suggested, deleted

## Parse Status Flow (dashboard input)
unparsed → queued → parsing → parsed

## Sync Flow (/todo-refresh)
1. Check sync_log for last sync time → determine scan window (1-7 days)
2. WorkIQ scans (separate calls): (a) Teams messages + meeting action items, (b) awaiting-response (Teams only), (c) DISABLED — email scanning
3. 3-tier priority validation: Direct (keep priority) → Group (downgrade by 1) → Tangential (P5)
4. Two-pass dedup: exact match on source_id, then semantic match by sender
5. Augment existing tasks with new context (dismissed items are never re-suggested)
6. Parse any unparsed tasks (same as /todo-parse)
7. Log to sync_log with result summary JSON

## Running

### Console mode
```bash
# Start the TodoNess dashboard (foreground, logs to stderr)
python -m src.app 8766
# Open http://localhost:8766
```

### System tray mode (Windows)
```bash
# Launch via pythonw (no console window, logs to data/todoness.log)
pythonw scripts/todoness_tray.pyw
```
The tray icon provides "Open Dashboard", "Sync Now", and "Stop & Exit" menu items.
Single-instance guard via `data/todoness.pid`.

### Start at Windows logon
```bash
# Install as a Windows Task Scheduler task (runs at logon)
python scripts/install_startup.py

# Remove startup task and stop running instance
python scripts/uninstall_startup.py
```
The installer registers a scheduled task named "TodoNess" that runs `pythonw scripts/todoness_tray.pyw` at logon. It also installs `pystray` and `Pillow` if missing.
