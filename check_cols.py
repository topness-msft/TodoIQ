import sqlite3
db = sqlite3.connect('data/claudetodo.db')
c = db.cursor()

# Check sync_log columns
c.execute("PRAGMA table_info(sync_log)")
cols = c.fetchall()
print("sync_log columns:")
for col in cols:
    print(f"  {col}")

db.close()
