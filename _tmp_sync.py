import sqlite3, json
from datetime import datetime, timezone

conn = sqlite3.connect('data/claudetodo.db')
now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

# Item 2: Augment task #523 (suggested) with latest context
new_snippet = "You asked Saurabh Pant and Dan Stevenson to clarify timing expectations and share a prior sample for the Agent Transformation Playbook so you could better understand scope, length, and audience. You're waiting on their guidance to proceed. Message sent 2026-03-16 afternoon."
new_priority = 3

# Update snippet and bump updated_count
cur = conn.execute("SELECT updated_count FROM tasks WHERE id = 523")
row = cur.fetchone()
old_count = row[0] if row and row[0] else 0

conn.execute(
    "UPDATE tasks SET source_snippet = ?, priority = MIN(priority, ?), updated_at = ?, updated_count = ? WHERE id = 523",
    (new_snippet, new_priority, now, old_count + 1)
)
conn.commit()
print(f"Updated task #523 (updated_count={old_count + 1})")

# Step 4: Check for unparsed tasks
unparsed = conn.execute(
    "SELECT id, title FROM tasks WHERE parse_status IN ('unparsed', 'queued')"
).fetchall()
print(f"Unparsed tasks: {len(unparsed)}")

# Step 5: Log the sync
email_count = 2   # 2 email items found from awaiting-response scan
chat_count = 0
meeting_count = 0
created_count = 0
updated_count = 1  # task #523 augmented
skipped_count = 1  # task #525 dismissed

summary = json.dumps({
    "email": email_count,
    "chat": chat_count,
    "meeting": meeting_count,
    "created": created_count,
    "updated": updated_count,
    "skipped": skipped_count
})

conn.execute(
    "INSERT INTO sync_log (sync_type, result_summary, tasks_created, tasks_updated, synced_at) VALUES (?,?,?,?,?)",
    ('full_scan', summary, created_count, updated_count, now)
)
conn.commit()
print(f"Sync logged at {now}")
print(f"Summary: {summary}")

conn.close()
