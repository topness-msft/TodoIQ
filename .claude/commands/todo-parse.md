---
description: Parse unparsed tasks — Claude reads raw text and enriches with structured fields
---

Parse tasks that were added via the dashboard input bar and need enrichment.

## Step 1: Fetch unparsed tasks

```python
import sqlite3
from datetime import datetime, timezone

conn = sqlite3.connect('$PROJECT_ROOT/data/claudetodo.db')
conn.row_factory = sqlite3.Row
tasks = conn.execute("SELECT id, raw_input, title, description, key_people, action_type, user_notes, source_type, parse_status FROM tasks WHERE parse_status IN ('unparsed', 'queued') AND status NOT IN ('deleted', 'completed')").fetchall()
conn.close()
```

If no unparsed tasks, say "All tasks are already parsed!" and stop.

## Step 2: For each unparsed task, mark as 'parsing'

```python
conn = sqlite3.connect('$PROJECT_ROOT/data/claudetodo.db')
now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
conn.execute("UPDATE tasks SET parse_status = 'parsing', updated_at = ? WHERE id = ?", (now, task_id))
conn.commit()
conn.close()
```

## Step 2b: Check if this is a coaching-only re-parse

A task needs **coaching-only** re-parse if it already has `title`, `description`, and `key_people` populated (i.e. it was previously fully parsed). This happens when the user changes the `action_type` or edits the description from the dashboard.

For coaching-only tasks, **skip Step 3** but first do **Step 2c** (incremental name resolution), then jump to **Step 3b** to re-generate `coaching_text`.

## Step 2c: Incremental name resolution for coaching-only re-parse

Before regenerating coaching, scan the current `description` and `user_notes` for person names that are NOT already in `key_people`. To detect new names:

1. Parse existing `key_people` JSON to get a set of already-resolved names (including alternatives).
2. Scan `description` and `user_notes` for capitalized multi-word tokens that look like person names (e.g. "Alex Kim", "Sarah") that aren't in the resolved set.
3. For each new name found, call `ask_work_iq` with "Who is [name]? Give me the top 3-4 most likely matches with full name, email, and role."
4. Append newly resolved people to the existing `key_people` array (don't replace existing entries).
5. Update the `key_people` column before proceeding to Step 3b.

This is additive — existing resolved people are preserved, and new names get resolved so coaching text can reference them properly with inline pills.

## Step 3: Full parse — reason about the raw_input

For each task's `raw_input`, use your intelligence to infer ALL of the following. Today's date is $CURRENT_DATE.

- **title**: A clean, concise task title (imperative form, e.g. "Schedule meeting with Jane by Wednesday")
- **description**: A fuller description of what the task involves, including any implied sub-steps. Be helpful and specific.
- **priority**: Integer 1-5 based on urgency cues:
  - 1 = urgent/ASAP/critical/blocker
  - 2 = important/soon/time-sensitive
  - 3 = normal (default)
  - 4 = low importance
  - 5 = information/FYI/not directly actionable by me
- **due_date**: ISO date (YYYY-MM-DD) resolved from any time references. "Next Wednesday" → calculate from today. "End of week" → Friday. "Tomorrow" → tomorrow. null if none implied.
- **key_people**: A JSON array of resolved people. For each name mentioned, call `ask_work_iq` with "Who is [name]? Give me the top 3-4 most likely matches with full name, email, and role." Pick the best match and store alternatives. Format:
  ```json
  [{"name": "Alex Kim", "email": "alex.kim@contoso.com", "role": "PM",
    "alternatives": [
      {"name": "John Smith", "email": "john.smith@contoso.com", "role": "Engineer"},
      {"name": "John Adams", "email": "john.adams@contoso.com", "role": "Designer"}
    ]}]
  ```
  Store as a JSON string in the `key_people` column. If WorkIQ can't resolve, store `[{"name": "John", "alternatives": []}]`.
- **OOO check** (full parse only, not coaching-only re-parse): After resolving key_people, check if any key person is currently out of office. For the **first** (primary) person in key_people, call `ask_work_iq` with: "Check [full name]'s current presence and availability status. Are they showing as Out of Office in Teams or Outlook? Do they have an OOO status, automatic reply, or Out of Office presence set? Also check if I've received any recent automatic reply or OOO email from them. If they are OOO, when are they returning?" If they ARE out of office, set `waiting_activity` to: `{"status": "out_of_office", "return_date": "YYYY-MM-DD", "summary": "[OOO details]", "checked_at": "[now]"}` (use null for return_date if unknown). If they are NOT out of office, leave `waiting_activity` as null. This ensures the OOO badge shows immediately on the dashboard.
- **source_type**: Do NOT change this field. Tasks entered via the dashboard are always 'manual'. Tasks created by /todo-refresh already have the correct source_type set from WorkIQ. Leave the existing value as-is.
- **related_meeting**: If a meeting is mentioned, describe it. Use WorkIQ if helpful: call `ask_work_iq` with "What meetings do I have related to [topic]?" **Important:** After resolving people in the key_people step, always use their full resolved names (e.g. "Jane Doe" not "Jane") in all subsequent WorkIQ queries for more precise results.
- **action_type**: Classify the task into one of these action types based on intent:

  | action_type | Infer when... |
  |---|---|
  | `schedule-meeting` | scheduling, finding time, setting up a meeting |
  | `respond-email` | replying to, responding to, drafting an email |
  | `review-document` | reviewing, reading, giving feedback on a doc/PR/report |
  | `follow-up` | checking in, nudging, getting a status update |
  | `prepare` | preparing for a meeting, presentation, demo |
  | `general` | default fallback |

- **is_quick_hit**: 1 if this is **definitely** a quick task (under ~15 minutes), 0 otherwise. Only tag as quick hit when you're confident. Strong signals: simple email reply, confirmation/approval, brief follow-up ping, forwarding info, short Teams message. NOT quick hit: anything requiring research, preparation, multi-step coordination, document review, meeting scheduling, or deep thought. When in doubt, default to 0.
- **coaching_text**: Generate coaching tailored to the `action_type` and `user_notes` (see Step 3b).

## Step 3b: Generate coaching_text (used by both full parse and coaching-only re-parse)

Generate `coaching_text` based on the task's `action_type`, `description`, `key_people`, and `user_notes`. **Always read `user_notes`** and incorporate them into coaching.

Tailor coaching by action type:

- **schedule-meeting**: Mention calendar availability for key_people (query WorkIQ if helpful), suggest duration/agenda. If `user_notes` contain agenda items → use them. If notes mention a duration → suggest that duration. Note the `/schedule-meeting` skill is available to help.
- **respond-email**: Suggest key points to address based on the source/description. Recommend appropriate tone. Note the `/respond-email` skill is available to help draft the reply.
- **review-document**: Suggest focus areas for the review, time-box the review (e.g. "aim for 30 min").
- **follow-up**: Suggest timeline based on priority/due_date, draft a follow-up outline.
- **prepare**: List concrete prep steps, suggest materials to gather, reference related_meeting if set.
- **general**: Break into 2-3 concrete next steps.

**Important:** Always use full resolved names (e.g. "Jane Doe" not "Jane") in the coaching text so inline people pills render correctly in the dashboard. If `user_notes` contain context (agenda, constraints, preferences), weave that context into the coaching.

## Step 3c: Auto-generate skill output

Generate `skill_output` for the task's primary `action_type` during parsing. By this point you already have title, description, key_people (resolved), action_type, user_notes, coaching_text, and WorkIQ context from earlier steps.

**Skip** if `action_type` is `general` or `review-document` — set `skill_output` to null and move to Step 4.

For all other action types, make **1 focused WorkIQ call** and generate the skill output in the same format as the standalone skill commands. The standalone commands (`/respond-email`, `/schedule-meeting`, `/follow-up`, `/prepare`, `/teams-message`) remain available for re-running with fresh context.

### respond-email
**WorkIQ query:** "Show me the recent email thread about [topic from title/description] with [key_people names]. Include the last 2-3 messages so I can see what was said."

**Output format:**
```
To: [name] <[email]>
Subject: Re: [inferred or from source]

[Draft body — 3-5 sentences, concise, mirror thread tone]

---
Tone: [professional/casual/urgent — inferred from context]
Key points addressed:
- [point 1]
- [point 2]
```

### follow-up / awaiting-response
**WorkIQ query:** "What are my most recent emails and Teams messages with [key_people names] about [topic from title/description]? When was the last interaction?"

**Output format (choose Email or Teams based on source_type):**
```
Channel: [Email / Teams]
To: [name] <[email]>
Subject: [if email — e.g. "Following up: [topic]"]

[Draft message — reference last interaction, be specific about what you need]

---
Last interaction: [date/summary if found]
Days since last contact: [N days]
Urgency: [based on due_date proximity]
```

### schedule-meeting
**WorkIQ query:** If `due_date` is set: "What is the shared calendar availability for [all key_people names] between now and [due_date]? Treat tentative calendar blocks as available. Only show slots during each person's Outlook working hours. Show free time slots that are at least 30 minutes long." If no `due_date`: same query but "this week" instead.

**Output format:**
```
Suggested meeting slots:
1. [Day], [Time] - [Time] ([duration]) — all attendees free
2. [Day], [Time] - [Time] ([duration]) — all attendees free
3. [Day], [Time] - [Time] ([duration]) — all attendees free

Duration: [from user_notes hint or 30 min default]
Attendees: [full names from key_people]
```

Pick the 3 best slots. Filter to working hours only, prefer mornings, avoid lunch (12-1pm).

### prepare
**WorkIQ queries (1-2 calls):**
1. If `related_meeting` is set: "What is the agenda and attendee list for [related_meeting]? What was discussed in previous instances?"
2. "What recent documents, presentations, or files have I worked on related to [topic from title/description]?"

**Output format:**
```
Preparation Notes: [meeting/event name]
Date: [due_date or meeting date if known]
Attendees: [key_people names and roles]

Before the meeting:
[ ] [Concrete prep item 1]
[ ] [Concrete prep item 2]
[ ] [Concrete prep item 3]

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

### teams-message
**WorkIQ query:** "What are my recent Teams chats with [key_people names] about [topic from title/description]? Show the most recent messages."

**Output format:**
```
To: [name] (via Teams)

[Draft message — shorter and more conversational than email, lead with key point]

---
Tone: [casual/direct/detailed — inferred from context]
Purpose: [what this message aims to accomplish]
```

### Guidelines (all action types)
- Use resolved full names from `key_people` (e.g. "Jane Doe" not "Jane")
- If `user_notes` specify points, tone, or constraints, incorporate them
- Do NOT include the `<<<SKILL_OUTPUT>>>` / `<<<END_SKILL_OUTPUT>>>` markers — those are only needed in standalone skill commands. Here the output goes directly into the `skill_output` variable for Step 4's DB write.
- If the WorkIQ query returns no useful context (e.g. no email thread found), still generate a reasonable draft based on the task description and key_people — note that context was limited.

**Note:** Coaching-only re-parse (Step 2b) continues to set `skill_output` to null — it only refreshes coaching text, not skill output.

## Step 4: Write the structured fields back

**For full parse:**

```python
conn = sqlite3.connect('$PROJECT_ROOT/data/claudetodo.db')
now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
conn.execute(
    """UPDATE tasks
       SET title=?, description=?, priority=?, due_date=?,
           key_people=?, related_meeting=?,
           coaching_text=?, action_type=?, skill_output=?,
           waiting_activity=?, is_quick_hit=?,
           suggestion_refreshed_at=?, parse_status='parsed', updated_at=?
       WHERE id=?""",
    (title, description, priority, due_date, key_people,
     related_meeting, coaching_text, action_type, skill_output,
     waiting_activity, is_quick_hit, now, now, task_id)
)
conn.commit()
conn.close()
```

Note: `waiting_activity` is the JSON string from the OOO check (or null if person is not OOO).

**For coaching-only re-parse:**

```python
conn = sqlite3.connect('$PROJECT_ROOT/data/claudetodo.db')
now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
conn.execute(
    """UPDATE tasks
       SET coaching_text=?, skill_output=?, suggestion_refreshed_at=?,
           parse_status='parsed', updated_at=?
       WHERE id=?""",
    (coaching_text, skill_output, now, now, task_id)
)
conn.commit()
conn.close()
```

## Step 5: Show summary

For each parsed task, display:
- Task ID and clean title
- Priority (P1-P5)
- Due date if set
- Key people if identified
- Action type (with icon)
- Coaching tip
- Skill output (if generated — e.g. scheduling slots for schedule-meeting)
- Source type
- Whether it was a full parse or coaching-only refresh

End with: "Parsed N task(s). Run /todo to see your updated task list."
