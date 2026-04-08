import sqlite3
import json
from datetime import datetime, timezone

conn = sqlite3.connect('data/claudetodo.db')
conn.row_factory = sqlite3.Row

# Get all non-completed tasks for dedup
rows = conn.execute(
    "SELECT id, status, source_id, title, source_snippet, action_type, key_people FROM tasks WHERE status != 'completed'"
).fetchall()

for r in rows:
    print(f"{r['id']}|{r['status']}|{r['source_id'] or ''}|{r['title'][:60]}|{r['action_type'] or ''}")

conn.close()
