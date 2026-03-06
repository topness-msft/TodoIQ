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

6. Show summary of actions taken.
