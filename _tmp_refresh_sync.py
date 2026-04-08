import json
import sqlite3
from datetime import datetime, timezone

summary = json.dumps({
    "email": 0,
    "chat": 6,
    "meeting": 2,
    "created": 4,
    "updated": 3,
    "skipped": 2
})

conn = sqlite3.connect('data/claudetodo.db')
conn.execute(
    "INSERT INTO sync_log (sync_type, result_summary, tasks_created, tasks_updated, synced_at) VALUES (?,?,?,?,?)",
    ('full_scan', summary, 4, 3,
     datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))
)
conn.commit()
conn.close()
print("Sync logged.")
