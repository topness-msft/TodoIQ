import sqlite3
from datetime import datetime, timezone

conn = sqlite3.connect("data/claudetodo.db")
conn.row_factory = sqlite3.Row
last_sync = conn.execute(
    "SELECT synced_at FROM sync_log WHERE sync_type IN ('full_scan', 'flagged_emails') ORDER BY synced_at DESC LIMIT 1"
).fetchone()
conn.close()

now = datetime.now(timezone.utc)
if last_sync and last_sync["synced_at"]:
    last_dt = datetime.fromisoformat(last_sync["synced_at"].replace("Z", "+00:00"))
    days_since = max(1, (now - last_dt).days)
    if days_since > 7:
        days_since = 7
else:
    days_since = 7

print(f"days_since={days_since}")
print(f"last_sync={last_sync['synced_at'] if last_sync else 'None'}")
