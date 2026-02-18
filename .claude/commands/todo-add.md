---
description: Add a task to TodoNess with natural language parsing
---

Add a new task to TodoNess by parsing natural language input with Claude's reasoning.

**Input:** $ARGUMENTS

Today's date is $CURRENT_DATE.

## Step 1: Reason about the input

Read the natural language text and infer ALL of the following:

- **title**: Clean, concise task title in imperative form
- **description**: Fuller description of what the task involves, including implied sub-steps
- **priority**: Integer 1-5 (1=urgent/ASAP/critical, 2=important/soon, 3=normal, 4=low, 5=information/FYI)
- **due_date**: ISO date (YYYY-MM-DD) resolved from any time references relative to today. null if none implied.
- **key_people**: A JSON array of resolved people (see format below)
- **source_type**: One of 'email', 'meeting', 'chat', 'manual'. Infer from the action described (e.g. "reply to the email from John" → 'email', "schedule a meeting" → 'meeting'). Default to 'manual' for general tasks.
- **related_meeting**: Meeting description if one is referenced, null otherwise.
- **action_type**: Classify the task into one of these action types based on intent:

  | action_type | Infer when... |
  |---|---|
  | `schedule-meeting` | scheduling, finding time, setting up a meeting |
  | `respond-email` | replying to, responding to, drafting an email |
  | `review-document` | reviewing, reading, giving feedback on a doc/PR/report |
  | `follow-up` | checking in, nudging, getting a status update |
  | `prepare` | preparing for a meeting, presentation, demo |
  | `general` | default fallback |

- **coaching_text**: A brief, actionable coaching tip for how to approach this task effectively. Tailor by action_type (see todo-parse Step 3b for coaching guidance per type).

## Step 2: Resolve people and meetings via WorkIQ

For each person mentioned, call `ask_work_iq` with: "Who is [name]? Give me the top 3-4 most likely matches with full name, email, and role."
Pick the best match and store alternatives as JSON:
```json
[{"name": "John Wheat", "email": "john.wheat@contoso.com", "role": "PM",
  "alternatives": [
    {"name": "John Smith", "email": "john.smith@contoso.com", "role": "Engineer"},
    {"name": "John Adams", "email": "john.adams@contoso.com", "role": "Designer"}
  ]}]
```
Store as a JSON string in the `key_people` column. If WorkIQ can't resolve, store `[{"name": "John", "alternatives": []}]`.

If a meeting is referenced, call `ask_work_iq` with: "What meetings do I have related to [topic]?"
Use the response to set related_meeting and refine due_date if relevant.

## Step 2b: Auto-invoke action-specific enrichment

After resolving people and generating coaching_text, auto-generate `skill_output` based on `action_type`. If `key_people` is empty, skip and set `skill_output` to null.

Build a name list from `key_people` JSON for all queries below.

| action_type | WorkIQ query | skill_output format |
|---|---|---|
| `schedule-meeting` | "Shared calendar availability for [names] [this week / by due_date]. Treat tentative as available. Only show slots during each person's Outlook working hours. Show free slots >= 30 min." | `Suggested meeting slots:\n1. [Day], [Time]-[Time] ([dur])\n...\nDuration: [from input or 30 min]\nAttendees: [names]` |
| `respond-email` | "Recent email thread about [topic] with [names]. Show last 2-3 messages." | `To: [name] <[email]>\nSubject: Re: [topic]\n\n[Draft body]\n---\nTone: [inferred]\nKey points: [bullets]` |
| `follow-up` | "Most recent emails and Teams messages with [names] about [topic]. When was the last interaction?" | `Channel: [Email/Teams based on source_type]\nTo: [name]\n\n[Draft follow-up]\n---\nLast interaction: [date]\nUrgency: [from due_date]` |
| `prepare` | "Agenda and attendees for [related_meeting]. Recent docs related to [topic]." | `Prep Notes: [event]\nBefore:\n[ ] [item]\nTalking points:\n- [point]\nMaterials:\n- [doc]\nTime estimate: [X min]` |
| `general`, `review-document` | No auto-enrichment | `skill_output` = null |

**Guidelines:**
- Check input text for constraints, tone, specific asks — weave into the output
- For `follow-up` with `source_type = 'chat'`: draft as a Teams message (casual tone)
- Store result as `skill_output` (included in the INSERT below)

If the action_type doesn't qualify or key_people is empty, set `skill_output` to null.

## Step 3: Write the task to SQLite

```python
import sqlite3
from datetime import datetime, timezone

conn = sqlite3.connect('$PROJECT_ROOT/data/claudetodo.db')
now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
conn.execute(
    """INSERT INTO tasks (title, description, status, parse_status, raw_input,
       priority, due_date, source_type, key_people, related_meeting,
       coaching_text, action_type, skill_output, created_at, updated_at)
       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
    (title, description, 'active', 'parsed', '$ARGUMENTS',
     priority, due_date, source_type, key_people, related_meeting,
     coaching_text, action_type, skill_output, now, now)
)
conn.commit()
task_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
conn.close()
```

## Step 4: Display the result

Show the created task with all enriched fields:
- Task ID and title
- Priority (P1-P5) and due date
- Key people (with resolved names if WorkIQ was used)
- Action type (with icon)
- Coaching tip
- Skill output (if generated — e.g. scheduling slots for schedule-meeting)
- Source type
