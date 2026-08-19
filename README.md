# TodoIQ

Local AI-powered task manager that integrates with Microsoft 365 via WorkIQ MCP.

TodoNess scans your Teams messages, meetings, and flagged emails to surface actionable items as suggested tasks. It provides AI coaching, follow-up drafting, and meeting prep through a web dashboard.

## Prerequisites

- Python 3.11+
- GitHub Copilot CLI installed and authenticated
- WorkIQ MCP configured for Microsoft 365 task discovery
- Cowork authenticated for drafting and approved actions

## Quick Start

```bash
# Install dependencies and register Riveter to start at logon
python scripts/install_startup.py

# The installer can start Riveter immediately.
# Open http://localhost:8766
```

For a foreground development run instead:

```bash
python -m pip install -r requirements.txt
python -m src.app 8766
```

## Usage

Open [http://localhost:8766](http://localhost:8766) and work entirely from the
Riveter dashboard.

### Add a task naturally

Type a quick reminder into **Add a task** as you would write it to yourself:

> send raj the complete adoption deck in teams

Riveter turns it into a structured task, resolves people, identifies the likely
next action, and adds useful context. If a name has multiple matches, choose the
right person from the **Key people** menu before continuing.

### Work the task

1. Select a task from the left pane.
2. Review its description, priority, due date, people, source, and notes.
3. Correct anything that Riveter inferred incorrectly.
4. Choose or change the action type, such as **Schedule meeting**, **Reply by
   email**, **Follow up**, or **Prepare**.
5. Add a note when Cowork needs extra context that is not in the source material.

### Use Cowork

Select **Preview with Cowork** to research the task and prepare the action.
Cowork can check Microsoft 365 context, draft a response, or find meeting
availability. Nothing is sent during preview.

If Cowork needs a decision, answer directly in the task card. Review and edit
the final draft or meeting details, confirm the destination, and then use the
final approval button to perform the action.

### Keep the list current

- **Mark complete** when the work is finished. Delivered Cowork actions also
  show a subtle completion button in their receipt.
- Use **Waiting** when someone else owes the next move.
- Use **Snooze** when the task should return later.
- Accept useful suggestions and dismiss ones that are not actionable.
- Use **Refresh** when the task context or source conversation has changed.

Riveter runs Microsoft 365 discovery and task maintenance in the background, so
normal use does not require terminal commands.

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

## Safe demo instance

Run a separate, fictional demo on port 8776. It uses only `data/demo/` and
hard-disables WorkIQ, Copilot, Cowork, and Microsoft 365 actions.

```bash
# Create/reset the fictional data (server must be stopped)
python scripts/demo_server.py reset

# Start persistently in the background
python scripts/demo_server.py start

# Open http://localhost:8776

# Check or stop it
python scripts/demo_server.py status
python scripts/demo_server.py stop
```

The demo dataset includes suggested, active, waiting, snoozed, and completed
tasks plus ready, delivered, unconfirmed, and safe scheduling-fallback Cowork
cards. Reset is deterministic, so recordings can start from the same state.

## Dependencies

Core app: `tornado`, `jinja2` (see `requirements.txt`).

Tray launcher (optional): `pystray`, `Pillow`.
