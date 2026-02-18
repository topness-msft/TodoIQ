---
description: Full M365 scan — single WorkIQ call surfaces actionable items as suggested tasks
---

Perform a comprehensive M365 scan via WorkIQ to surface actionable items as suggested tasks.

Today's date is $CURRENT_DATE.

## Step 0: Clear sync request marker

```python
from pathlib import Path
p = Path('$PROJECT_ROOT/data') / '.sync_requested'
if p.exists():
    p.unlink()
```

## Step 1: Determine sync window

```python
import sqlite3
from datetime import datetime, timezone, timedelta

conn = sqlite3.connect('$PROJECT_ROOT/data/claudetodo.db')
conn.row_factory = sqlite3.Row
last_sync = conn.execute(
    "SELECT synced_at FROM sync_log WHERE sync_type IN ('full_scan', 'flagged_emails') ORDER BY synced_at DESC LIMIT 1"
).fetchone()
conn.close()

now = datetime.now(timezone.utc)
if last_sync and last_sync['synced_at']:
    last_dt = datetime.fromisoformat(last_sync['synced_at'].replace('Z', '+00:00'))
    days_since = max(1, (now - last_dt).days)  # floor at 1 for overlap safety
    if days_since > 7:
        days_since = 7  # cap at 7 days
else:
    days_since = 7  # first run default
```

Report the sync window: "Scanning the last {days_since} day(s)..."

## Step 2: WorkIQ scan (two passes)

Call `ask_work_iq` **twice** — once for email, once for Teams + meetings. This avoids context limits that caused emails to drop when combined into one query. WorkIQ returns **structured task suggestions** with resolved names, descriptions, and action types — so Claude does NOT need to interpret raw text.

### Pass 2a: Email scan

```
What emails in my Inbox need my attention or action? ONLY search my Inbox folder (not Sent, Archive, or other folders). Include: (1) ALL emails currently flagged in my Inbox (no time limit — include every flagged email), (2) emails in my Inbox from the last {days_since} days where I am on the To line (not just CC or BCC) that ask me specifically for a response or action and I haven't replied yet. Exclude emails sent to distribution lists or broad groups unless I am specifically called out by name in the body. For each item, return it as a structured task suggestion with ALL of these fields: 1. **Task title**: A clean imperative action describing WHAT I NEED TO DO (e.g. "Reply to Sarah's budget proposal"). Not the email subject — describe the action. 2. **Description**: 2-3 sentences of context: what was the original ask, current state, what specifically needs to happen next. 3. **Source type**: email. 4. **Key people**: For each person involved, give their FULL resolved name and email address (e.g. "Phil Topness, phil.topness@microsoft.com"). Resolve aliases and short names to full directory names. 5. **Priority**: P1 (urgent/deadline today), P2 (time-sensitive), P3 (normal), P4 (low/FYI). 6. **Original subject or topic**: The root subject (strip Re:/Fwd: prefixes). 7. **Date**: When the item was sent/occurred. 8. **Action type**: One of: respond-email, follow-up, general. Format each item as a numbered task with clear field labels.
```

### Pass 2b: Teams + Meetings scan

```
What Teams messages and meeting action items need my attention or action? Include: (1) Teams messages from the last 3 days directed at me by name or @mentioning me that I haven't responded to, (2) action items from meetings in the last 3 days assigned to me or that I committed to, (3) Teams messages I SENT in the last {days_since} days that contain a question or request where the recipient hasn't responded yet. For each item, return it as a structured task suggestion with ALL of these fields: 1. **Task title**: A clean imperative action describing WHAT I NEED TO DO (e.g. "Schedule workshop walkthrough with Steve"). Not the message topic — describe the action. 2. **Description**: 2-3 sentences of context: what was the original ask, current state, what specifically needs to happen next. 3. **Source type**: teams or meeting. 4. **Key people**: For each person involved, give their FULL resolved name and email address (e.g. "Phil Topness, phil.topness@microsoft.com"). Resolve aliases and short names to full directory names. 5. **Priority**: P1 (urgent/deadline today), P2 (time-sensitive), P3 (normal), P4 (low/FYI). 6. **Original subject or topic**: The root subject (strip Re:/Fwd: prefixes). 7. **Date**: When the item was sent/occurred. 8. **Action type**: One of: respond-email, follow-up, schedule-meeting, prepare, general. Format each item as a numbered task with clear field labels.
```

Combine results from both passes before proceeding to Step 3.

## Step 3: Validate and extract fields

### Step 3a: Relevance validation (Claude)

For **each item** WorkIQ returned, assess whether it's genuinely actionable by me (Phil Topness):

1. **"Is the action mine?"** — Am I the person being asked to do something, decide, respond, or follow up? Or am I merely mentioned as context, CC'd, or is the action for someone else?
2. **"Is this stale or concluded?"** — Does the conversation appear finished (I already replied, the thread moved on, the message was deleted)? If so, skip entirely.
3. **"Is this automated noise?"** — Is this a confirmation, receipt, notification, or noreply email with no genuine action required? If so, skip entirely.

Outcomes:
- **Genuinely mine + actionable** → keep the priority WorkIQ assigned
- **Not clearly mine / just mentioned** → downgrade to **P5** (Information)
- **Stale / concluded / automated noise** → **skip** (do not create task)

### Step 3b: Extract fields from WorkIQ's structured response

WorkIQ returns task suggestions with most fields already populated. For **each item**, extract directly from WorkIQ's response:

| Field | Source | Notes |
|-------|--------|-------|
| **title** | WorkIQ `Task title` | Use as-is — already imperative form |
| **description** | WorkIQ `Description` | Use as task description (context + next steps) |
| **source_type** | WorkIQ `Source type` | Map: `email` → `email`, `teams` → `chat`, `meeting` → `meeting` |
| **key_people** | WorkIQ `Key people` | Convert to JSON: `[{"name": "Full Name", "email": "addr@domain.com"}]`. Exclude yourself from the list. |
| **priority** | WorkIQ `Priority` | Map: P1→1, P2→2, P3→3, P4→4. Override to **4** if validation found item is not clearly actionable by me. |
| **action_type** | WorkIQ `Action type` | Use as-is |
| **source_snippet** | WorkIQ `Description` | Same as description — the contextual summary |
| **source_url** | WorkIQ link references | Extract from markdown links in WorkIQ response if available, otherwise null |

**Claude generates** (not from WorkIQ):
- **source_id**: Composite dedup key from the original subject + first key person's email: `{source_type}::{first_person_email_lower}::{root_subject_first_50_lower}` (strip Re:/Fwd: prefixes; do NOT include date)

### Dedup check (two-pass: exact then semantic)

**Pass 1 — Exact match** on source_id or title prefix:

```python
import sqlite3

conn = sqlite3.connect('$PROJECT_ROOT/data/claudetodo.db')
conn.row_factory = sqlite3.Row
existing = conn.execute(
    "SELECT id, status, source_id, title, source_snippet FROM tasks WHERE source_id = ? OR LOWER(SUBSTR(title, 1, 40)) = LOWER(SUBSTR(?, 1, 40))",
    (source_id, title)
).fetchall()
conn.close()
```

**Pass 2 — Semantic match** (if no exact match found):

Query existing tasks from the same key person to check for semantic duplicates:

```python
conn = sqlite3.connect('$PROJECT_ROOT/data/claudetodo.db')
conn.row_factory = sqlite3.Row
# Normalize sender: match both alias forms (e.g. saurabh.pant@ and spant@)
sender_lower = first_person_email.strip().lower()
sender_prefix = sender_lower.split('@')[0]
same_sender_tasks = conn.execute(
    "SELECT id, status, source_id, title, source_snippet FROM tasks WHERE source_id LIKE ?",
    ('%::' + sender_prefix + '%',)
).fetchall()
conn.close()
```

For each `same_sender_task`, compare its title and description against the new item. **If they describe the same underlying ask or action** (even with different wording), treat it as a match.

Be aggressive about dedup — it's better to augment an existing task than to create a near-duplicate. When in doubt, it's a match.

**If a match is found**, decide based on status:
- **dismissed** → skip entirely, never re-suggest dismissed items
- **active / in_progress / completed** → **augment**: update `source_snippet` with latest context if meaningfully new (e.g. new deadline, escalation). Update `updated_at`. Increment `updated_count`.
- **suggested** → update `source_snippet` and `priority` if the new item shows increased urgency. Increment `updated_count`.

```python
# Augment existing task with newer context
import sqlite3
from datetime import datetime, timezone

conn = sqlite3.connect('$PROJECT_ROOT/data/claudetodo.db')
now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
conn.execute(
    "UPDATE tasks SET source_snippet = ?, priority = MIN(priority, ?), updated_at = ? WHERE id = ?",
    (new_source_snippet, new_priority, now, existing_task_id)
)
conn.commit()
conn.close()
```

**Note on flagged/categorized emails:** WorkIQ always returns these regardless of age. Normal dedup applies — if already in the DB, augment. But any *newly* flagged email that isn't already in the DB is always created as a suggestion.

If no match found, create the task:

```python
import sqlite3
from datetime import datetime, timezone

conn = sqlite3.connect('$PROJECT_ROOT/data/claudetodo.db')
now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
conn.execute(
    """INSERT INTO tasks (title, description, status, parse_status, priority,
       source_type, source_id, source_snippet, source_url, key_people,
       action_type, coaching_text, created_at, updated_at)
       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
    (title, description, 'suggested', 'parsed', priority,
     source_type, source_id, source_snippet, source_url, key_people,
     action_type, None, now, now)
)
conn.commit()
conn.close()
```

Track counts: `created`, `updated` (augmented existing), `skipped` (dismissed), and counts by source type (`email`, `chat`, `meeting`).

## Step 4: Parse any unparsed tasks

Check for tasks with `parse_status IN ('unparsed', 'queued')` — if any exist, run the same logic as `/todo-parse` to enrich them.

## Step 5: Log the sync

```python
import json
import sqlite3
from datetime import datetime, timezone

summary = json.dumps({
    "email": email_count,
    "chat": chat_count,
    "meeting": meeting_count,
    "created": created_count,
    "updated": updated_count,
    "skipped": skipped_count
})

conn = sqlite3.connect('$PROJECT_ROOT/data/claudetodo.db')
conn.execute(
    "INSERT INTO sync_log (sync_type, result_summary, tasks_created, tasks_updated, synced_at) VALUES (?,?,?,?,?)",
    ('full_scan', summary, created_count, updated_count,
     datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))
)
conn.commit()
conn.close()
```

## Step 6: Show summary

Display a summary table:

```
TodoNess Refresh Complete
──────────────────────────────────────────────────
Source       | Found | Created | Updated | Skipped
──────────────────────────────────────────────────
Email        |   X   |    X    |    X    |    X
Teams/Chat   |   X   |    X    |    X    |    X
Meeting      |   X   |    X    |    X    |    X
──────────────────────────────────────────────────
Total        |   X   |    X    |    X    |    X

Review suggestions in the dashboard and promote tasks you want to work on.
Dashboard: http://localhost:8766
```

If no new items were found, say: "Everything is up to date — no new items found since last sync."
