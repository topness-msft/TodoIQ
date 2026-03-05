---
description: Check for activity on waiting tasks via WorkIQ
---

Check all "waiting" tasks for recent activity from their key people using WorkIQ.

Today's date is $CURRENT_DATE.

## Step 1: Load waiting tasks (including snoozed OOF tasks due for re-check)

Use the Bash tool to run this Python script to get all waiting tasks and snoozed OOF tasks:

```bash
python -c "
import sqlite3, json
conn = sqlite3.connect('data/claudetodo.db')
conn.row_factory = sqlite3.Row
rows = conn.execute(\"\"\"
    SELECT id, title, description, key_people, source_type, source_id, created_at, status, waiting_activity
    FROM tasks
    WHERE status = 'waiting'
       OR (status = 'snoozed'
           AND waiting_activity LIKE '%out_of_office%'
           AND (json_extract(waiting_activity, '$.checked_at') IS NULL
                OR json_extract(waiting_activity, '$.checked_at') < datetime('now', '-20 hours')))
\"\"\").fetchall()
for r in rows:
    print(json.dumps({'id': r['id'], 'title': r['title'], 'key_people': r['key_people'] or '', 'source_type': r['source_type'] or 'manual', 'source_id': r['source_id'] or '', 'created_at': r['created_at'], 'status': r['status'], 'waiting_activity': r['waiting_activity'] or ''}))
conn.close()
"
```

If there are zero tasks, print "No waiting tasks to check." and stop.

## Step 2: Choose who to query and call WorkIQ

For each task, determine the **target person** to check:

1. **Non-manual tasks** (`source_type` is `email`, `chat`, or `meeting`): Extract the originator from `source_id` (format: `type::email::subject` — the email/middle portion identifies who raised it). Use that person's name from `key_people`. This is the person most likely to have responded.
2. **Manual tasks** or if source originator can't be determined: Use the first person in `key_people`.
3. **No key_people**: classify as `no_activity` with summary "No key people to check" and skip the WorkIQ query.

Determine the **query start date**:
- **Manual tasks** (`source_type = 'manual'`): use 2 days before `created_at` — manual tasks are often created after the relevant activity already happened.
- **Non-manual tasks**: use `created_at` as-is — the task was auto-created at the time of the activity.

**First**, check if the target person is out of office. Ask WorkIQ:
> "Check [person]'s current presence and availability status. Are they showing as Out of Office in Teams or Outlook? Do they have an OOO status, automatic reply, or Out of Office presence set? Also check if I've received any recent automatic reply or OOO email from them. If they are OOO, when are they returning?"

**Note:** WorkIQ sometimes misses OOO status with simple queries. The explicit mention of "presence", "Teams", and "Outlook" helps it check the right signals.

**Then**, build the communication query using the **source channel** to focus the search:

- `source_type = 'chat'` → "What are my most recent Teams chats and messages with [person] since [start date]?"
- `source_type = 'email'` → "What are my most recent emails with [person] since [start date]?"
- `source_type = 'meeting'` → "What are my most recent meetings and meeting chats with [person] since [start date]?"
- `source_type = 'manual'` or unknown → "What are my most recent emails, Teams messages, and chats with [person] since [start date]?"

Append to all queries: "List all interactions found."

**IMPORTANT:** Query broadly about ALL communication with the person on that channel — do NOT limit to the specific task topic. WorkIQ may miss relevant responses if the query is too narrow. You will classify relevance yourself in Step 3.

## Step 3: Classify responses

Review the WorkIQ results against the task's title and description. Classify using one of four statuses. **`out_of_office` takes priority** — if the OOO check shows the person has an automatic reply or is OOO, classify as `out_of_office` regardless of any recent communications.

- **`out_of_office`** — person has an automatic reply / OOO set. Summary: describe the OOO message. Include `return_date` (ISO date like "2026-03-10", or null if unknown).
- **`no_activity`** — no messages at all from that person since the task was created. Summary: "No response from [person] since [date]"
- **`activity_detected`** — person has been communicating but not clearly about this task's topic. Summary: describe what was found and note whether it might be related
- **`may_be_resolved`** — person sent a clear response or resolution relevant to this specific task. Summary: brief description of the resolution

When in doubt, prefer `activity_detected` over `no_activity` — any communication is worth surfacing. Prefer `activity_detected` over `may_be_resolved` unless the resolution is obvious.

**WorkIQ errors:** If `ask_work_iq` fails or returns an error for a task, **skip that task entirely** — do NOT write a result for it. This preserves any previous check data. Only write results for tasks where WorkIQ returned a real response.

## Step 4: Write ALL results to SQLite

After checking ALL tasks, use the Bash tool to run a single Python script that writes every result to the database. Build the full script with all task results hardcoded, then execute it.

For `out_of_office` results, include `return_date` in the JSON (ISO date string or null).

For **snoozed OOF tasks** (tasks that came from the expanded query with `status = 'snoozed'`): if the re-check shows the person is **no longer OOO** (i.e. not classified as `out_of_office`), **auto-unsnooze** them — set `status = 'waiting'`, clear `snoozed_until`, and write the new classification.

```bash
python -c "
import sqlite3, json
from datetime import datetime, timezone
conn = sqlite3.connect('data/claudetodo.db')
now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
results = [
    (TASK_ID, 'CLASSIFICATION', 'SUMMARY', RETURN_DATE_OR_NONE, ORIGINAL_TASK_STATUS),
    ...
]
for task_id, classification, summary, return_date, orig_status in results:
    activity = {'status': classification, 'summary': summary, 'checked_at': now}
    if return_date:
        activity['return_date'] = return_date
    val = json.dumps(activity)
    if orig_status == 'snoozed' and classification != 'out_of_office':
        # Auto-unsnooze: person is back, move to waiting
        conn.execute('UPDATE tasks SET waiting_activity = ?, status = ?, snoozed_until = NULL, updated_at = ? WHERE id = ?', (val, 'waiting', now, task_id))
        print('Task ' + str(task_id) + ': auto-unsnoozed (person no longer OOO)')
    else:
        conn.execute('UPDATE tasks SET waiting_activity = ?, updated_at = ? WHERE id = ?', (val, now, task_id))
conn.commit()
conn.close()
print('Updated ' + str(len(results)) + ' tasks')
"
```

Replace TASK_ID, CLASSIFICATION, SUMMARY, RETURN_DATE_OR_NONE, ORIGINAL_TASK_STATUS with actual values. Use `None` for return_date if unknown. Use the task's original `status` field from Step 1.

## Step 5: Print summary

**You MUST print your results using this EXACT format with markers:**

<<<SKILL_OUTPUT>>>
Waiting Activity Check — [date]
Checked [N] tasks

#[id] [title] — [status]: [summary]
...
<<<END_SKILL_OUTPUT>>>
