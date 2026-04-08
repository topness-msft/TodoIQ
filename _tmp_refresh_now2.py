from pathlib import Path
import sqlite3
from datetime import datetime, timezone, timedelta

# Step 0: Clear sync request marker
p = Path('data') / '.sync_requested'
if p.exists():
    p.unlink()
    print('Cleared .sync_requested marker')
else:
    print('No .sync_requested marker found')

# Step 1: Determine sync window
conn = sqlite3.connect('data/claudetodo.db')
conn.row_factory = sqlite3.Row
last_sync = conn.execute(
    "SELECT synced_at FROM sync_log WHERE sync_type IN ('full_scan', 'flagged_emails') ORDER BY synced_at DESC LIMIT 1"
).fetchone()
conn.close()

now = datetime.now(timezone.utc)
if last_sync and last_sync['synced_at']:
    last_dt = datetime.fromisoformat(last_sync['synced_at'].replace('Z', '+00:00'))
    days_since = max(1, (now - last_dt).days)
    if days_since > 7:
        days_since = 7
    print(f'Last sync: {last_sync["synced_at"]}')
else:
    days_since = 7
    print('No previous sync found')

print(f'DAYS_SINCE={days_since}')
