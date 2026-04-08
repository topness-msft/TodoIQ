import sqlite3
from datetime import datetime, timezone

conn = sqlite3.connect('data/claudetodo.db')
conn.row_factory = sqlite3.Row
rows = conn.execute("SELECT synced_at FROM sync_log WHERE sync_type IN ('full_scan', 'flagged_emails') ORDER BY synced_at DESC LIMIT 1").fetchall()
conn.close()

now = datetime.now(timezone.utc)
if rows:
    last_dt = datetime.fromisoformat(rows[0]['synced_at'].replace('Z', '+00:00'))
    days_since = max(1, (now - last_dt).days)
    if days_since > 7:
        days_since = 7
    print(f"Last sync: {rows[0]['synced_at']}")
else:
    days_since = 7
    print("No prior sync found")
print(f"days_since={days_since}")
