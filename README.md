# TodoIQ

Local AI-powered task manager that integrates with Microsoft 365 via WorkIQ MCP.

TodoNess scans your Teams messages, meetings, and flagged emails to surface actionable items as suggested tasks. It provides AI coaching, follow-up drafting, and meeting prep through a web dashboard.

## Prerequisites

- Python 3.11+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated
- WorkIQ MCP configured in Claude Code settings

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Start the dashboard server
python -m src.app

# Open http://localhost:8766
```

## Usage

The dashboard runs as a local web server. AI features (scanning, parsing, skills) run through Claude Code slash commands:

| Command | Purpose |
|---------|---------|
| `/todo` | Status summary and dashboard URL |
| `/todo-add "text"` | Add a task with natural language parsing |
| `/todo-parse` | Parse tasks added via the dashboard quick-add |
| `/todo-refresh` | Scan M365 for new actionable items |
| `/todo-review` | Review tasks needing attention |
| `/waiting-check` | Check for activity on waiting tasks |
| `/suggestion-check` | Check if suggested tasks are already resolved |

Skills generate contextual drafts for individual tasks:

| Skill | Purpose |
|-------|---------|
| `respond-email` | Draft an email response |
| `schedule-meeting` | Suggest meeting times |
| `teams-message` | Draft a Teams message |
| `follow-up` | Draft a follow-up message |
| `prepare` | Build meeting/presentation prep notes |

## Architecture

See [CLAUDE.md](CLAUDE.md) for detailed architecture, database schema, and development notes.

```
Claude Code Commands (/todo-refresh, /todo-add, skills)
  |-- calls WorkIQ MCP for M365 data
  |-- writes results to SQLite

SQLite DB (data/claudetodo.db)
  |-- tasks, task_context, refresh_schedule, sync_log

Tornado Web Server (localhost:8766)
  |-- reads SQLite, serves dashboard + REST API + WebSocket
```

## Run at Startup (Windows)

TodoNess can run as a background app with a system tray icon that starts automatically at logon.

```bash
# Install dependencies and register startup task
python scripts/install_startup.py

# To remove from startup
python scripts/uninstall_startup.py
```

The tray icon provides:
- **Double-click** to open the dashboard
- **Sync Now** to trigger a manual M365 scan
- **Stop & Exit** to shut down

Logs are written to `data/todoness.log`. Requires `pystray` and `Pillow` (installed automatically by the install script).

## Dependencies

Core app: `tornado`, `jinja2` (see `requirements.txt`).

Tray launcher (optional): `pystray`, `Pillow`.
