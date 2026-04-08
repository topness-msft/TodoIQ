import sqlite3, json

conn = sqlite3.connect('data/claudetodo.db')
conn.row_factory = sqlite3.Row
rows = conn.execute(
    "SELECT id, status, source_id, title, source_snippet, action_type, key_people FROM tasks WHERE status NOT IN ('completed', 'archived')"
).fetchall()
for r in rows:
    print(f"{r['id']}|{r['status']}|{r['source_id']}|{r['title'][:60]}|{r['action_type']}")
print(f"---TOTAL: {len(rows)}")
conn.close()
