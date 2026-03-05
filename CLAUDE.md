# TodoNess - Project Notes

## Overview
TodoNess is a local AI-powered task manager that integrates with Microsoft 365 via WorkIQ MCP. It reads M365 context (flagged emails, meetings, calendar) to surface actionable suggestions, lets users manage tasks through a web dashboard, and provides AI coaching on each task.

## Technology Stack
- **Runtime:** Python 3.11
- **Web Framework:** Tornado 6.5
- **Templating:** Jinja2 3.1.6
- **Database:** SQLite3 (WAL mode)
- **Dependencies:** Zero external — all pre-installed

## Architecture
```
Claude Code Commands (/todo-refresh, /todo-review)
  ↕ calls WorkIQ MCP    ↕ writes SQLite
SQLite DB (data/claudetodo.db)
  tasks | task_context | refresh_schedule | sync_log
Tornado Web Server (localhost:8766)
  Reads SQLite → serves dashboard + REST API + WebSocket
```

WorkIQ queries happen inside Claude Code commands only. The Tornado server never calls WorkIQ directly.

## Key Files
- `src/db.py` — SQLite schema, connection management
- `src/models.py` — Task CRUD, lifecycle (promote/dismiss/complete/start), context, sync log
- `src/app.py` — Tornado routes, entry point (port 8766), `start_server()` for embedded use
- `src/handlers/task_api.py` — REST API: /api/tasks, /api/tasks/<id>, /api/stats
- `src/handlers/task_actions.py` — POST /api/tasks/<id>/action
- `src/handlers/ws.py` — WebSocket at /ws for live updates
- `src/handlers/dashboard.py` — GET / renders dashboard
- `src/services/workiq_queries.py` — M365 query templates (Teams/meetings scan + awaiting-response)
- `src/services/claude_runner.py` — Shared `claude -p` subprocess manager (label-based dedup)
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

## Skills
- `respond-email` — Draft email response for email-sourced tasks
- `schedule-meeting` — Suggest meeting times based on calendar availability
- `teams-message` — Draft a Teams message for chat-based tasks
- `follow-up` — Draft a follow-up message for tasks needing a check-in
- `prepare` — Build preparation notes for meetings/presentations

## Database Schema
Four tables: `tasks`, `task_context`, `refresh_schedule`, `sync_log`. See `src/db.py` for full schema.

## Task Status Flow
- **suggested** → active, waiting, dismissed, deleted
- **active** → in_progress, waiting, completed, dismissed, deleted
- **in_progress** → active, waiting, completed, deleted
- **waiting** → active, in_progress, completed, deleted
- **completed** → active, deleted
- **dismissed** → active, suggested, deleted

## Parse Status Flow (dashboard input)
unparsed → queued → parsing → parsed

## Sync Flow (/todo-refresh)
1. Check sync_log for last sync time → determine scan window (1-7 days)
2. WorkIQ scans (separate calls): (a) Teams messages + meeting action items, (b) awaiting-response items
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
