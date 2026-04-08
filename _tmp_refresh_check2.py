import sqlite3

conn = sqlite3.connect(r'C:\Users\phtopnes\claude\projects\ClaudeTodo\data\claudetodo.db')
# Delete the bogus sync entry from the dummy script
conn.execute("DELETE FROM sync_log WHERE synced_at = '2026-04-08T14:03:47Z'")
print(f"Deleted {conn.total_changes} bogus sync entry")
conn.commit()
conn.close()

