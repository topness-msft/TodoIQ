import sqlite3
import json
from datetime import datetime, timezone

conn = sqlite3.connect('data/claudetodo.db')
conn.row_factory = sqlite3.Row
now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

summary = json.dumps({
    "email": 1,
    "chat": 2,
    "meeting": 0,
    "created": 3,
    "updated": 4,
    "skipped": 0
})
conn.execute(
    "INSERT INTO sync_log (sync_type, result_summary, tasks_created, tasks_updated, synced_at) VALUES (?,?,?,?,?)",
    ('full_scan', summary, 3, 4, now)
)
conn.commit()
print(f"Logged sync at {now}")

unparsed = conn.execute(
    "SELECT COUNT(*) as cnt FROM tasks WHERE parse_status IN ('unparsed', 'queued')"
).fetchone()
print(f"Unparsed tasks: {unparsed['cnt']}")
conn.close()
