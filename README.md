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

## Settings

App-wide settings live in `data/settings.json`, which is gitignored because it
is per-user. Every value fails closed: if the file is missing, unreadable or
malformed, TodoIQ falls back to its shipped defaults rather than to no
behaviour at all.

```json
{
  "cowork_api_transport": true,
  "cowork_voice": {
    "teams": "work-teams-voice",
    "email": "work-email-voice",
    "default_channel": "teams"
  },
  "meeting_preferences": {
    "default_minutes": 25,
    "start_offset_minutes": 5,
    "notes": ""
  }
}
```

### `cowork_voice` — which voice a draft is written in

Cowork drafts are written in your own voice using a Cowork skill, chosen by the
channel the task is bound to. Set `teams` and `email` to the skill you want for
each. Set either to `null` to name no skill for that channel; the inline
mechanics (contractions, no em-dashes, no corporate filler, channel-specific
openings and sign-offs) still apply, because a skill lives outside this repo and
can change or fail to resolve without a code change.

`default_channel` is the voice used when a task carries no channel signal of its
own — typically one you typed yourself rather than one derived from a Teams
thread or a mail item. It is the LAST thing consulted: a task from a Teams
thread is written in the Teams voice regardless of what this is set to. It
selects a voice only, and never binds a recipient, so a task with no destination
still shows "No delivery destination selected". Leave it `null` to keep the
neutral register, which avoids both a subject line and a sign-off.

### `meeting_preferences` — how meetings get proposed

Standing defaults applied whenever a draft proposes or books a meeting time.
`default_minutes` is the length to assume, and `start_offset_minutes` is how far
past the hour or half hour to start. The offset is fixed and does not scale with
length: at 5, a 25 minute meeting runs :05 to :30 and a 55 minute meeting runs
:05 to :00. `notes` is free text for anything else standing ("Never book me
before 9am").

Omit the block entirely and nothing is added to the prompt. Values that are not
sane numbers are dropped rather than passed through, so a malformed block
behaves like an absent one.

This is deliberately not tied to a task's action type. The per-action guidance
is, and only 6 of the 17 open tasks that read as scheduling are classified that
way, so anything keyed to it applies about a third of the time. These
preferences ride every prompt, phrased as a condition, so they also apply when a
task is redirected into a meeting after the fact.

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
