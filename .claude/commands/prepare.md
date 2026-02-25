---
description: Build preparation notes for meetings, presentations, or demos
---

Build a structured preparation checklist for an upcoming meeting, presentation, or event.

**Input:** $ARGUMENTS (task ID — **required**)

Today's date is $CURRENT_DATE.

## Step 0: Validate input

If `$ARGUMENTS` is empty or not a valid integer, stop immediately with:
> **Usage:** `/prepare <task_id>`
>
> Example: `/prepare 12`

## Step 1: Read the task from SQLite

```python
import sqlite3

conn = sqlite3.connect('$PROJECT_ROOT/data/claudetodo.db')
conn.row_factory = sqlite3.Row
task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
conn.close()
```

If the task doesn't exist, stop with: "Task #[id] not found."

Extract: `key_people`, `user_notes`, `description`, `title`, `due_date`, `related_meeting`, `source_snippet`.

## Step 2: Gather meeting and document context from WorkIQ

Query WorkIQ to understand what you're preparing for:

1. If `related_meeting` is set: "What is the agenda and attendee list for [related_meeting]? What was discussed in previous instances of this meeting?"
2. "What recent documents, presentations, or files have I worked on related to [topic from title/description]?"
3. If key_people exist: "What topics has [key_people names] been focused on recently that relate to [topic]?"

This helps build a preparation plan that's informed by actual meeting context and attendee expectations.

## Step 3: Build preparation notes

Based on context, build a structured prep checklist:

**Format:**
```
Preparation Notes: [meeting/event name]
Date: [due_date or meeting date if known]
Attendees: [key_people names and roles]

Before the meeting:
[ ] [Prep item 1 — e.g. "Review the latest Spark Tank deck"]
[ ] [Prep item 2 — e.g. "Pull usage metrics from last workshop"]
[ ] [Prep item 3]

Key talking points:
- [Point 1 — informed by recent context]
- [Point 2]
- [Point 3]

Materials to bring/share:
- [Document/link 1]
- [Document/link 2]

Questions to ask:
- [Question informed by recent discussions]
- [Question about open items]

Time estimate: [X minutes of prep needed]
```

**Guidelines:**
- Make prep items concrete and actionable (not vague "prepare slides" — say "update slide 4 with Q1 numbers")
- If `user_notes` contain specific topics or constraints, prioritize those
- Reference actual documents/meetings found via WorkIQ when possible
- Estimate realistic prep time based on scope
- If the meeting is recurring, note what changed since last time
- Order prep items by priority — most important first

## Step 4: Write to skill_output

Capture the **full output from Step 3** into a Python variable called `skill_output`, then write it to the database:

```python
import sqlite3
from datetime import datetime, timezone

# skill_output must contain the complete prep notes you composed in Step 3.
# Assign it as a triple-quoted string with the EXACT text you drafted above.
skill_output = """<PASTE YOUR FULL PREP NOTES HERE>"""

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
```

**Important:** Write to `skill_output`, NOT `coaching_text`. The `skill_output` variable MUST contain the prep notes — do not leave it empty or undefined.

## Step 5: Display results

Show the preparation notes and note:
> "Prep notes saved to task #[id]. Check off items as you go."
> "View in dashboard: http://localhost:8766"
