---
description: Check suggested tasks for progress — detect if they're resolved or still need action
---

Check all "suggested" tasks for recent activity to help the user decide whether to accept or dismiss them.

Today's date is $CURRENT_DATE.

**IMPORTANT:** Call `ask_work_iq` directly to query M365 data. Do NOT use shell commands (`workiq ask ...`) or nested `copilot -p` calls — those will fail.

## Step 1: Load suggested tasks

**Single-task mode:** If `$ARGUMENTS` is a positive integer, treat it as a target task ID and only check that one task. Use this Python script — it loads exactly one task regardless of status:

```bash
python -c "
import sqlite3, json
conn = sqlite3.connect('data/claudetodo.db')
conn.row_factory = sqlite3.Row
rows = conn.execute(\"\"\"
    SELECT id, title, description, key_people, source_type, source_id, created_at, waiting_activity, user_notes
    FROM tasks
    WHERE id = TARGET_ID
\"\"\").fetchall()
for r in rows:
    print(json.dumps({'id': r['id'], 'title': r['title'], 'description': r['description'] or '', 'key_people': r['key_people'] or '', 'source_type': r['source_type'] or 'manual', 'source_id': r['source_id'] or '', 'created_at': r['created_at'], 'waiting_activity': r['waiting_activity'] or '', 'user_notes': r['user_notes'] or ''}))
conn.close()
"
```

Replace `TARGET_ID` with the integer from `$ARGUMENTS`. If the task doesn't exist, print "Task not found." and stop.

**Bulk mode (default, when `$ARGUMENTS` is empty):** Use the Bash tool to run this Python script to get all suggested tasks, prioritizing unchecked ones first:

```bash
python -c "
import sqlite3, json
conn = sqlite3.connect('data/claudetodo.db')
conn.row_factory = sqlite3.Row
rows = conn.execute(\"\"\"
    SELECT id, title, description, key_people, source_type, source_id, created_at, waiting_activity, user_notes
    FROM tasks
    WHERE status = 'suggested'
    ORDER BY CASE WHEN waiting_activity IS NULL THEN 0 ELSE 1 END, created_at DESC
\"\"\").fetchall()
for r in rows:
    print(json.dumps({'id': r['id'], 'title': r['title'], 'description': r['description'] or '', 'key_people': r['key_people'] or '', 'source_type': r['source_type'] or 'manual', 'source_id': r['source_id'] or '', 'created_at': r['created_at'], 'waiting_activity': r['waiting_activity'] or '', 'user_notes': r['user_notes'] or ''}))
conn.close()
"
```

If there are zero tasks, print "No suggested tasks to check." and stop.

## Step 2: Process each task — query, classify, and write immediately

For each task, perform these steps **one at a time**, writing the result to the database immediately after each task is checked. This ensures partial progress is saved if the process is interrupted.

### 2.0: WorkIQ connection check (do this ONCE, before the first task)

The WorkIQ MCP server can take a few seconds to connect on a cold start, especially in headless `copilot -p` runs. A "tool not available / not connected" at session start usually means the MCP handshake hadn't finished yet — **not** that WorkIQ is down.

Before processing any tasks, make a tiny probe call to `ask_work_iq` (e.g. ask "What is the subject of my most recent email?"):

1. If it succeeds, proceed to process tasks normally.
2. If it fails with a connection/availability error, **wait ~20 seconds and retry the probe once.**
3. If the retry ALSO fails, **abort without touching any tasks**: do NOT write `waiting_activity`, do NOT refresh `checked_at`, do NOT stamp `unclear` on anything. Leave every task exactly as it was so it auto-retries on the next cycle, and print the summary noting WorkIQ was unreachable. A connection-level outage must never overwrite good data or stamp cards with a misleading fresh timestamp.

Only begin per-task processing (Step 2a onward) once the probe has actually returned data.

### 2a: Determine the target person

1. **Non-manual tasks** (`source_type` is `email`, `chat`, or `meeting`): Extract the originator from `source_id` (format: `type::email::subject` — the email/middle portion identifies who raised it). Use that person's name from `key_people`.
2. **Manual tasks** or if source originator can't be determined: Use the first person in `key_people`.
3. **No key_people**: classify as `unclear` with summary "No key people to check" and skip the WorkIQ query.

### 2b: Query WorkIQ

Determine the **query start date**: use `created_at` as the start date — we want to know what happened since the suggestion was created.

Call `ask_work_iq` to query for ALL recent communication with the person across every channel. Do NOT use shell commands, nested `copilot -p` calls, or `workiq ask` CLI — call the `ask_work_iq` tool directly.

Ask WorkIQ:

> "What are my most recent emails, Teams messages, and chats with [person] about [task topic from title] since [start date]? Was this topic resolved, addressed, or is it still pending? List all interactions found."

**IMPORTANT:** Always query all channels regardless of `source_type` — responses can come on any channel.

#### @WorkIQ inline questions

Check the task's `user_notes` for unanswered `@WorkIQ` questions. A line contains an `@WorkIQ` question if it includes `@WorkIQ` (case-insensitive). A question is **unanswered** if the line immediately following it does NOT start with `  →` (two spaces then →).

If there are unanswered questions, append them to the WorkIQ query for that task:

> "Additionally, answer these questions from the user's notes: 1) [question text without the @WorkIQ prefix] 2) [next question] ..."

### 2c: Classify the response

Review the WorkIQ results against the task's title and description. Classify using one of three statuses:

- **`likely_resolved`** — clear evidence the topic was addressed, action was taken, or the request was fulfilled. Summary: brief description of the resolution.
- **`still_pending`** — no response yet, or the person acknowledged but hasn't acted. Summary: describe what was found (or "No response from [person] since [date]").
- **`unclear`** — some activity found but can't determine if the task is resolved. Summary: describe what was found.

When in doubt, prefer `unclear` over `likely_resolved` — only classify as resolved when the evidence is clear.

**WorkIQ errors:** If `ask_work_iq` fails for a task *after* the Step 2.0 probe succeeded, distinguish two cases:

- **Query-level error** (timeout, empty result, query-specific error) while WorkIQ is still connected: do NOT skip. Classify the task as `unclear` with summary `"WorkIQ query failed: <brief error>"` (e.g., `"WorkIQ query failed: Request timed out"`) and proceed to Step 2d to write the result with a fresh `checked_at`, so the dashboard reflects the attempt and the user can retry.
- **Connection-level error** ("not connected", tool missing, MCP unavailable) — i.e. WorkIQ dropped mid-run: do NOT stamp the task. Stop processing further tasks and follow the Step 2.0 abort behavior (leave remaining tasks untouched, no fresh timestamps). Never mark a card `unclear: WorkIQ not connected` — a connection outage must not overwrite data or create misleading retry timestamps.

### 2d: Write this task's result to SQLite immediately

After classifying **this one task**, write its result to the database right away:

```bash
python -c "
import sqlite3, json
from datetime import datetime, timezone
conn = sqlite3.connect('data/claudetodo.db')
now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
activity = {'status': 'CLASSIFICATION', 'summary': 'SUMMARY', 'checked_at': now}
conn.execute('UPDATE tasks SET waiting_activity = ?, updated_at = ? WHERE id = ?', (json.dumps(activity), now, TASK_ID))
conn.commit()
conn.close()
print('Updated task #TASK_ID')
"
```

Replace TASK_ID, CLASSIFICATION, SUMMARY with actual values.

If the task had unanswered `@WorkIQ` questions, also write the answers back into `user_notes` in the same step:

```bash
python -c "
import sqlite3
from datetime import datetime, timezone
conn = sqlite3.connect('data/claudetodo.db')
now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
row = conn.execute('SELECT user_notes FROM tasks WHERE id = ?', (TASK_ID,)).fetchone()
if row and row[0]:
    lines = row[0].split('\n')
    new_lines = []
    qa = [('QUESTION_LINE', 'ANSWER'), ...]
    for line in lines:
        new_lines.append(line)
        for q, a in qa:
            if line.strip() == q.strip():
                new_lines.append('  \u2192 ' + a)
                break
    conn.execute('UPDATE tasks SET user_notes = ?, updated_at = ? WHERE id = ?', ('\n'.join(new_lines), now, TASK_ID))
    conn.commit()
conn.close()
"
```

**Then move on to the next task and repeat from Step 2a.**

## Step 3: Print summary

After all tasks are processed (or if time runs out), print a summary of what was checked.

**You MUST print your results using this EXACT format with markers:**

<<<SKILL_OUTPUT>>>
Suggestion Check — [date]
Checked [N] suggested tasks

#[id] [title] — [status]: [summary]
...
<<<END_SKILL_OUTPUT>>>
