import sqlite3
from datetime import datetime, timezone

conn = sqlite3.connect('data/claudetodo.db')
now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
conn.execute("UPDATE tasks SET parse_status = 'parsing', updated_at = ? WHERE id = 780", (now,))
conn.commit()
conn.close()
print('Marked 780 as parsing')
