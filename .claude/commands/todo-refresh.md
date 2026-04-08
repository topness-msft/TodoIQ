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

## Step 2a: WorkIQ scan (Teams + Meetings)

Call `ask_work_iq` with ONE query for Teams messages and meeting action items. WorkIQ returns **structured task suggestions** with resolved names, descriptions, and action types — so Claude does NOT need to interpret raw text.

> **Note:** Broad (unflagged) email scanning is disabled — WorkIQ cannot reliably scope unflagged emails to Inbox. Flagged inbox emails are handled separately in Step 2c below.

```
What Teams messages and meeting action items need my attention or action? Include: (1) Teams messages from the last {days_since} days directed at me by name or @mentioning me that I haven't responded to, (2) action items from meetings in the last {days_since} days assigned to me or that I committed to. For each item, return it as a structured task suggestion with ALL of these fields: 1. **Task title**: A clean imperative action describing WHAT I NEED TO DO (e.g. "Schedule workshop walkthrough with Alex"). Not the message topic — describe the action. 2. **Description**: 2-3 sentences of context: what was the original ask, current state, what specifically needs to happen next. 3. **Source type**: teams or meeting. 4. **Key people**: For each person involved, give their FULL resolved name and email address (e.g. "Jane Doe, jane.doe@contoso.com"). Resolve aliases and short names to full directory names. 5. **Priority**: P1 (urgent/deadline today), P2 (time-sensitive), P3 (normal), P4 (low/FYI). 6. **Original subject or topic**: The root subject (strip Re:/Fwd: prefixes). 7. **Date**: When the item was sent/occurred. 8. **Action type**: One of: respond-email, follow-up, schedule-meeting, prepare, general. Format each item as a numbered task with clear field labels.
```

## Step 2b: WorkIQ scan (Awaiting Response)

Call `ask_work_iq` with a separate query focused on outbound messages where I'm waiting for a reply. Use `awaiting-response` as the action_type.

```
What messages or emails have I SENT in the last {days_since} days that contain a question, request, or ask where the recipient hasn't responded yet? Only include items where I am clearly waiting for a response — not messages I sent that were purely informational. For each item, return it as a structured task suggestion with ALL of these fields: 1. **Task title**: A clean imperative action (e.g. "Follow up with Alex on budget approval"). 2. **Description**: 2-3 sentences: what I asked, who I'm waiting on, when I sent it. 3. **Source type**: email, teams, or meeting. 4. **Key people**: For each person involved, give their FULL resolved name and email address. 5. **Priority**: P3 (normal) or P4 (low) — these are lower urgency since I'm waiting, not being asked. 6. **Original subject or topic**: The root subject (strip Re:/Fwd: prefixes). 7. **Date**: When I sent the message. 8. **Action type**: awaiting-response. Format each item as a numbered task with clear field labels.
```

## Step 2c: WorkIQ scan (Flagged Inbox Emails)

Call `ask_work_iq` for flagged emails in the Inbox only. Unlike Steps 2a/2b, this query has **no time window** — a flagged email represents explicit user intent regardless of age.

```
Show me only flagged emails in my Inbox folder. Do not include emails from Archive, Deleted Items, Sent Items, or any other folder — only the Inbox. For each item, return it as a structured task suggestion with ALL of these fields: 1. **Task title**: A clean imperative action describing WHAT I NEED TO DO (e.g. "Reply to Sarah's budget proposal", "Schedule workshop walkthrough with Steve"). Not the message subject — describe the action. 2. **Description**: 2-3 sentences of context: what was the original ask, current state, what specifically needs to happen next. 3. **Source type**: email. 4. **Key people**: For each person involved, give their FULL resolved name and email address (e.g. "Jane Doe, jane.doe@contoso.com"). Resolve aliases and short names to full directory names. 5. **Priority**: P1 (urgent/deadline today), P2 (time-sensitive), P3 (normal), P4 (low/FYI). 6. **Original subject or topic**: The root subject (strip Re:/Fwd: prefixes). 7. **Date**: When the item was sent/occurred. 8. **Action type**: One of: respond-email, follow-up, awaiting-response, schedule-meeting, prepare, general. Format each item as a numbered task with clear field labels.
```

If WorkIQ returns no flagged emails, log "No flagged inbox emails found" and continue.

## Step 3: Validate and extract fields

### Step 3a: Relevance validation — 3-tier priority (Claude)

For **each item** WorkIQ returned, classify into one of three tiers:

| Tier | Description | Priority treatment |
|------|-------------|-------------------|
| **Direct** | Someone is asking ME specifically to do something | Keep WorkIQ's priority as-is |
| **Group** | Assigned to a group/role I belong to (e.g. "Coaches", "AI team") | Downgrade by 1 level (P1→P2, P2→P3, etc., max P4) |
| **Tangential** | I'm mentioned as context, CC'd, or action is for someone else | Set to P5 (Information) |

Then apply these additional filters:

1. **"Is this stale or concluded?"** — Does the conversation appear finished (I already replied, the thread moved on, the message was deleted)? If so, skip entirely.
2. **"Is this automated noise?"** — Is this a confirmation, receipt, notification, or noreply email with no genuine action required? If so, skip entirely.

Outcomes:
- **Direct + actionable** → keep WorkIQ's priority
- **Group + actionable** → downgrade priority by 1 level (min P4)
- **Tangential / not clearly mine** → set to **P5** (Information)
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
| **action_type** | WorkIQ `Action type` | Use as-is (respond-email, follow-up, awaiting-response, schedule-meeting, prepare, general) |
| **source_snippet** | WorkIQ `Description` | Same as description — the contextual summary |
| **source_url** | WorkIQ link references | Extract from markdown links in WorkIQ response if available, otherwise null |

**Claude generates** (not from WorkIQ):
- **source_id**: Composite dedup key from the original subject + first key person's email: `{source_type}::{first_person_email_lower}::{root_subject_first_50_lower}` (strip Re:/Fwd: prefixes; do NOT include date)

### Fuzzy title matching helper

Use this function throughout dedup to catch paraphrased duplicates (e.g. "Send EBC participant list for April 21" vs "Send EBC participant names for April 21 session"):

```python
def title_tokens(t):
    """Extract meaningful tokens from a title, lowercased, stopwords removed."""
    stop = {'a','an','the','to','for','of','on','in','at','and','or','with','my','re','fwd'}
    return set(w for w in t.lower().split() if w not in stop and len(w) > 1)

def fuzzy_title_match(t1, t2, threshold=0.5):
    """Token-overlap (Jaccard) match. Returns True if similarity >= threshold."""
    s1, s2 = title_tokens(t1), title_tokens(t2)
    if not s1 or not s2:
        return False
    return len(s1 & s2) / len(s1 | s2) >= threshold
```

### In-batch dedup (before DB checks)

Steps 2a, 2b, and 2c can return overlapping items (e.g. a Teams message appears as both "needs attention" and "awaiting response", or a flagged email duplicates a meeting action item). Before any DB interaction, deduplicate within the collected batch itself:

1. Collect ALL extracted items from Steps 2a, 2b, and 2c into a single list.
2. Group by `source_id`. If two items share the same `source_id`, keep the one with the higher priority (lower number). If equal, keep the first one encountered.
3. For each remaining pair of items, if `fuzzy_title_match(title_a, title_b)` returns True, they are the same item worded differently. Keep the one with higher priority.
4. Log how many in-batch duplicates were removed (add to `skipped` count).

Only the deduplicated list proceeds to the DB dedup checks below.

### Dedup check (two-pass: exact/fuzzy then semantic)

**Pass 1 — Exact + fuzzy match** on source_id, title prefix, or token overlap:

```python
import sqlite3

conn = sqlite3.connect('$PROJECT_ROOT/data/claudetodo.db')
conn.row_factory = sqlite3.Row

# First: exact match on source_id or title prefix
existing = conn.execute(
    "SELECT id, status, source_id, title, source_snippet FROM tasks WHERE source_id = ? OR LOWER(SUBSTR(title, 1, 40)) = LOWER(SUBSTR(?, 1, 40))",
    (source_id, title)
).fetchall()

# If no exact match, try fuzzy token-overlap against same-sender tasks
if not existing:
    sender_lower = first_person_email.strip().lower()
    sender_prefix = sender_lower.split('@')[0]
    candidates = conn.execute(
        "SELECT id, status, source_id, title, source_snippet FROM tasks WHERE source_id LIKE ?",
        ('%::' + sender_prefix + '%',)
    ).fetchall()
    existing = [c for c in candidates if fuzzy_title_match(title, c['title'])]

conn.close()
```

**Pass 2 — Semantic match** (if no exact match found):

Query existing tasks from the same key person to check for semantic duplicates:

```python
conn = sqlite3.connect('$PROJECT_ROOT/data/claudetodo.db')
conn.row_factory = sqlite3.Row
# Normalize sender: match both alias forms (e.g. jane.doe@ and jdoe@)
sender_lower = first_person_email.strip().lower()
sender_prefix = sender_lower.split('@')[0]
same_sender_tasks = conn.execute(
    "SELECT id, status, source_id, title, source_snippet, action_type FROM tasks WHERE source_id LIKE ?",
    ('%::' + sender_prefix + '%',)
).fetchall()
conn.close()
```

For each `same_sender_task`, decide if it's a semantic duplicate of the new item. Two tasks are duplicates if they involve **the same person AND the same underlying conversation, project, or topic** — even if:
- The titles use different wording (e.g. "Advise X on Y" vs. "Follow up with X about Y")
- The action types differ (e.g. one is `follow-up` and the other is `awaiting-response`)
- One came from Step 2a and the other from Step 2b
- The source_id subjects are paraphrased differently

**Match criteria** — a match if ANY of these are true (same sender assumed):
1. Both titles reference the same project, initiative, or topic keywords
2. The descriptions discuss the same conversation thread or meeting
3. One task is the natural follow-up or continuation of the other

**NOT a match** only if the tasks involve genuinely different asks from the same person (e.g. person A asked about budget AND separately about a hiring decision).

Be aggressive about dedup — it's better to augment an existing task than to create a near-duplicate. **When in doubt, it's a match.**

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
