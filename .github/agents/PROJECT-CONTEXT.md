# PROJECT-CONTEXT: TodoIQ

## One-liner
TodoIQ is a local Windows task manager that detects actionable Microsoft 365
work through WorkIQ, stores it in SQLite, and exposes a Tornado dashboard with
Cowork-powered draft, approval, and direct-action flows.

## Architecture principles (immutable unless explicitly revised)
- WorkIQ detects tasks; Cowork researches/drafts actions; TodoIQ owns durable
  task state and human-facing approval boundaries.
- Cowork previews are write-barriered. External M365 writes require a separate
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
- Unit tests: `python -m pytest -q` (pytest/unittest; `tests/test_*.py`).
- E2E / visual tests: `python -m pytest -q -m e2e` (Playwright Chromium;
  `tests/e2e/`, screenshots under `temp/` or `test-runs/`).
- Lint / typecheck / build: not configured. Use `node --check <changed-js>` for
  JavaScript syntax.
- Coverage / mutation gates: not configured.
- Current suite status: 263 unit tests + 52 subtests and 48 E2E tests green on
  2026-08-01.
- E2E must run in a separate pytest invocation; `pytest.ini` deselects it by
  default because Playwright's sync API conflicts with Tornado test loops.

## Deploy
- Environments:
  - Dev/dogfood: `http://localhost:8768` from this worktree.
  - Production/tray: `http://localhost:8766`, scheduled task `TodoNess`.
- Dev deploy command: from the selected checkout,
  `python -m src.app 8768`.
- Prod deploy command: from the selected checkout,
  `python scripts/install_startup.py`, start now = Yes. Requires explicit
  current-conversation production approval and port 8766 ownership cleanup.
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

- Rollback procedure:
  1. Stop the deployed tray by its exact PID and stop all other TodoIQ writers.
  2. Create timestamped backups of both checkout DBs with SQLite backup/checkpoint.
  3. Copy/restore the authoritative deployed DB into the rollback checkout's
     `data/claudetodo.db` (new schema is additive; old code ignores `task_actions`).
  4. Run the rollback checkout's `python scripts/install_startup.py`, start now.
  5. Verify task count, latest sync timestamp and all probes above.

## Known gotchas / latent bugs
- Main and worktree DB files do not synchronize. One must be explicitly
  authoritative during a rollout.
- `task_actions` latest row is ordered by integer `id`, not second-precision
  timestamps.
- Cowork `tool_trace[].ok` means the call returned, not that a write executed.
- Execution claims delivery only after a recognized write tool succeeds on the
  unbarriered execution turn; ambiguous outcomes require checking the destination.
- Cowork CLI 1.21.89 resumed SSE omits required `conversationId`; TodoIQ backend
  resume needs the compatibility shim and persisted full island URL.
- Cowork auth can expire between successful runs; runner performs one silent
  refresh and one retry only.
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
- Production approval wording and post-deploy probes are mandatory.
