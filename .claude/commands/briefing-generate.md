---
description: Generate Chief of Staff briefing from tasks and M365 context
---

Generate a Chief of Staff briefing by analyzing current tasks and querying M365 for calendar/communication context.

Today's date is $CURRENT_DATE.

## Step 1: Load current tasks

```python
import sqlite3, json

conn = sqlite3.connect('$PROJECT_ROOT/data/claudetodo.db')
conn.row_factory = sqlite3.Row
rows = conn.execute("""
    SELECT id, title, description, status, priority, key_people, source_type,
           source_id, source_url, due_date, committed_date, coaching_text,
           waiting_activity, action_type, created_at, updated_at
    FROM tasks
    WHERE status IN ('active','in_progress','waiting','suggested')
    ORDER BY priority, status, updated_at DESC
""").fetchall()

tasks = []
for r in rows:
    t = dict(r)
    if t['key_people']:
        try:
            t['key_people'] = json.loads(t['key_people'])
        except:
            t['key_people'] = []
    else:
        t['key_people'] = []
    if t['waiting_activity']:
        try:
            t['waiting_activity'] = json.loads(t['waiting_activity'])
        except:
            t['waiting_activity'] = None
    tasks.append(t)

conn.close()
print(f"Loaded {len(tasks)} tasks")
for t in tasks:
    people = ', '.join(p.get('name','') for p in t['key_people']) if t['key_people'] else 'none'
    wa = ''
    if t['waiting_activity']:
        wa = f" | wa={t['waiting_activity'].get('status','')} {t['waiting_activity'].get('summary','')[:60]}"
    print(f"  #{t['id']} [{t['status']}] P{t['priority']} — {t['title']} | people: {people}{wa}")
```

## Step 2: Query M365 context via WorkIQ

Make these 3 WorkIQ calls. Summarize each result concisely.

### 2a: Today's calendar

Call `ask_work_iq`:
```
What meetings do I have today ($CURRENT_DATE)? For each meeting list: time, title, attendees (names + emails), whether there's an agenda or shared docs, and any commitments I made in previous meetings with those same attendees.
```

### 2b: Week meeting load

Call `ask_work_iq`:
```
How many hours of meetings do I have each day for the next 5 business days starting $CURRENT_DATE? For each day give: total meeting hours, number of meetings, and any large blocks of free time (90+ minutes).
```

### 2c: Unanswered messages

Call `ask_work_iq`:
```
Are there any Teams messages or emails directed specifically at me that I haven't responded to in the last 7 days? Only include direct messages or small group chats (5 or fewer people) where someone asked me a question or requested action. For each, include: sender name and email, date sent, subject/topic, and how many days it's been waiting.
```

## Step 3: Generate briefing JSON

Using the task data from Step 1 and the M365 context from Step 2, generate a structured briefing. Think like an experienced **chief of staff** — don't just list things, provide insight about what matters and why.

### Clustering rules
- Group tasks into **3-6 initiatives** based on shared key_people and topic overlap
- Each initiative should have a clear theme name
- Tasks can only belong to one initiative; if ambiguous, put in the one with more people overlap

### Health assessment
- **on-track**: Active progress, no stale items, dependencies moving
- **at-risk**: Some items stale (>7 days no update) or missing key responses
- **stale**: Most items haven't moved in 14+ days
- **blocked**: External dependency preventing progress

### For each initiative, write a CoS narrative
This is the most important part. Write like a chief of staff briefing the exec — direct, opinionated, specific:
- **Progress**: What actually moved? What's unblocked? Be specific about who did what.
- **Risk**: What's about to go wrong if nothing changes? Name the person or dependency.
- **Next actions**: Split into You (exec must do), Others (delegate/expect), Hold (park until date/event).

### Output format

Output ONLY valid JSON (no markdown fences, no commentary) with this exact structure:

```json
{
  "attention": {
    "stale_followups": [
      {"task_id": 450, "title": "...", "person": "Name", "days_waiting": 29, "priority": 3}
    ],
    "unanswered": [
      {"person": "Name", "topic": "...", "days_waiting": 6, "source": "teams|email"}
    ]
  },
  "initiatives": [
    {
      "name": "Initiative Name",
      "health": "on-track|at-risk|stale|blocked",
      "task_count": 6,
      "waiting_count": 2,
      "people": ["Name One", "Name Two"],
      "task_ids": [450, 488, 513],
      "cos_narrative": "Full CoS narrative in HTML. Use <p> tags for paragraphs and <strong> for emphasis. Include Progress, Risk, and Next Actions sections.",
      "actions": [
        {"label": "Action button text", "type": "primary|ai|secondary", "task_id": 450, "action": "promote|start|null"}
      ]
    }
  ],
  "calendar": {
    "today_summary": "5.5 hours of meetings — 1 has no agenda",
    "today_meetings": [
      {"time": "9:00 AM", "title": "Meeting name", "attendees": ["Name"], "has_agenda": true, "related_task_ids": [812]}
    ],
    "meeting_preps": [
      {
        "time": "9:00 AM",
        "title": "Meeting name",
        "attendees": ["Name One", "Name Two"],
        "open_tasks": [{"id": 812, "title": "...", "status": "active"}],
        "prep_insight": "CoS-style prep note: what to bring up, what to watch for, what commitments you made previously with these attendees. Be specific and actionable.",
        "actions": [
          {"label": "Build prep notes", "type": "ai", "task_id": 812}
        ]
      }
    ],
    "week_load": [
      {"day": "Wed", "hours": 5.5, "meeting_count": 6, "is_today": true}
    ],
    "recommendation": "Plain text recommendation about the week"
  },
  "people": [
    {
      "name": "Greg Hurlman",
      "initials": "GH",
      "color": "#e65100",
      "detail": "5 tasks · 3 waiting",
      "badge": null,
      "badge_type": null,
      "task_count": 5,
      "waiting_count": 3
    }
  ],
  "relationship_insight": {
    "person": "Greg Hurlman",
    "title": "Insight title",
    "body": "Insight body text",
    "actions": [
      {"label": "Action text", "type": "ai|primary|secondary", "task_id": 450}
    ]
  }
}
```

Important:
- Task IDs in narratives should use `#NNN` format (the page auto-linkifies them)
- People colors: pick from this palette based on initials hash: #e65100, #2564cf, #8b5cf6, #d13438, #107c10, #f7630c, #616161, #00838f, #6a1b9a, #4e342e
- Stale follow-ups: only include waiting tasks older than 7 days
- People list: sorted by urgency (most stale/overdue first), max 8 people
- Calendar week_load: next 5 business days
- **Action buttons**: Every button MUST have a `task_id`. Use `"action": "promote"` for buttons that promote suggested tasks to active. Use `"type": "ai"` for buttons that should trigger AI skill generation (draft replies, build prep notes). Use `"type": "primary"` for direct actions, `"secondary"` for lower-priority. Buttons without `action` field will open the task detail panel.

## Step 4: Save to database

```python
import sqlite3, json
from datetime import datetime, timezone

# The briefing JSON was generated in Step 3. Parse it from the output above.
# IMPORTANT: The JSON must be the exact output from Step 3 — do not modify it.

briefing_json = """<paste the JSON from Step 3 here>"""

# Validate it parses
data = json.loads(briefing_json)
assert 'initiatives' in data, "Missing initiatives"
assert 'attention' in data, "Missing attention"

now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
conn = sqlite3.connect('$PROJECT_ROOT/data/claudetodo.db')
conn.execute(
    "INSERT INTO briefing_cache (id, status, content, generated_at) "
    "VALUES (1, 'ready', ?, ?) "
    "ON CONFLICT(id) DO UPDATE SET status='ready', content=?, generated_at=?, error_message=NULL",
    (briefing_json, now, briefing_json, now)
)
conn.commit()
conn.close()
print(f"Briefing saved at {now}")
```

If any step fails, save the error:

```python
import sqlite3
conn = sqlite3.connect('$PROJECT_ROOT/data/claudetodo.db')
conn.execute(
    "UPDATE briefing_cache SET status='error', error_message=? WHERE id=1",
    ("Error description here",)
)
conn.commit()
conn.close()
```
