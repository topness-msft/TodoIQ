# PROJECT-CONTEXT: TodoIQ

## One-liner
TodoIQ is a local Windows task manager that detects actionable Microsoft 365
work through WorkIQ, stores it in SQLite, and exposes a Tornado dashboard with
structured WorkIQ delivery plus Cowork-powered general action flows.

## Architecture principles (immutable unless explicitly revised)
- WorkIQ detects tasks and owns structured meeting, email, and Teams delivery;
  Cowork researches/drafts all other actions. TodoIQ owns durable task state and
  human-facing approval boundaries.
- Structured WorkIQ previews expose read-only tools. "Read-only" means the tool
  itself cannot write, not that the operation is conceptually a read: Graph's
  `findMeetingTimes` and `getSchedule` are POST actions reachable only through
  `workiq-do_action`, the same primitive that sends mail, so they stay out of
  preview even though they only read. Slots offered to the user must come from a
  live M365 query, and the evidence records which class of query produced them
  (`FindMeetingTimes+interaction` for Cowork's scheduler call, `copilot-ask` for
  the structured preview's Copilot query). Execution is gated on that evidence
  being a recognised live source, never on a label the writer chose for itself.
  Execution exposes one
  channel-specific write primitive and requires a correlated external reference.
  Cowork previews remain write-barriered. External M365 writes require a separate
  TodoIQ execution action, an exact-draft confirmation, and a durable child audit
  row with duplicate-send protection.
- SQLite is the source of truth. Schema changes are additive/idempotent and
  migrations run before the server listens.
- Never infer delivery audience from Cowork `conversation_id`; destination and
  conversation identity are separate data.
- External tools are defence-in-depth, not a sandbox. `--deny-tools` is not a
  safety control; preview callback interception and the explicit execution
  confirmation boundary are.

## Module map / key files
- `src/app.py` — Tornado route registration, startup migrations/recovery,
  periodic jobs, port entry point.
- `src/db.py` — SQLite path, schema, WAL connection setup and migrations.
- `src/models.py` — task lifecycle, sync data, task-action persistence.
- `src/handlers/cowork.py` — preview, interaction-answer, and explicit execute
  routes plus cautious delivery finalization.
- `src/services/cowork_runner.py` — prompt/output parsing, write-tool callback
  config, subprocess registry, auth recovery and island resolution. Large
  high-blast-radius module; prompt composition starts near the top, parser near
  the middle, process runner in the lower third.
- `src/services/claude_runner.py` — shared `copilot -p` subprocess manager for
  WorkIQ-backed slash commands.
- `src/services/structured_delivery.py` — read-only WorkIQ preview prompts,
  channel-isolated write prompts, strict correlated results, and worker lifecycle
  for calendar, email, and Teams delivery.
- `static/js/dashboard.js` — `/` dashboard behavior and Cowork card. Large
  classic script; task list/detail rendering is in the first quarter, Cowork
  rendering/transitions/polling near the end.
- `static/mock-todo.html` — production `/todo` document plus standalone mock
  behavior. `src/handlers/todoiq.py` injects `todoiq-api.js`.
- `static/js/todoiq-api.js` — API-backed overrides for `/todo`.
- `scripts/todoness_tray.pyw` — background tray host, PID guard and port 8766.
- `scripts/install_startup.py` / `uninstall_startup.py` — Windows Task Scheduler
  registration and tray lifecycle.
- `.claude/commands/todo-refresh.md` — live WorkIQ detection/dedup orchestration;
  much business logic remains prompt-driven.

## Load-bearing / high-blast-radius paths
- `data/claudetodo.db` — authoritative user data. Stop all writers and use the
  SQLite backup API/checkpoint before moving or replacing it.
- `src/db.py`, `src/models.py` — schema and lifecycle integrity.
- `src/services/cowork_runner.py`, `src/handlers/cowork.py` — M365 write safety,
  auth, process recovery and action audit chain.
- `scripts/todoness_tray.pyw` / scheduled task `TodoNess` — production process
  ownership and autostart.
- Static UI files are lower data risk but require Playwright visual gates.

## Build / test tooling  (verified)
- Test dependencies: `pip install -r requirements-dev.txt` then
  `playwright install chromium`. `requirements.txt` stays runtime-only.
- Unit tests: `python -m pytest -q` (pytest/unittest; `tests/test_*.py`).
- E2E / visual tests: `python -m pytest -q -m e2e` (Playwright Chromium;
  `tests/e2e/`, screenshots under `temp/` or `test-runs/`).
- Lint / typecheck / build: not configured. Use `node --check <changed-js>` for
  JavaScript syntax.
- Coverage / mutation gates: not configured.
- Current suite status: 1123 unit tests + 201 subtests, and 214 E2E tests, all
  green on 2026-08-24 (full e2e run 3m16s).
- E2E must run in a separate pytest invocation; `pytest.ini` deselects it by
  default because Playwright's sync API conflicts with Tornado test loops.
- E2E tests share ONE session-scoped server and database. `tests/e2e/conftest.py`
  clears the task tables before every test — without that, rows leaked by
  failing tests accumulate past the 200-row cap in `list_tasks`
  (`src/models.py:241`) and later tests silently stop seeing the tasks they
  seed, failing as 30s selector timeouts in the full run while passing alone.
- Browser tests carry a 90s per-test timeout (attached in the e2e conftest, not
  `pytest.ini`, so unit tests are unaffected).

## `tasks.waiting_activity` contract
One TEXT column, JSON, written by three commands with different vocabularies
(`waiting-check`, `suggestion-check`, `todo-parse`). The v2 shape and the rules
for reading it live in `src/services/waiting_activity.py`; `src/models.py`
`_row_to_dict` normalises every row through it on read and attaches a derived
`waiting_signal`, so legacy rows render without a migration. The raw column is
never rewritten — client-side search and the commands' own `json_extract`
queries still read it directly.

The commands build their JSON inline in bash and cannot import the module, so
`tests/test_waiting_activity.py::TestProducerReaderContract` is the only thing
holding writer and reader together. Change one, change both.

`check_state: failed` means the check did not run. It must never render as a
finding: "could not check" and "checked, found nothing" are different answers.
`may_be_resolved` is an inference and renders as "Looks done?" with a magnifier
— never the completion tick, and nothing auto-completes a task from it.

## `tasks.source_locator` contract
`source_id` is a dedup key (`{type}::{person}::{subject_first_50}`), NOT a
locator — different threads about one subject collide by design. `source_locator`
is the re-openable identifier: JSON, one plain column (no CHECK, so it never
triggers `_rebuild_tasks_constraints`), shape owned by
`src/services/source_locator.py`.

`db.backfill_source_locators` runs once at startup and recovers locators from
links already in `source_url` (1,775 of 2,434 live tasks, all thread-readable:
1,277 Teams chats, 411 meetings, 87 email).
It is idempotent and only touches rows whose column is empty, so anything
captured at creation is never overwritten. Deriving on read instead would put
~1,900 regexes on every `list_tasks` call; `_row_to_dict` keeps a single-URL
fallback for tasks created after the pass.

Honesty rules baked into the shape:
- A `teams_channel` needs team_id + channel_id + message_id. A partial triple is
  refused — it would read as a locator and locate nothing.
- `"captured"` (recorded at creation) is never the fallback for an unrecognised
  value; reconstructions are always `"derived_from_url"`.
- `is_thread_readable` is defined as "`read_plan` returns something", so a caller
  cannot be told a thread is readable and then handed nothing to read.

`read_plan` owns the endpoint sequences, all verified against live Graph on
2026-08-24 (the recurring failure here is a worker shown no endpoint inventing
none):
- `teams_chat` — `/me/chats/{conversation_id}/messages`
- `teams_channel` — `/teams/{team}/channels/{channel}/messages/{id}/replies`
- `email` — `/me/messages/{ItemID}` for `conversationId`, then
  `/me/messages?$filter=conversationId eq '...'`. **Never add `$orderby`** to
  that filter: Graph rejects the pair as `InefficientFilter` (400). This is one
  conversation by id, not a mailbox scan, so it stays inside the email policy.
- `meeting` — `/me/events/{eventId}` for `onlineMeeting.joinUrl`, extract
  `19:meeting_...@thread.v2`, then read that chat.

Outlook `?ItemID=` is a Graph message id and Teams `/l/meeting/details?eventId=`
is a Graph event id. Both were once recorded here as unresolved spikes with the
fields left null; probing settled both, and the nulls were costing 387 tasks.

`/todo-refresh` writes this inline from bash and cannot import the module, so
`tests/test_source_locator.py::TestProducerReaderContract` holds writer and
reader together. Delivery paths (`cowork_runner`, `structured_delivery`) still
call `parse_source_url` directly — this module wraps it, and replacing those
broadcast-audience callers needs its own parity audit.

## Deploy
- Environments:
  - **Production/tray: `C:\Users\phtopnes\Riveter\app`**, a dedicated clone on
    `main`, serving `http://localhost:8766` via scheduled task `TodoNess`. Its
    database is `C:\Users\phtopnes\Riveter\app\data\claudetodo.db`. Update it
    with `git -C C:\Users\phtopnes\Riveter\app pull`, then re-run the installer.
    Production deliberately does NOT run from a worktree: worktrees are
    disposable, and pinning logon startup to one meant the tray broke or fell
    back to older code when it was cleaned up.
  - Dev/dogfood: `http://localhost:8768` from a worktree. It uses that
    worktree's own `data/claudetodo.db`, which is NOT production data.
  - Isolated live demo: `http://localhost:8776`, fictional seed plus explicitly
    enabled parsing/Cowork capabilities, with all local state under `data/demo/`.
- Dev deploy command: from the selected checkout,
  `python -m src.app 8768`.
- Demo lifecycle commands, from the selected checkout:
  - `python scripts/demo_server.py stop`
  - `python scripts/demo_server.py reset`
  - `python scripts/demo_server.py start`
  - `python scripts/demo_server.py status`
  Reset is destructive only to `data/demo/riveter-demo.db` and refuses while the
  validated demo process or port is live. Never copy production data into demo.
- Prod deploy command: from the selected checkout,
  `python scripts/install_startup.py`, start now = Yes. Riveter is a single-user
  local prototype: once relevant tests and required visual gates pass, production
  deployment does not require a separate approval prompt. This exception is
  scoped to this project only. Port 8766 ownership cleanup remains mandatory.
- Auto-deploy triggers: none. Git push does not deploy.
- Known-broken deploy patterns to REFUSE:
  - Never run two TodoIQ writers against different DBs and call them synced.
  - Never start a new tray before stopping the current port-8766 listener.
  - Never rollback code by restarting the old checkout without first copying
    the authoritative DB into that checkout.
  - Never copy a live SQLite file while a writer is running.
  - Never deploy while Cowork preview or execution subprocesses are active.
- In-flight-work hazard: stopping the server interrupts running Cowork actions;
  startup recovery changes stranded previews to `failed` and executions to
  `execute_unconfirmed` so Riveter never invents delivery success or failure.
- Post-deploy verification probes:

  | Route | Method | Expected | Why |
  |---|---|---|---|
  | `/api/stats` | GET | 200 | Server and authoritative DB are readable |
  | `/` | GET | 200 | Main dashboard renders |
  | `/todo` | GET | 200 | Alternate UI and adapter render |
  | `/api/sync-status` | GET | 200 | WorkIQ sync state is available |
  | `/api/runner-status` | GET | 200 | No orphaned subprocess state |
  | `/static/js/dashboard.js` | GET | 200 | Current static feature bundle served |

- Demo verification additionally requires:
  - exactly 55 seeded tasks and exactly 7 prebuilt action rows in `ready` state
  - statuses: 18 suggested, 12 active, 3 in progress, 6 waiting, 6 completed,
    5 dismissed, and 5 snoozed
  - four suggested tasks carry deterministic activity summaries; exactly two are
    `likely_resolved`, with one each `activity_detected` and `may_be_resolved`
  - source types cover chat, email, meeting, and manual tasks; action types cover
    scheduling, Teams follow-up, email response, preparation, document review,
    and awaiting-response flows
  - chat/Teams, meeting, and email each have at least 18 source records
  - prebuilt Cowork results cover two Teams delivery drafts, three email delivery
    drafts, and two meeting scheduling previews; none has execution, destination
    confirmation, delivery confirmation, or successful write-tool evidence
  - the scheduling previews include one three-person and one two-person meeting,
    each with three query-backed 25-minute UTC options beginning at 5 or 35
    minutes past the hour
  - all demo contacts are limited to Rima Reyes, Bobby Chang, Luis Camino, Steve
    Jeffery, Manuela Pichler, Adrian Maclean, and Aamer Kaleem, with fictional
    confirmed identity fields and UI-compatible alternatives arrays
  - every task uses the fixed `2026-08-20T18:00:00Z` seed timestamp and every
    source ID uses the `demo::` namespace; all 6 chat tasks use approved
    fabricated `teams.microsoft.com/l/message/` URL shapes that
    `parse_source_url` classifies as one-to-one, while non-chat URLs remain null
  - every description is a substantive narrative naming a key person and
    recording an August 2026 date, surrounding context, current state, and next
    step; every expanded source summary synthesizes at least two channels and
    states the current status rather than presenting an isolated quote
  - `data/demo/settings.json` has `cowork_api_transport: true`
  - sync and standalone skill POST routes return 403
  - parsing, Cowork session, and approved execution flags are present on the
    validated demo process
  - ports 8766 and 8768 retain their original PIDs, DB hashes, and health probes
  - browser load issues no requests outside `127.0.0.1:8776` until the presenter
    explicitly starts parsing or Cowork

- Demo rollback: stop port 8776 with `demo_server.py stop`, check out the prior
  commit, then run reset/start/status. Production and dev are never part of demo
  rollback.

- Rollback procedure:
  1. Stop the deployed tray by its exact PID and stop all other TodoIQ writers.
  2. Create timestamped backups of both checkout DBs with SQLite backup/checkpoint.
  3. Copy/restore the authoritative deployed DB into the rollback checkout's
     `data/claudetodo.db` (new schema is additive; old code ignores `task_actions`).
  4. Run the rollback checkout's `python scripts/install_startup.py`, start now.
  5. Verify task count, latest sync timestamp and all probes above.

## Known gotchas / latent bugs
- Main and worktree DB files do not synchronize. One must be explicitly
  authoritative during a rollout. Production is
  `C:\Users\phtopnes\Riveter\app\data\claudetodo.db`; the copy that used to be
  live in this worktree is renamed `claudetodo.superseded-20260824.db` so
  nothing can serve it by accident.
- `task_actions` latest row is ordered by integer `id`, not second-precision
  timestamps.
- Cowork `tool_trace[].ok` means the call returned, not that a write executed.
- Structured execution claims delivery only after a matching correlation ID and
  non-empty WorkIQ delivery reference. Ambiguous outcomes are never retried
  automatically and require checking the destination.
- Cowork CLI 1.21.89 resumed SSE omits required `conversationId`; TodoIQ backend
  resume needs the compatibility shim and persisted full island URL.
- Cowork auth can expire between successful runs; runner performs one silent
  refresh and one retry only.
- Direct Microsoft Graph is NOT an available transport, and this was measured on
  2026-08-22 rather than assumed. Riveter authenticates as the Azure PowerShell
  first-party client (`1950a258-227b-4e31-a9cf-717495945fc2`) against a
  Cowork-scoped resource. Its MSAL cache holds one refresh token targeted at
  `6ab48b67-.../access_as_user` with no `family_id`, so it is not a FOCI token
  and silent acquisition of any Graph scope returns None. Device-code consent
  fails `AADSTS53003` (Conditional Access cannot bind a compliant device through
  that flow), and interactive consent fails `AADSTS65002`: first-party client to
  first-party resource requires preauthorization by the API owner. No tenant
  admin can grant this. Reaching Graph directly would require registering a new
  Entra application plus delegated consent for `Calendars.ReadWrite`,
  `Mail.Send` and `ChatMessage.Send`. Until that exists, WorkIQ is the only
  supported path to Graph, and structured delivery must keep going through it.
- WorkIQ read tools cannot see another person's calendar: `/me/calendarView`
  returns 200 but `/users/{other}/calendarView` returns 404. Attendee free/busy
  is only reachable through POST actions (`/me/findMeetingTimes`,
  `getSchedule`), which need `workiq-do_action` — the same primitive that can
  send mail. That is why structured previews currently let the model reason
  about availability instead of calling Graph's scheduler, and why suggested
  slots can differ between two runs of the same task.
- Cowork can emit `ta` technical approvals for Teams `PostMessage`; direct
  execution answers only when the tool, Teams thread and draft exactly match
  Riveter's immutable approval snapshot. Uncaptured email/calendar shapes fail
  closed. Calendar `CreateEvent` has also failed upstream after approval.
- `/todo` is served from `static/mock-todo.html`; edits to only the dashboard do
  not automatically affect it.
- OneDrive workspace root is a reparse point; allow the configured root but
  future child-path code must detect escaping junctions.

## Do-not-rebuild
- Reuse `parse_source_url`, `_refs`, `compose_prompt`, `parse_cowork_output`,
  callback-config generation and the task-action audit chain.
- Reuse per-task Cowork poller maps and startup recovery.
- Reuse `escapeHtml` / `esc`, `markdown-utils.js`, and existing overlay/modal
  patterns.
- Reuse the first-party Open-in-Cowork URL derived from `conversation_id`;
  do not build an embedded chat before exhausting that surface.
- Reuse `--file` / `--download-files` and `/upload` / `/download` for Cowork
  artifact exchange; Cowork cannot directly address local Windows paths.

## Environment notes
- Windows, Python 3.11, PowerShell. Use Windows backslash paths.
- Production is a user-session tray app, not a cloud service.
- Cowork and WorkIQ use delegated user authentication; no managed identity.
- `data/` is gitignored and contains DBs, logs, backups and local settings.
- User-approved workspace root:
  `C:\Users\phtopnes\OneDrive - Microsoft\Documents\__TodoIq`.
- Post-deploy probes are mandatory. Do not request a separate production deploy
  approval after the relevant test and visual gates pass; this standing policy is
  specific to Riveter.
