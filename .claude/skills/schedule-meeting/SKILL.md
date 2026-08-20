---
name: schedule-meeting
description: Suggest optimal meeting times for a task
triggers:
  - task involves scheduling a meeting
  - user asks to "schedule", "set up meeting", "find time"
  - task has key_people and involves coordination
---

# Schedule Meeting

Find optimal meeting times based on calendar availability via WorkIQ.

**Important:** WorkIQ cannot create or send meeting invites. It can only query work hours and calendar availability to suggest suitable times. The user must send the invite themselves (e.g. via Outlook).

## Steps

1. **Load task context**: Read the task from TodoNess SQLite database at `$PROJECT_ROOT/data/claudetodo.db`. Get key_people, due_date, and priority.

2. **Get your availability**: Call `ask_work_iq`:
   - "What does my calendar look like from [today] through [due_date or next 5 business days]? Show all busy and tentative blocks."

3. **Get attendee availability**: For each person in key_people, call `ask_work_iq`:
   - "What does [person]'s calendar look like from [today] through [due_date or next 5 business days]? Show their busy and tentative blocks, and what are their typical work hours?"

4. **Find open slots**: Analyze the calendars to identify mutual free time.
   - **Treat tentative blocks as available** — they can be scheduled over.
   - Only treat confirmed/busy blocks as unavailable.
   - Respect each person's work hours (don't suggest 7 AM if they start at 9).
   - If a due_date exists, only suggest times before that deadline.

5. **Rank and suggest 3 times**: Pick the best slots considering:
   - Prefer mornings for P1/P2 (important) meetings
   - Avoid back-to-back with other meetings — leave at least a 15-min buffer
   - Prefer earlier in the week when more flexibility remains
   - Consider time zones if people are in different locations
   - **Default to 25 minutes** as the user's standing rule unless the task explicitly requires a different duration

6. **Present options**: Show suggested times with reasoning:
   ```
   Suggested Meeting Times with [key_people]

   1. Tuesday Feb 17, 10:00-10:25 AM — Both calendars free, morning slot
   2. Wednesday Feb 18, 2:00-2:25 PM — After lunch, no conflicts
   3. Thursday Feb 19, 9:00-9:25 AM — Start of day, good for focused discussion

   Next step: Send the invite from Outlook for your preferred slot.
   ```

7. **Save context**: Store the availability analysis as task_context in the database:
   ```python
   import sqlite3
   conn = sqlite3.connect('$PROJECT_ROOT/data/claudetodo.db')
   conn.execute(
       "INSERT INTO task_context (task_id, context_type, content, query_used) VALUES (?,?,?,?)",
       (task_id, 'calendar_event', suggested_times_summary, 'schedule-meeting skill')
   )
   conn.commit()
   conn.close()
   ```
