---
description: Parse unparsed tasks — Claude reads raw text and enriches with structured fields
---

Parse tasks that were added via the dashboard input bar and need enrichment.

Every database connection in this command must use:

```python
db_path = __import__('os').environ.get(
    'TODONESS_DB_PATH',
    '$PROJECT_ROOT/data/claudetodo.db',
)
```

When `RIVETER_DEMO_MODE=1`, keep the turn fast and focused: resolve named people,
infer the structured task fields and coaching, but skip OOO/presence checks,
related-meeting research, and Step 3c skill-output generation.

## Step 1: Fetch unparsed tasks

```python
import sqlite3
from datetime import datetime, timezone

conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
tasks = conn.execute("SELECT id, raw_input, title, description, key_people, action_type, user_notes, source_type, source_url, parse_status FROM tasks WHERE parse_status IN ('unparsed', 'queued') AND status NOT IN ('deleted', 'completed')").fetchall()
conn.close()
```

If no unparsed tasks, say "All tasks are already parsed!" and stop.

## Step 2: For each unparsed task, mark as 'parsing'

```python
conn = sqlite3.connect(db_path)
now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
conn.execute("UPDATE tasks SET parse_status = 'parsing', updated_at = ? WHERE id = ?", (now, task_id))
conn.commit()
conn.close()
```

## Step 2b: Check if this is a coaching-only re-parse

A task needs **coaching-only** re-parse if it already has `title`, `description`, and `key_people` populated (i.e. it was previously fully parsed). This happens when the user changes the `action_type`, edits the description, or chooses a different identity from the dashboard.

For coaching-only tasks, **skip Step 3**. Do Step 2c only for entries that
remain unresolved or lack an email. Do not repeat Step 2d when `key_people`
already contains confirmed email-backed people. Never replace confirmed `key_people`,
never restore `unresolved` on a confirmed person, and never re-add a person the user removed.
Then jump to Step 3b to regenerate `coaching_text`.

## Step 2c: Incremental name resolution for coaching-only re-parse

Before regenerating coaching, resolve only existing name-only or unresolved
entries already in `key_people`:

1. Parse existing `key_people` JSON. Treat every entry without a non-empty `email` as unresolved, even if its name is already present.
2. For each unresolved entry, first run an exact directory query for the full
   display name. When it returns exactly one internal tenant profile whose
   normalized display name matches and which has an email, persist that exact
   profile without `unresolved`. Multiple plausible matches, fuzzy matches,
   guest/external-only results, or profiles without an email remain candidates:
   put the best candidate first, retain the others in `alternatives`, and keep
   `unresolved: true`.
3. Do not scan task prose for new people during coaching-only refresh. The
   existing selected list is authoritative; names absent from it may have been
   removed by the user.
4. Update only the existing unresolved entries in `key_people` before proceeding
   to Step 3b.

Existing email-backed people without an `unresolved` marker must be preserved
unchanged. Their presence in `key_people` means the user selected them; a
coaching refresh must not reconstruct the attendee list from the source chat.
Name-only or genuinely ambiguous people are upgraded in place so the existing
alternate-name dropdown remains the explicit confirmation boundary.

## Step 2d: Resolve exact Teams-link participants

Run this step for a full parse whenever `source_url` is a
`teams.microsoft.com/l/chat/.../conversations` or `/l/message/...` link. For a
coaching-only parse, run it only when `key_people` is empty; otherwise preserve
the selected list as described above. Complete it before any Cowork scheduling
preview. Complete exact participant resolution before any Cowork scheduling preview.
The Teams conversation is authoritative. Never substitute a recent chat or recent contact
when exact lookup fails.

1. Fetch the signed-in profile with WorkIQ (`/me?$select=id,displayName,mail,userPrincipalName`)
   and parse the decoded conversation ID from `source_url`.
2. Fetch exact membership with
   `/users/{self_id}/chats/{encoded_conversation_id}/members`. Do not add `$top`;
   chat membership does not support it.
3. From the exact membership response, exclude the signed-in user. For a 1:1 link, also use
   `parse_source_url(source_url, me=self_id)["counterparty_id"]` as the exact member
   object ID. If a membership row exposes only its base64 `id`, decode it and
   extract the final validated Entra GUID; do not guess from display name.
4. Fetch each exact directory profile from
   `/users/{member_object_id}?$select=id,displayName,mail,userPrincipalName,jobTitle,officeLocation`.
   Use `mail`, falling back to `userPrincipalName`, only from that exact profile.
5. Fetch recent messages only from the same conversation with
   `/users/{self_id}/chats/{encoded_conversation_id}/messages?$select=id,createdDateTime,from,body&$top=20`.
   If the meeting request explicitly names additional attendees, directory-search
   those names with the exact chat participants and topic as context. Store those
   matches with `unresolved: true` and alternatives; never silently select one.
6. Exact membership profiles are confirmed identities; do not add `unresolved`
   to exact internal profiles. Exact membership proves identity, not meeting attendance.
   Persist an exact internal 1:1 counterpart as
   `{name,email,role,aad_object_id}` without `unresolved`. For group chats:
   - If the request explicitly names attendees, add only those exact members.
   - If it explicitly says the whole group/everyone should attend, add every
     exact internal member as confirmed.
   - Otherwise add exact internal members with `attendance_uncertain: true`.
     Their identity is confirmed, but the user must confirm or remove each
     attendee before scheduling.
   Guest or external membership profiles always keep `unresolved: true` even
   when membership and email are exact. Additional people found only through
   name search follow the Step 2c certainty rule.
7. If exact membership/profile lookup fails, lacks an email, or is ambiguous, leave
   the person unresolved and record the failure in the task description. Never
   continue scheduling with an empty `key_people` list.

When several confirmed attendees remain, Cowork's existing availability matrix
shows the time choices across every person. Before proposing times, use calendar
working-hours data to try to verify each attendee's timezone; disclose unknown
timezones rather than guessing.

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
- **key_people**: A JSON array of directory-matched people. For each name
  mentioned, first use an exact full display-name directory query. Exactly one
  internal tenant profile with an exact normalized display name and email may be
  persisted as confirmed. Multiple plausible matches, fuzzy matches, guests,
  or missing-email profiles must keep `unresolved: true` and alternatives for
  explicit selection. Format:
  ```json
  [{"name": "Alex Kim", "email": "alex.kim@contoso.com", "role": "PM",
    "unresolved": true,
    "alternatives": [
      {"name": "John Smith", "email": "john.smith@contoso.com", "role": "Engineer"},
      {"name": "John Adams", "email": "john.adams@contoso.com", "role": "Designer"}
    ]}]
  ```
  Store as a JSON string in the `key_people` column. If WorkIQ can't resolve, store `[{"name": "John", "alternatives": [], "unresolved": true}]`.
- **OOO check** (full parse only, not coaching-only re-parse): After resolving key_people, check if any key person is currently out of office. For the **first** (primary) person in key_people, call `ask_work_iq` with: "Check [full name]'s current presence and availability status. Are they showing as Out of Office in Teams or Outlook? Do they have an OOO status, automatic reply, or Out of Office presence set? Also check if I've received any recent automatic reply or OOO email from them. If they are OOO, when are they returning?" If they ARE out of office, set `waiting_activity` to: `{"status": "out_of_office", "return_date": "YYYY-MM-DD", "summary": "[OOO details]", "checked_at": "[now]"}` (use null for return_date if unknown). If they are NOT out of office, leave `waiting_activity` as null. This ensures the OOO badge shows immediately on the dashboard.
- **source_type**: Do NOT change this field. Tasks entered via the dashboard are always 'manual'. Tasks created by /todo-refresh already have the correct source_type set from WorkIQ. Leave the existing value as-is.
- **source_url**: Do NOT change or clear this field. A manually pasted Teams link is preserved here so the parsed task still links to the original conversation.
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

`coaching_text` is the task's **intent** — the specific next action, in the imperative, naming the person and the concrete ask. It is handed to the action layer verbatim as its instruction, so it must be *executable*, not advisory. Never derive it from `action_type` alone: `action_type` selects the verb, not the content. Two tasks must never share a `coaching_text` string — if what you wrote would fit any other task of the same `action_type` unchanged, it is too generic. See `/todo-refresh` Step 3b for the full contract and worked good/bad examples; the bar is identical here.

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

## Step 3d: Answer @WorkIQ questions in user_notes

For each task, check its `user_notes` for unanswered `@WorkIQ` questions. A line contains an `@WorkIQ` question if it includes `@WorkIQ` (case-insensitive). A question is **unanswered** if the line immediately following it does NOT start with `  →` (two spaces then →).

If there are unanswered questions, make a single WorkIQ call with all questions:

> "Answer these questions about [task title]: 1) [question text without the @WorkIQ prefix] 2) [next question] ..."

After getting the response, write answers back into `user_notes` by inserting `  → [answer text]` on the line immediately below each answered question. Use:

```python
conn = sqlite3.connect(db_path)
now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
task_id = TASK_ID
qa_pairs = [('@WorkIQ question line text', 'answer text'), ...]
row = conn.execute('SELECT user_notes FROM tasks WHERE id = ?', (task_id,)).fetchone()
if row and row[0]:
    lines = row[0].split('\n')
    new_lines = []
    for line in lines:
        new_lines.append(line)
        for q, a in qa_pairs:
            if q.strip() in line:
                new_lines.append('  → ' + a)
                break
    conn.execute('UPDATE tasks SET user_notes = ?, updated_at = ? WHERE id = ?', ('\n'.join(new_lines), now, task_id))
    conn.commit()
conn.close()
```

Replace TASK_ID and the question/answer pairs with actual values. Skip this step if the task has no unanswered `@WorkIQ` questions. This step applies to both full parse and coaching-only re-parse.

## Step 4: Write the structured fields back

**For full parse:**

```python
conn = sqlite3.connect(db_path)
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
conn = sqlite3.connect(db_path)
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
