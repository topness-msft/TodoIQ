---
description: Interactive review of tasks needing attention
---

Walk through tasks that need attention and take action.

Steps:
1. Query for tasks needing attention:
   - Overdue tasks (due_date < today, status != completed/dismissed)
   - Suggested tasks (status = 'suggested') — need promote/dismiss decision
   - Stale active tasks (suggestion_refreshed_at > 24 hours ago or NULL)

2. Present each group with counts:
   ```
   TodoNess Review

   Overdue (2)
   Pending Suggestions (3)
   Stale Active Tasks (1)
   ```

3. For each task in priority order, present:
   - Task title, priority, due date, source
   - Coaching text if available
   - Available actions based on status

4. Ask the user what action to take:
   - Suggested tasks: Promote to active? Dismiss? Skip?
   - Overdue tasks: Mark complete? Update due date? Dismiss?
   - Stale tasks: Refresh context via WorkIQ? Skip?

5. If refreshing context, call `ask_work_iq` with a **cross-channel** query and update coaching_text.

   The query should ask across ALL communication channels (email, Teams, meetings) regardless of the task's original source_type. Use this pattern:

   > "What are my recent emails, Teams messages, and meeting interactions with [key person] related to [task topic] since [task created date or last refresh date]? Include any relevant context, updates, or responses."

   - Extract [key person] from the task's `key_person` field
   - Extract [task topic] from the task's title and any existing context
   - Use `suggestion_refreshed_at` as the "since" date if set, otherwise use `created_at`
   - Update `coaching_text` with the enriched context from the response
   - Update `suggestion_refreshed_at` to now

   **Note:** This cross-channel approach is intentional. Responses to tasks can arrive on any channel — a meeting action item may be resolved via email, and a Teams request may get an email reply. Always query all channels to build the full picture.

   ### @WorkIQ inline questions

   When refreshing a task, also check its `user_notes` for unanswered `@WorkIQ` questions. A line contains an `@WorkIQ` question if it includes `@WorkIQ` (case-insensitive). A question is **unanswered** if the line immediately following it does NOT start with `  →` (two spaces then →).

   If there are unanswered questions, append them to the WorkIQ cross-channel query:

   > "Additionally, answer these questions from the user's notes: 1) [question text without the @WorkIQ prefix] 2) ..."

   After getting the response, write answers back into `user_notes` by inserting `  → [answer text]` on the line immediately below each answered question. Use:

   ```bash
   python -c "
   import sqlite3
   from datetime import datetime, timezone
   conn = sqlite3.connect('data/claudetodo.db')
   now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
   task_id = TASK_ID
   qa_pairs = [('@WorkIQ question line text', 'answer text'), ...]
   row = conn.execute('SELECT user_notes FROM tasks WHERE id = ?', (task_id,)).fetchone()
   if row and row[0]:
       lines = row[0].split('\n')
       new_lines = []
       for line in lines:
           new_lines.append(line)
           for question_line, answer in qa_pairs:
               if line.strip() == question_line.strip():
                   new_lines.append('  → ' + answer)
                   break
       conn.execute('UPDATE tasks SET user_notes = ?, updated_at = ? WHERE id = ?', ('\n'.join(new_lines), now, task_id))
       conn.commit()
   conn.close()
   "
   ```

   Replace TASK_ID and the question/answer pairs with actual values. Skip this if the task has no unanswered `@WorkIQ` questions.

6. Show summary of actions taken.
