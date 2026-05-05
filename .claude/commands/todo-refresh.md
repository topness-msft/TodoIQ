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
What Teams messages and meeting action items need my attention or action? ONLY include items where I am DIRECTLY involved — meaning: (1) 1:1 or small group chats (5 or fewer people) where someone asked me to do something, (2) channel messages that @mention me by name, (3) action items from meetings explicitly assigned to me or that I verbally committed to. Do NOT include: messages from large channels/threads where I was not directly addressed, broadcast announcements, FYI-only posts, or threads where I'm just a participant but not asked to act. For each item, return it as a structured task suggestion with ALL of these fields: 1. **Task title**: A clean imperative action describing WHAT I NEED TO DO (e.g. "Schedule workshop walkthrough with Alex"). Not the message topic — describe the action. 2. **Description**: 2-3 sentences of context: what was the original ask, current state, what specifically needs to happen next. 3. **Source type**: teams or meeting. 4. **Key people**: For each person involved, give their FULL resolved name and PRIMARY email address in first.last@microsoft.com format (resolve aliases like "spant" to the full "saurabh.pant@microsoft.com" via directory lookup). Exclude myself from the list. 5. **Priority**: P1 (urgent/deadline today), P2 (time-sensitive), P3 (normal), P4 (low/FYI). 6. **Original subject or topic**: The root subject (strip Re:/Fwd: prefixes). 7. **Date**: The **original timestamp** of when this specific message/meeting/email was first sent or occurred. NOT the last activity in the thread, NOT the date you found it during this scan, NOT today's date. For Teams messages this is the `createdTime` of the originating message. If you can't determine the original date confidently, return null/empty rather than guessing — do not substitute a recent date. 8. **Action type**: One of: respond-email, follow-up, schedule-meeting, prepare, general. 9. **Audience size**: How many people were in the chat/thread (e.g. "1:1", "group of 3", "channel ~50 members"). Format each item as a numbered task with clear field labels.
```

## Step 2b: WorkIQ scan (Awaiting Response)

Call `ask_work_iq` with a separate query focused on outbound messages where I'm waiting for a reply. Use `awaiting-response` as the action_type.

```
What Teams messages or chats have I SENT in the last {days_since} days that contain a question, request, or ask where the recipient hasn't responded yet? Only include Teams messages — do NOT include emails. Only include items where I am clearly waiting for a response — not messages I sent that were purely informational. For each item, return it as a structured task suggestion with ALL of these fields: 1. **Task title**: A clean imperative action (e.g. "Follow up with Alex on budget approval"). 2. **Description**: 2-3 sentences: what I asked, who I'm waiting on, when I sent it. 3. **Source type**: email, teams, or meeting. 4. **Key people**: For each person involved, give their FULL resolved name and PRIMARY email address in first.last@microsoft.com format (resolve aliases like "spant" to "saurabh.pant@microsoft.com" via directory lookup). 5. **Priority**: P3 (normal) or P4 (low) — these are lower urgency since I'm waiting, not being asked. 6. **Original subject or topic**: The root subject (strip Re:/Fwd: prefixes). 7. **Date**: The **original timestamp** of when I sent the message — NOT the last activity in the thread, NOT today's date, NOT when I found it during this scan. For Teams messages this is the `createdTime` of my original outbound message. If you can't determine the original date confidently, return null/empty rather than guessing. 8. **Action type**: awaiting-response. Format each item as a numbered task with clear field labels.
```

## Step 2c: DISABLED — Email scanning

Email scanning (flagged inbox) is **disabled** as of April 2026. WorkIQ cannot reliably scope email queries to flagged-only items in the Inbox folder — results include unflagged emails from all folders. Skip this step entirely.

## Step 3:Validate and extract fields

### Step 3a: Relevance validation — 3-tier priority (Claude)

For **each item** WorkIQ returned, classify into one of three tiers:

| Tier | Description | Priority treatment |
|------|-------------|-------------------|
| **Direct** | Someone is asking ME specifically to do something | Keep WorkIQ's priority as-is |
| **Group** | Assigned to a group/role I belong to (e.g. "Coaches", "AI team") | Downgrade by 1 level (P1→P2, P2→P3, etc., max P4) |
| **Tangential** | I'm mentioned as context, CC'd, or action is for someone else | Set to P5 (Information) |

Then apply these additional filters:

1. **"Is this stale or concluded?"** — Apply TWO checks:
   - **Conversation state:** Has the conversation finished (I already replied, the thread moved on, the message was deleted)? If so, skip entirely.
   - **Age threshold:** Is the source_date older than the last refresh? Check the `sync_log` table for the most recent `synced_at` — any source_date before that was already available in a prior scan and either wasn't surfaced or was dismissed. Skip these unless there's clear evidence the action is still needed (e.g., an upcoming deadline, a recent follow-up message). Flagged emails are exempt from the age threshold (a flag represents explicit intent regardless of age).
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
| **source_date** | WorkIQ `Date` | ISO 8601 date string (e.g. "2026-04-14") of when the source item was **originally** sent/occurred. WorkIQ has been observed misreporting this for old Teams messages (drift of 30–90 days has been seen). When `source_url` is a Teams chat link with an embedded message-id timestamp, the dedup service automatically reconciles `source_date` against that URL timestamp and prefers the URL when drift exceeds 14 days — see `refresh_dedup._reconcile_source_date_with_url`. |
| **source_url** | WorkIQ link references | Extract from markdown links in WorkIQ response if available, otherwise null |

**Claude generates** (not from WorkIQ):
- **source_id**: Composite dedup key from the original subject + first key person's **primary email** (must be `first.last@microsoft.com` format — resolve aliases via WorkIQ if needed): `{source_type}::{first_person_primary_email_lower}::{root_subject_first_50_lower}` (strip Re:/Fwd: prefixes; do NOT include date)
- **coaching_text**: A brief 1-2 sentence actionable tip tailored to the action_type. Examples:
  - `respond-email` → "Review the thread, then reply with your decision. Keep it concise — 2-3 sentences max."
  - `follow-up` → "Check if there's been any reply since. If not, a brief nudge with a specific ask works best."
  - `schedule-meeting` → "Check your calendar for open slots this week, then propose 2-3 times."
  - `prepare` → "Review the agenda and jot down 2-3 talking points before the meeting."
  - `general` → "Break this down — what's the very first concrete step?"

### Fuzzy title matching helper

Use this function throughout dedup to catch paraphrased duplicates (e.g. "Send EBC participant list for April 21" vs "Send EBC participant names for April 21 session"):

```python
def title_tokens(t):
    """Extract meaningful tokens from a title, lowercased, stopwords removed."""
    stop = {'a','an','the','to','for','of','on','in','at','and','or','with','my',
            're','fwd','follow','up','check','confirm','send','share','provide',
            'schedule','review','respond','reply','draft','prepare','update',
            'get','set','discuss','meeting','email','teams','message','request'}
    return set(w for w in t.lower().split() if w not in stop and len(w) > 1)

def person_tokens(t):
    """Extract likely person name tokens (capitalized words) from a title."""
    return set(w.lower() for w in t.split() if w[0].isupper() and len(w) > 2
               and w not in ('Teams','Email','Meeting','Power','Scale','Agent','CAT',
                             'CAPE','EBC','CAB','Level','Brazil','Kickstarter'))

def fuzzy_title_match(t1, t2, threshold=0.45):
    """Token-overlap (Jaccard) match. Returns True if similarity >= threshold.
    Also returns True if person names overlap AND topic tokens overlap >= 0.3."""
    s1, s2 = title_tokens(t1), title_tokens(t2)
    if not s1 or not s2:
        return False
    jaccard = len(s1 & s2) / len(s1 | s2)
    if jaccard >= threshold:
        return True
    # Fallback: if same person mentioned in both titles and some topic overlap
    p1, p2 = person_tokens(t1), person_tokens(t2)
    if p1 and p2 and p1 & p2 and jaccard >= 0.3:
        return True
    return False
```

### In-batch dedup (before DB checks)

Steps 2a, 2b, and 2c can return overlapping items (e.g. a Teams message appears as both "needs attention" and "awaiting response", or a flagged email duplicates a meeting action item). Before any DB interaction, deduplicate within the collected batch itself:

1. Collect ALL extracted items from Steps 2a, 2b, and 2c into a single list.
2. Group by `source_id`. If two items share the same `source_id`, keep the one with the higher priority (lower number). If equal, keep the first one encountered.
3. For each remaining pair of items, if `fuzzy_title_match(title_a, title_b)` returns True, they are the same item worded differently. Keep the one with higher priority.
4. Log how many in-batch duplicates were removed (add to `skipped` count).

Only the deduplicated list proceeds to the DB dedup checks below.

### Dedup check (three-pass: exact, fuzzy, then semantic)

**Pass 1 — Exact match** on source_id or title prefix:

```python
import sqlite3

conn = sqlite3.connect('$PROJECT_ROOT/data/claudetodo.db')
conn.row_factory = sqlite3.Row

# Exact match on source_id or title prefix
existing = conn.execute(
    "SELECT id, status, source_id, title, source_snippet FROM tasks WHERE source_id = ? OR LOWER(SUBSTR(title, 1, 40)) = LOWER(SUBSTR(?, 1, 40))",
    (source_id, title)
).fetchall()
conn.close()
```

**Pass 2 — Fuzzy match by person** (if no exact match found):

Search for candidates using BOTH source_id patterns AND key_people JSON. This catches cases where the same person appears with different email formats or aliases.

```python
import json

conn = sqlite3.connect('$PROJECT_ROOT/data/claudetodo.db')
conn.row_factory = sqlite3.Row

# Extract all person identifiers for broad candidate search
sender_lower = first_person_email.strip().lower()
sender_prefix = sender_lower.split('@')[0]
# Also extract person's last name for key_people JSON search
sender_parts = sender_prefix.replace('.', ' ').split()
sender_last = sender_parts[-1] if sender_parts else sender_prefix

# Search by source_id pattern OR key_people containing the person's name/email
candidates = conn.execute(
    """SELECT id, status, source_id, title, source_snippet, key_people, action_type
       FROM tasks
       WHERE status NOT IN ('deleted')
       AND (source_id LIKE ? OR source_id LIKE ?
            OR key_people LIKE ? OR key_people LIKE ?)""",
    ('%::' + sender_prefix + '::%',
     '%::' + sender_last + '%',
     '%' + sender_lower + '%',
     '%' + sender_last + '%')
).fetchall()
conn.close()

# Apply fuzzy title match against all candidates
existing = [c for c in candidates if fuzzy_title_match(title, c['title'])]
```

**Pass 3 — Semantic match** (if still no match found):

Use the same candidate set from Pass 2 (already fetched). For each candidate, apply Claude's judgment to detect semantic duplicates.

For each candidate task, decide if it's a semantic duplicate of the new item. Two tasks are duplicates if they involve **the same person AND the same underlying conversation, project, or topic** — even if:
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

**If a match is found** (from any pass), decide based on the matched task's status:
- **dismissed** → skip entirely, never re-suggest dismissed items
- **completed** → skip dedup; create a fresh suggestion. Completed tasks close the loop; re-occurrence is a legitimate new signal.
- **active / in_progress / waiting / snoozed / suggested** → **augment**: source_date = MAX(existing, new), priority = MIN(existing, new), source_snippet replaced with newer context (older preserved as a `task_context` row of type 'dedup').

Use the typed creation/augmentation service which atomically performs the
status-conditional dedup-check + insert/update inside `BEGIN IMMEDIATE` so
parallel refreshes can't race into duplicates:

```python
from src.services.refresh_dedup import create_or_refresh_suggestion

result = create_or_refresh_suggestion(
    title=title,
    description=description,
    source_type=source_type,
    source_id=source_id,
    source_snippet=source_snippet,
    source_date=source_date,
    source_url=source_url,
    key_people=key_people,
    priority=priority,
    action_type=action_type,
    coaching_text=coaching_text,
)
# result.outcome ∈ {'created', 'augmented', 'skipped_dismissed', 'skipped_completed'}
# result.task is the resulting task dict (or matched task for skips)
# result.matched_task_id / result.dedup_reason describe the dedup decision
```

The service handles:
  - exact source_id, fuzzy source_id, and title-similarity dedup checks
  - status-conditional behavior (dismissed/completed skip, live augment)
  - MAX semantics for `source_date` so older signals never clobber newer
  - MIN semantics for `priority` so urgency only ratchets up
  - automatic `task_person` derivation for the canonical person identity layer
  - non-destructive provenance: augmentations append a `task_context` row

**Note on flagged/categorized emails:** WorkIQ always returns these regardless of age. Normal dedup applies — if already in the DB, augment. But any *newly* flagged email that isn't already in the DB is always created as a suggestion.

**Staleness guard:** The `create_task()` function in `src/models.py` also enforces a code-side staleness check — suggested tasks with `source_date` older than 14 days (excluding emails) are auto-downgraded to P5. This is a safety net; the prompt-side filter above should catch most stale items before they reach the DB.

Track counts: `created`, `updated` (`outcome == 'augmented'`), `skipped` (`outcome` starts with `skipped_`), and counts by source type (`email`, `chat`, `meeting`).

## Step 4: Parse any unparsed tasks

Check for tasks with `parse_status IN ('unparsed', 'queued')` — if any exist, run the same logic as `/todo-parse` to enrich them.

## Step 5: Shadow-mode LLM dedup cross-check

After all task creation/augmentation is complete, run the shadow dedup module as a final post-check. This is **SHADOW MODE**: it does NOT delete, merge, dismiss, or change any tasks. It only writes `shadow_dup_of` and `shadow_dup_reason` annotations on newly-created tasks so a human can review potential duplicates via the dashboard. The Pass 1/2/3 dedup above remains the authoritative dedup — shadow dedup is an independent cross-check that uses batched LLM ratification against person-overlapping candidates.

Run the CLI entry point from the project root and capture its JSON stdout. A 120-minute window covers edge cases where this refresh itself took up to ~2 hours:

```python
import json
import subprocess

shadow_proc = subprocess.run(
    ['python', '-m', 'src.services.shadow_dedup', '--since', '120'],
    cwd='$PROJECT_ROOT',
    capture_output=True,
    text=True,
    timeout=600,
)

try:
    shadow_summary = json.loads(shadow_proc.stdout.strip().splitlines()[-1])
except (json.JSONDecodeError, IndexError):
    shadow_summary = {
        "ok": False,
        "error": "failed to parse shadow dedup output",
        "stdout": shadow_proc.stdout[-500:],
        "stderr": shadow_proc.stderr[-500:],
        "returncode": shadow_proc.returncode,
    }
```

`shadow_summary` will look like `{"ok": true, "new_items": 14, "flagged": 6}`. It is attached to the sync_log `result_summary` in Step 6 below. Do not act on `flagged` — those annotations are for human review only.

## Step 6: Log the sync

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
    "skipped": skipped_count,
    "shadow_dedup": shadow_summary
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

## Step 7: Show summary

Display a summary table:

```
TodoIQ Refresh Complete
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
