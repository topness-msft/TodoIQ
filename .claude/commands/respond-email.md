---
description: Draft an email response for email-sourced tasks
---

Draft an email reply based on a task's context, people, and source material.

**Input:** $ARGUMENTS (task ID — **required**)

Today's date is $CURRENT_DATE.

## Step 0: Validate input

If `$ARGUMENTS` is empty or not a valid integer, stop immediately with:
> **Usage:** `/respond-email <task_id>`
>
> Example: `/respond-email 20`

## Step 1: Read the task from SQLite

```python
import sqlite3

conn = sqlite3.connect('$PROJECT_ROOT/data/claudetodo.db')
conn.row_factory = sqlite3.Row
task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
conn.close()
```

If the task doesn't exist, stop with: "Task #[id] not found."

Extract: `key_people`, `user_notes`, `description`, `title`, `source_snippet`, `source_url`, `related_meeting`.

## Step 2: Gather email context from WorkIQ

Build a WorkIQ query to get the relevant email thread. Use the key_people names and task description to find the right context:

1. If `source_snippet` or `source_url` exists, query: "Show me the recent email thread about [topic from title/description] with [key_people names]. Include the last 2-3 messages so I can see what was said."
2. If no source context, query: "What are my recent emails with [key_people names] about [topic from title/description]? Show me the most recent thread."

Also check `user_notes` for any tone, points, or constraints the user wants in the reply.

## Step 3: Draft the email reply

Based on the email context and task details, draft a professional email reply:

**Format:**
```
To: [name] <[email]>
Subject: Re: [inferred or from source]

[Draft body]

---
Tone: [professional/casual/urgent — inferred from context]
Key points addressed:
- [point 1]
- [point 2]
```

**Guidelines:**
- Keep it concise — aim for 3-5 sentences unless the context demands more
- Mirror the tone of the original thread
- If `user_notes` specify points to address, make sure they're all covered
- Include a clear call-to-action or next step at the end
- Use the person's first name in the greeting (e.g. "Hi Mehdi,")
- Do NOT include agenda items or coaching — those stay in coaching_text

## Step 4: Write to skill_output

Capture the **full draft from Step 3** (everything from the "To:" line through the notes) into a Python variable called `skill_output`, then write it to the database:

```python
import sqlite3
from datetime import datetime, timezone

# skill_output must contain the complete draft you composed in Step 3.
# Assign it as a triple-quoted string with the EXACT text you drafted above.
skill_output = """<PASTE YOUR FULL DRAFT HERE — To: line, Subject:, body, notes>"""

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

**Important:** Write to `skill_output`, NOT `coaching_text`. The `skill_output` variable MUST contain the draft text — do not leave it empty or undefined.

## Step 5: Display results

Show the draft email and note:
> "Email draft saved to task #[id]. You can copy this into Outlook to send."
> "Edit the draft in your notes or re-run `/respond-email [id]` after updating user_notes to adjust."
> "View in dashboard: http://localhost:8766"
