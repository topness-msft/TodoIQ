---
description: Suggest optimal meeting times for a task
---

Find shared calendar availability for a task's key people and suggest meeting slots.

**Input:** $ARGUMENTS (task ID — **required**)

Today's date is $CURRENT_DATE.

## Step 0: Validate input

If `$ARGUMENTS` is empty or not a valid integer, stop immediately with:
> **Usage:** `/schedule-meeting <task_id>`
>
> Example: `/schedule-meeting 19`

## Step 1: Read the task from SQLite

```python
import sqlite3

conn = sqlite3.connect('$PROJECT_ROOT/data/claudetodo.db')
conn.row_factory = sqlite3.Row
task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
conn.close()
```

If the task doesn't exist, stop with: "Task #[id] not found."

Extract: `key_people`, `user_notes`, `description`, `due_date`, `title`.

## Step 2: Validate key_people

Parse `key_people` as JSON. If it's empty or null, stop with:
> "Task #[id] has no key people. Add people first, then run `/schedule-meeting [id]` again."

Build a comma-separated list of full names from the key_people array (e.g. "Jane Doe and Alex Kim").

## Step 3: Query WorkIQ for shared calendar availability

Build a single multi-person query to `ask_work_iq`:

- If `due_date` is set: "What is the shared calendar availability for [Person1], [Person2], and [Person3] between now and [due_date]? Treat tentative calendar blocks as available. Only show slots during each person's Outlook working hours. Show free time slots that are at least [requested duration or 25 minutes] long."
- If no `due_date`: "What is the shared calendar availability for [Person1], [Person2], and [Person3] this week? Treat tentative calendar blocks as available. Only show slots during each person's Outlook working hours. Show free time slots that are at least [requested duration or 25 minutes] long."

Also check `user_notes` for duration hints (e.g. "1 hour", "15 min"). Default to 25 minutes if nothing specified.

**Important:** Only suggest slots that fall within each attendee's configured Outlook working hours. If no working hours are available in the query window, say so explicitly rather than suggesting off-hours slots.

## Step 4: Build scheduling summary

Parse the WorkIQ response and build a clean summary. Do NOT include agenda items (those stay in coaching_text).

**You MUST output your summary using this EXACT format, including the `<<<SKILL_OUTPUT>>>` and `<<<END_SKILL_OUTPUT>>>` marker lines. These markers are required for the dashboard to capture your output:**

```
<<<SKILL_OUTPUT>>>
Suggested meeting slots:
1. [Day], [Time] - [Time] ([duration]) — all attendees free
2. [Day], [Time] - [Time] ([duration]) — all attendees free
3. [Day], [Time] - [Time] ([duration]) — all attendees free

Duration: [from user_notes hint or 25 min default]
Attendees: [full names from key_people]
<<<END_SKILL_OUTPUT>>>
```

Pick the 3 best slots. Filter to each person's Outlook working hours only — never suggest slots outside configured work schedules. Prefer morning slots and avoid lunch hour (12-1pm). If no overlapping working-hours slots exist in the window, report that clearly and suggest extending the date range.

Your output will be automatically saved to the dashboard. No further action needed.
