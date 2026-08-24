---
description: Check for activity on waiting tasks via WorkIQ
---

Check all "waiting" tasks for recent activity from their key people using WorkIQ.

Today's date is $CURRENT_DATE.

## Scope: all waiting tasks, or one

If this command was invoked with a task id (`/waiting-check 2129`), check **only
that task** — load it by id regardless of its status, since the user asked for
it explicitly by pressing Check Now on that card. Skip the OOF re-check sweep in
Step 1 and use this query instead:

```bash
python -c "
import sqlite3, json
conn = sqlite3.connect('data/claudetodo.db')
conn.row_factory = sqlite3.Row
r = conn.execute('SELECT id, title, description, key_people, source_type, source_id, source_url, created_at, status, waiting_activity, user_notes FROM tasks WHERE id = ?', (TASK_ID,)).fetchone()
if r:
    print(json.dumps({'id': r['id'], 'title': r['title'], 'key_people': r['key_people'] or '', 'source_type': r['source_type'] or 'manual', 'source_id': r['source_id'] or '', 'source_url': r['source_url'] or '', 'created_at': r['created_at'], 'status': r['status'], 'waiting_activity': r['waiting_activity'] or '', 'user_notes': r['user_notes'] or ''}))
conn.close()
"
```

Everything else — the cursor, the queries, the classification, the write — is
identical. Then stop; do not sweep the rest.

With no task id, check every waiting task as described below.

## Step 1: Load waiting tasks (including snoozed OOF tasks due for re-check)

Use the Bash tool to run this Python script to get all waiting tasks and snoozed OOF tasks:

```bash
python -c "
import sqlite3, json
conn = sqlite3.connect('data/claudetodo.db')
conn.row_factory = sqlite3.Row
rows = conn.execute(\"\"\"
    SELECT id, title, description, key_people, source_type, source_id, source_url, created_at, status, waiting_activity, user_notes
    FROM tasks
    WHERE status = 'waiting'
       OR (status = 'snoozed'
           AND waiting_activity LIKE '%out_of_office%'
           AND (json_extract(waiting_activity, '\$.checked_at') IS NULL
                OR json_extract(waiting_activity, '\$.checked_at') < datetime('now', '-20 hours')))
\"\"\").fetchall()
for r in rows:
    print(json.dumps({'id': r['id'], 'title': r['title'], 'key_people': r['key_people'] or '', 'source_type': r['source_type'] or 'manual', 'source_id': r['source_id'] or '', 'source_url': r['source_url'] or '', 'created_at': r['created_at'], 'status': r['status'], 'waiting_activity': r['waiting_activity'] or '', 'user_notes': r['user_notes'] or ''}))
conn.close()
"
```

If there are zero tasks, print "No waiting tasks to check." and stop.

### Work out the window to read (`check_since`)

Do **not** use `updated_at`. This command writes `updated_at` on every run
(Step 4), so using it would mean "since the last time I looked" and would
shrink the window each pass — and would step straight over anything that
arrived while a check was failing.

For each task, `check_since` is:

1. The `checked_at` of the previous `waiting_activity`, **if** that check
   succeeded (its `check_state` is absent or `"ok"`).
2. Otherwise the previous `check_since` — a failed check must not advance the
   cursor, or the unread period is skipped forever.
3. Otherwise `created_at` (for `manual` tasks, 2 days before `created_at` —
   manual tasks are often written up after the activity already happened).

## Step 2: Choose who to query and call WorkIQ

For each task, determine the **target person** to check:

1. **Non-manual tasks** (`source_type` is `email`, `chat`, or `meeting`): Extract the originator from `source_id` (format: `type::email::subject` — the email/middle portion identifies who raised it). Use that person's name from `key_people`. This is the person most likely to have responded.
2. **Manual tasks** or if source originator can't be determined: Use the first person in `key_people`.
3. **No key_people**: classify as `no_activity` with summary "No key people to check" and skip the WorkIQ query.

Determine the **query start date**:
- **Manual tasks** (`source_type = 'manual'`): use 2 days before `created_at` — manual tasks are often created after the relevant activity already happened.
- **Non-manual tasks**: use `created_at` as-is — the task was auto-created at the time of the activity.

**First**, check if the target person is out of office. Ask WorkIQ:
> "Check [person]'s current presence and availability status. Are they showing as Out of Office in Teams or Outlook? Do they have an OOO status, automatic reply, or Out of Office presence set? Also check if I've received any recent automatic reply or OOO email from them. If they are OOO, when are they returning?"

**Note:** WorkIQ sometimes misses OOO status with simple queries. The explicit mention of "presence", "Teams", and "Outlook" helps it check the right signals.

**Then**, read for activity. Prefer the originating thread; fall back to the person.

**(a) Thread-scoped — use this when `source_url` is a `teams.microsoft.com` link.**
The URL carries the conversation id, so the exact thread can be re-read:

> "Read the Teams conversation at [source_url] and list every message posted
> since [check_since], with sender, timestamp and text. If there are none, say
> so explicitly."

Record `source_scope: "thread"` for this task, and the conversation id from the
URL.

**(b) Person-scoped — everything else** (Outlook links, meeting tasks, manual
tasks, or any task with no usable `source_url`):

> "What are my most recent emails, Teams messages, and chats with [person] since [check_since]? List all interactions found."

Record `source_scope: "person"`.

The distinction matters to the reader: "no reply on this thread" is a much
stronger statement than "nothing from this person anywhere", and the card
renders them differently. Do not claim the first when you did the second.

**IMPORTANT:** For person-scoped reads, always query all channels regardless of `source_type` — responses can come on any channel (e.g. a meeting action item resolved via email, an email task answered in Teams). Do NOT limit to the specific task topic. WorkIQ may miss relevant responses if the query is too narrow. You will classify relevance yourself in Step 3.

### @WorkIQ inline questions

For each task, check its `user_notes` for unanswered `@WorkIQ` questions. A line contains an `@WorkIQ` question if it includes `@WorkIQ` (case-insensitive). A question is **unanswered** if the line immediately following it does NOT start with `  →` (two spaces then →).

If there are unanswered questions, append them to the WorkIQ activity query for that task:

> "Additionally, answer these questions from the user's notes: 1) [question text without the @WorkIQ prefix] 2) [next question] ..."

Keep the answers separate from the activity classification — save the answers for writing back to `user_notes` in Step 4.

## Step 3: Classify responses

Review the WorkIQ results against the task's title and description. Classify using one of four statuses. **`out_of_office` takes priority** — if the OOO check shows the person has an automatic reply or is OOO, classify as `out_of_office` regardless of any recent communications.

- **`out_of_office`** — person has an automatic reply / OOO set. Summary: describe the OOO message. Include `return_date` (ISO date like "2026-03-10", or null if unknown).
- **`no_activity`** — no messages at all from that person since the task was created. Summary: "No response from [person] since [date]"
- **`activity_detected`** — person has been communicating but not clearly about this task's topic. Summary: describe what was found and note whether it might be related
- **`may_be_resolved`** — person sent a clear response or resolution relevant to this specific task. Summary: brief description of the resolution

When in doubt, prefer `activity_detected` over `no_activity` — any communication is worth surfacing. Prefer `activity_detected` over `may_be_resolved` unless the resolution is obvious.

`may_be_resolved` renders as **"Looks done?"** — a prompt for the user to
confirm, not a completion. Nothing in Riveter completes a task off the back of
it. Do not stretch to reach it; an over-eager "looks done" invites the user to
drop something that is still open, which is the most expensive mistake this
check can make.

### Capture evidence for whatever you classify

A summary on its own is an assertion. For any classification other than
`no_activity`, record up to 3 `evidence` entries — the actual messages the
judgement rests on:

```json
{"excerpt": "Sending the numbers over now", "when": "2026-08-21T09:00:00Z", "where": "Teams", "url": null}
```

Quote the source; do not paraphrase into the excerpt. `where` is the channel
("Teams", "Email"). `url` is a deep link if WorkIQ returned one, otherwise
null. The card renders these so the user can check the claim rather than take
it on trust.

**WorkIQ errors:** If `ask_work_iq` fails, times out, or returns nothing
readable for a task, **record the failure** — do NOT skip the task and do NOT
invent a classification.

This is the point of the whole check. Previously a failure meant nothing was
written, so the card kept showing the last successful result with its original
timestamp: "I could not look" was displayed as "I looked and found this". Write
instead:

```json
{"check_state": "failed", "error": "[what happened]", "previous": {...the prior activity, if any...}}
```

The dashboard renders that as **"Couldn't check"**, keeps any earlier finding
clearly labelled as earlier, and leaves the cursor where it was so the next run
re-reads the same window.

## Step 4: Write ALL results to SQLite

After checking ALL tasks, use the Bash tool to run a single Python script that writes every result to the database. Build the full script with all task results hardcoded, then execute it.

For `out_of_office` results, include `return_date` in the JSON (ISO date string or null).

For **snoozed OOF tasks** (tasks that came from the expanded query with `status = 'snoozed'`): if the re-check shows the person is **no longer OOO** (i.e. not classified as `out_of_office`), **auto-unsnooze** them — set `status = 'waiting'`, clear `snoozed_until`, and write the new classification.

```bash
python -c "
import sqlite3, json
from datetime import datetime, timezone
conn = sqlite3.connect('data/claudetodo.db')
now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
# (task_id, classification_or_None, summary, return_date, orig_status,
#  check_since, source_scope, conversation_id, evidence, error_or_None, previous_or_None)
results = [
    (TASK_ID, 'CLASSIFICATION', 'SUMMARY', RETURN_DATE_OR_NONE, ORIGINAL_TASK_STATUS,
     'CHECK_SINCE', 'SOURCE_SCOPE', CONVERSATION_ID_OR_NONE, EVIDENCE_LIST, None, None),
    ...
]
for (task_id, classification, summary, return_date, orig_status,
     check_since, source_scope, conversation_id, evidence, error, previous) in results:
    activity = {
        'version': 2,
        'producer': 'waiting-check',
        'check_state': 'failed' if error else 'ok',
        'checked_at': now,
        'check_since': check_since,
        'source_scope': source_scope,
    }
    if error:
        # A failed check has no finding of its own. Keep the earlier one under
        # 'previous' so the card can show it AS earlier, and leave check_since
        # where it was so the next run re-reads the window nobody managed to read.
        activity['error'] = error
        if previous:
            activity['previous'] = previous
    else:
        activity['status'] = classification
        activity['summary'] = summary
        if evidence:
            activity['evidence'] = evidence
        if conversation_id:
            activity['conversation_id'] = conversation_id
        if return_date:
            activity['return_date'] = return_date
    val = json.dumps(activity)
    if orig_status == 'snoozed' and not error and classification != 'out_of_office':
        # Auto-unsnooze: person is back, move to waiting. Never on a failed
        # check - 'could not tell' is not evidence that they are back.
        conn.execute('UPDATE tasks SET waiting_activity = ?, status = ?, snoozed_until = NULL, updated_at = ? WHERE id = ?', (val, 'waiting', now, task_id))
        print('Task ' + str(task_id) + ': auto-unsnoozed (person no longer OOO)')
    else:
        conn.execute('UPDATE tasks SET waiting_activity = ?, updated_at = ? WHERE id = ?', (val, now, task_id))
conn.commit()
conn.close()
print('Updated ' + str(len(results)) + ' tasks')
"
```

Replace the placeholders with actual values. Use `None` for `return_date`,
`conversation_id`, `error` and `previous` where they do not apply, and `[]` for
`evidence`. Use the task's original `status` field from Step 1. For a failed
check pass `None` for the classification and summary — the shape refuses to
carry a finding alongside a failure.

The shape written here is the v2 contract in `src/services/waiting_activity.py`;
`src/models.py` normalises every row through it on read, so older rows still
render. Keep the two in step.

### Write @WorkIQ answers back to `user_notes`

For tasks that had unanswered `@WorkIQ` questions, write answers back into `user_notes`. Add a second Python script (or extend the one above) that:

1. Reads the current `user_notes` for the task
2. Finds each `@WorkIQ` question line
3. Inserts `  → [answer text]` on the line immediately below each question
4. Writes back via `UPDATE tasks SET user_notes = ?, updated_at = ? WHERE id = ?`

```bash
python -c "
import sqlite3
from datetime import datetime, timezone
conn = sqlite3.connect('data/claudetodo.db')
now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
answers = [
    (TASK_ID, [('@WorkIQ question line text', 'answer text'), ...]),
    ...
]
for task_id, qa_pairs in answers:
    row = conn.execute('SELECT user_notes FROM tasks WHERE id = ?', (task_id,)).fetchone()
    if not row or not row[0]:
        continue
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
print('Wrote @WorkIQ answers back to user_notes')
"
```

Replace TASK_ID and the question/answer pairs with actual values from the WorkIQ responses. Only include tasks that had unanswered `@WorkIQ` questions. Skip this step if no tasks had questions.

## Step 5: Print summary

**You MUST print your results using this EXACT format with markers:**

<<<SKILL_OUTPUT>>>
Waiting Activity Check — [date]
Checked [N] tasks

#[id] [title] — [status]: [summary]
...
<<<END_SKILL_OUTPUT>>>
