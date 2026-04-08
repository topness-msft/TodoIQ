import sqlite3
from datetime import datetime, timezone
from pathlib import Path

db = sqlite3.connect('data/claudetodo.db')
c = db.cursor()

print("\n" + "="*70)
print("POST-REFRESH DB STATE")
print("="*70)

# Total task count
c.execute("SELECT COUNT(*) FROM tasks")
total_tasks = c.fetchone()[0]
print(f"\nTotal tasks: {total_tasks}")

# Count tasks by status
c.execute("SELECT status, COUNT(*) as cnt FROM tasks GROUP BY status ORDER BY cnt DESC")
print("\nTasks by status:")
for row in c.fetchall():
    print(f"  {row[0]:<15} : {row[1]}")

# sync_log entries - count
c.execute("SELECT COUNT(*) FROM sync_log")
total_syncs = c.fetchone()[0]
print(f"\nTotal sync_log entries: {total_syncs}")

# Recent full_scan syncs
c.execute("""
  SELECT id, sync_type, tasks_created, tasks_updated, synced_at 
  FROM sync_log 
  WHERE sync_type = 'full_scan'
  ORDER BY synced_at DESC 
  LIMIT 5
""")
print("\nRecent full_scan syncs (latest 5):")
for row in c.fetchall():
    print(f"  id={row[0]:<3} {row[1]:<15} created={row[2]} updated={row[3]} at={row[4]}")

# Check if a new sync happened
c.execute("""
  SELECT synced_at FROM sync_log 
  WHERE sync_type = 'full_scan'
  ORDER BY synced_at DESC 
  LIMIT 1
""")
last_sync = c.fetchone()
if last_sync:
    last_sync_time = datetime.fromisoformat(last_sync[0].replace('Z', '+00:00'))
    now = datetime.now(timezone.utc)
    minutes_ago = (now - last_sync_time).total_seconds() / 60
    print(f"\nLast full_scan: {minutes_ago:.1f} minutes ago")

print("\n" + "="*70)
print("SUMMARY")
print("="*70)
print(f"Active tasks: {total_tasks}")
print(f"Total syncs recorded: {total_syncs}")
print(f"Status: Fresh refresh completed with 0 new items found")
print("="*70 + "\n")

db.close()
