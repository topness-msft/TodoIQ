---
description: Draft a follow-up message for tasks needing a check-in
---

Draft a follow-up message based on a task's context, last interactions, and due date.

**Input:** $ARGUMENTS (task ID — **required**)

Today's date is $CURRENT_DATE.

## Step 0: Validate input

If `$ARGUMENTS` is empty or not a valid integer, stop immediately with:
> **Usage:** `/follow-up <task_id>`
>
> Example: `/follow-up 15`

## Step 1: Read the task from SQLite

```python
import sqlite3

conn = sqlite3.connect('$PROJECT_ROOT/data/claudetodo.db')
conn.row_factory = sqlite3.Row
task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
conn.close()
```

If the task doesn't exist, stop with: "Task #[id] not found."

Extract: `key_people`, `user_notes`, `description`, `title`, `due_date`, `source_type`, `source_snippet`, `related_meeting`.

## Step 2: Gather interaction history from WorkIQ

Query WorkIQ to understand when you last interacted and what the status was:

1. "What are my most recent emails and Teams messages with [key_people names] about [topic from title/description]? When was the last interaction?"
2. If `related_meeting` is set: "What was the outcome of [related_meeting]? Were there any action items for [key_people names]?"

This helps determine:
- How long it's been since last contact
- What was the last thing discussed
- Whether there are outstanding action items to reference

## Step 3: Draft the follow-up

Based on context, draft a follow-up message. Choose the right channel based on `source_type`:
- `email` → draft as email
- `chat` → draft as Teams message
- `meeting` → draft as email (more formal for meeting follow-ups)
- `manual` → draft as email by default

**Format:**
```
Channel: [Email / Teams]
To: [name] <[email]>
Subject: [if email — e.g. "Following up: [topic]"]

[Draft message]

---
Last interaction: [date/summary if found]
Days since last contact: [N days]
Urgency: [based on due_date proximity]
```

**Guidelines:**
- Reference the last interaction to show continuity ("Following up on our [date] discussion about...")
- Be specific about what you need — a status update, a decision, a deliverable
- If overdue or approaching due_date, add gentle urgency without being pushy
- If `user_notes` contain specific asks, build around those
- Keep it brief — follow-ups should be easy to respond to
- Suggest a quick call if the topic is complex

## Step 4: Write to skill_output — MANDATORY, DO NOT SKIP

**You MUST execute this step immediately after drafting. Do NOT ask for confirmation. Do NOT present options. Just run the code.**

This runs in a non-interactive `claude -p` session — there is no user to respond. Execute the Bash tool with this Python code now:

```python
import sqlite3
from datetime import datetime, timezone

# skill_output must contain the complete draft you composed in Step 3.
# Assign it as a triple-quoted string with the EXACT text you drafted above.
skill_output = """<PASTE YOUR FULL DRAFT HERE — Channel:, To:, Subject:, body, notes>"""

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
- The `skill_output` variable MUST contain the draft text — do not leave it empty or undefined
- If you do not execute this code, the dashboard will show no output

## Step 5: Display results

Show the draft and confirm the DB write succeeded:
> "Follow-up draft saved to task #[id]. Copy into [Email/Teams] to send."
