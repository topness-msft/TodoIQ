import json, sys
tasks = []
with open('_tmp_waiting.py', 'r') as f:
    pass  # just checking file exists

# Re-run the query directly
import sqlite3
conn = sqlite3.connect('data/claudetodo.db')
conn.row_factory = sqlite3.Row
rows = conn.execute("""
    SELECT id, title, description, key_people, source_type, source_id, created_at, status, waiting_activity, user_notes
    FROM tasks
    WHERE status = 'waiting'
       OR (status = 'snoozed'
           AND waiting_activity LIKE '%out_of_office%'
           AND (json_extract(waiting_activity, '$.checked_at') IS NULL
                OR json_extract(waiting_activity, '$.checked_at') < datetime('now', '-20 hours')))
""").fetchall()
print(f"TASK_COUNT:{len(rows)}")
for r in rows:
    kp = r['key_people'] or ''
    st = r['source_type'] or 'manual'
    sid = r['source_id'] or ''
    un = r['user_notes'] or ''
    desc = r['description'] or ''
    wa = r['waiting_activity'] or ''
    print(f"TASK|{r['id']}|{r['title']}|{kp}|{st}|{sid}|{r['created_at']}|{r['status']}|{wa[:100]}|{un[:200]}")
    print(f"DESC|{r['id']}|{desc[:200]}")
conn.close()
