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

Build a comma-separated list of full names from the key_people array (e.g. "Pratap Ladhani and John Wheat").

## Step 3: Query WorkIQ for shared calendar availability

Build a single multi-person query to `ask_work_iq`:

- If `due_date` is set: "What is the shared calendar availability for [Person1], [Person2], and [Person3] between now and [due_date]? Treat tentative calendar blocks as available. Only show slots during each person's Outlook working hours. Show free time slots that are at least 30 minutes long."
- If no `due_date`: "What is the shared calendar availability for [Person1], [Person2], and [Person3] this week? Treat tentative calendar blocks as available. Only show slots during each person's Outlook working hours. Show free time slots that are at least 30 minutes long."

Also check `user_notes` for duration hints (e.g. "1 hour", "15 min"). Default to 30 minutes if nothing specified.

**Important:** Only suggest slots that fall within each attendee's configured Outlook working hours. If no working hours are available in the query window, say so explicitly rather than suggesting off-hours slots.

## Step 4: Build scheduling summary

Parse the WorkIQ response and build a clean summary. Do NOT include agenda items (those stay in coaching_text).

Format:
```
Suggested meeting slots:
1. [Day], [Time] - [Time] ([duration]) — all attendees free
2. [Day], [Time] - [Time] ([duration]) — all attendees free
3. [Day], [Time] - [Time] ([duration]) — all attendees free

Duration: [from user_notes hint or 30 min default]
Attendees: [full names from key_people]
```

Pick the 3 best slots. Filter to each person's Outlook working hours only — never suggest slots outside configured work schedules. Prefer morning slots and avoid lunch hour (12-1pm). If no overlapping working-hours slots exist in the window, report that clearly and suggest extending the date range.

## Step 5: Write to skill_output — MANDATORY, DO NOT SKIP

**You MUST execute this step immediately after drafting. Do NOT ask for confirmation. Do NOT present options. Just run the code.**

This runs in a non-interactive `claude -p` session — there is no user to respond. Execute the Bash tool with this Python code now:

```python
import sqlite3
from datetime import datetime, timezone

# skill_output must contain the complete scheduling suggestions you composed in Step 4.
# Assign it as a triple-quoted string with the EXACT text you drafted above.
skill_output = """<PASTE YOUR FULL SCHEDULING OUTPUT HERE>"""

task_id = $ARGUMENTS

conn = sqlite3.connect('$PROJECT_ROOT/data/claudetodo.db')
now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
conn.execute(
    """UPDATE tasks
       SET skill_output = ?, suggestion_refreshed_at = ?, updated_at = ?
       WHERE id = ?""",
    (skill_output, now, now, task_id)
)
conn.commit()
conn.close()
print(f"skill_output written to task #{task_id}")
```

**Critical rules:**
- Execute this code via Bash immediately — do NOT ask "Would you like me to save this?"
- Write to `skill_output`, NOT `coaching_text`
- The `skill_output` variable MUST contain the scheduling text — do not leave it empty or undefined
- Agenda and coaching advice stay in coaching_text
- If you do not execute this code, the dashboard will show no output

## Step 6: Display results

Show the scheduling summary and confirm the DB write succeeded:
> "Scheduling suggestions saved to task #[id]. You can send the invite from Outlook."
