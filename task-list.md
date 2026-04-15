# Task List

## Open Tasks
- [ ] #006 | 2026-03-09 | XL | Browser Action Tasks — Playwright two-phase recon/execute for form-filling tasks

- [ ] #008 | 2026-03-12 | XL | Recurring Tasks — tasks that repeat on a schedule, each recurrence runs full AI coaching and AI skill execution

- [x] #013 | 2026-04-15 | M | Add source_date column to tasks schema — No source_date field exists, so we can't tell when the underlying Teams message/meeting/email occurred (only when the DB record was created). Add source_date TEXT (ISO 8601), populate from WorkIQ timestamps during /todo-refresh, backfill where possible. Enables staleness detection for auto-dismissing old suggestions. | completed: 2026-04-15

- [x] #014 | 2026-04-15 | L | Improve semantic dedup in todo-refresh — Current dedup only catches exact source_id matches. Analysis found 29 duplicate groups covering 112/166 suggested tasks (~67%), plus ~15-20 suggestions duplicating active/waiting tasks. Needs: (1) fuzzy title comparison within suggested tasks, (2) cross-status dedup against active/waiting/in_progress tasks, (3) person+topic matching from source_id fields. Worst offenders had 5-6 copies of the same task. | completed: 2026-04-15

## In Progress

## Completed
- [x] #012 | 2026-03-14 | XS | Replace claude -p subprocess calls with copilot -p | completed: 2026-03-14
- [x] #011| 2026-03-12 | S  | Periodic DB backup — automatically back up claudetodo.db on a schedule | completed: 2026-03-12
- [x] #010 | 2026-03-12 | S  | Filter by key person — dashboard filter that shows all tasks related to a specific person across all statuses | completed: 2026-03-12
- [x] #009 | 2026-03-12 | M  | Check suggestions for progress — /suggestion-check command via WorkIQ | completed: 2026-03-12

## Completed
- [x] #007 | 2026-03-10 | Add quick-task filter at the top of the active section on the dashboard | completed: 2026-03-12
- [x] #005 | 2026-03-09 | Scale parse timeout by batch size (base 5 min + 3 min per task) | completed: 2026-03-09
- [x] #002 | 2026-03-06 | Add email context enrichment to /todo-review coaching refresh | owner: review-agent | completed: 2026-03-06
- [x] #001 | 2026-03-06 | Update /waiting-check to query ALL channels for all waiting tasks | owner: command-editor | completed: 2026-03-06
- [x] #003 | 2026-03-06 | Document "email as context, not source" pattern in claude.md | owner: docs-writer | completed: 2026-03-06
