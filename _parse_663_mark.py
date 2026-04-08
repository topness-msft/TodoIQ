import sqlite3
from datetime import datetime, timezone

conn = sqlite3.connect('data/claudetodo.db')
now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
for tid in [663, 664, 665, 666]:
    conn.execute("UPDATE tasks SET parse_status = 'parsing', updated_at = ? WHERE id = ?", (now, tid))
conn.commit()
conn.close()
print('Marked 663, 664, 665, 666 as parsing')
