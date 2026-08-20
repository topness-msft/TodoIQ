---
description: Draft a Copilot Cowork prompt for scheduling a meeting
---

Draft a ready-to-paste prompt for Microsoft Copilot Cowork that combines all task context into a single scheduling request the user can hand off to Cowork.

**Input:** $ARGUMENTS (task ID — **required**)

Today's date is $CURRENT_DATE.

## Step 0: Validate input

If `$ARGUMENTS` is empty or not a valid integer, stop immediately with:
> **Usage:** `/cowork-prompt <task_id>`
>
> Example: `/cowork-prompt 19`

## Step 1: Read the task from SQLite

```python
import sqlite3

conn = sqlite3.connect('$PROJECT_ROOT/data/claudetodo.db')
conn.row_factory = sqlite3.Row
task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
conn.close()
```

If the task doesn't exist, stop with: "Task #[id] not found."

Extract: `title`, `description`, `key_people`, `user_notes`, `due_date`, `coaching_text`, `skill_output`, `related_meeting`.

## Step 2: Build the Cowork prompt

Compose a natural-language prompt the user will paste into Copilot Cowork. Cowork will find available times and schedule the meeting — so the prompt must give it everything it needs in one shot.

The task already has rich context from prior AI enrichment. Use these fields as your primary source material:

- **`coaching_text`** — AI coaching with discussion points, background, suggested agenda items, and preparation notes. This is the richest source for the meeting agenda and purpose.
- **`skill_output`** — output from prior skills (e.g. `/schedule-meeting` availability, `/prepare` notes). If it contains suggested time slots, reference them as preferred times. If it contains prep notes or talking points, fold them into the agenda.
- **`user_notes`** — the user's own notes, which may contain duration preferences, rescheduling intent, or specific asks.

Build the prompt with these elements:

1. **Goal** — schedule (or reschedule) a meeting. If `user_notes` mention rescheduling, frame it that way. If `related_meeting` is set, reference the existing meeting by name.
2. **Participants** — full names from `key_people`. Cowork resolves them in the org directory.
3. **Agenda / purpose** — pull discussion points and context from `coaching_text` and `skill_output`. Don't just use the title — surface the specific topics, decisions needed, and background that coaching already identified.
4. **Duration** — check `user_notes`, `coaching_text`, and `skill_output` for duration hints (e.g. "45 min", "1 hour", "quick sync"). Default to 25 minutes if nothing specified.
5. **Scheduling constraints** — use `due_date` if set ("find a time before March 15"). If no due date, say "this week" or "in the next few days". Add "during working hours" and prefer morning slots. If `skill_output` contains specific free slots from a prior `/schedule-meeting` run, suggest those as preferred times.
6. **Any other preferences** from `user_notes` — e.g. "avoid Fridays", "needs to be in-person", "include a Teams link".

**Do NOT call WorkIQ.** Everything needed is already in the task fields.

## Step 3: Output the prompt

**You MUST output using this EXACT format, including the `<<<SKILL_OUTPUT>>>` and `<<<END_SKILL_OUTPUT>>>` marker lines:**

```
<<<SKILL_OUTPUT>>>
Copilot Cowork prompt (copy and paste):

---
[The drafted prompt text — ready to paste into Cowork]
---

Participants: [names]
Duration: [duration]
Topic: [title]
<<<END_SKILL_OUTPUT>>>
```

**Example output:**

```
<<<SKILL_OUTPUT>>>
Copilot Cowork prompt (copy and paste):

---
Schedule a 25-minute meeting with Jane Doe and Alex Kim to review the Q2 budget proposal. We need to discuss the revised headcount numbers and agree on the final submission before the March 15 deadline. Please find a time that works for all attendees before March 14, during working hours, preferably in the morning.
---

Participants: Jane Doe, Alex Kim
Duration: 25 minutes
Topic: Q2 budget review
<<<END_SKILL_OUTPUT>>>
```

**Guidelines:**
- Write as a direct instruction to Cowork — clear, specific, all-in-one
- Front-load the scheduling ask, then the agenda context
- Keep it to one paragraph — Cowork handles concise prompts best
- If `user_notes` mention specific topics or preparation, weave them into the agenda portion
- If there's a `related_meeting`, frame as rescheduling that meeting rather than creating a new one

Your output will be automatically saved to the dashboard. No further action needed.
