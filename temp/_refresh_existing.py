import sqlite3, json

conn = sqlite3.connect('data/claudetodo.db')
conn.row_factory = sqlite3.Row
rows = conn.execute("SELECT id, status, source_id, title, source_snippet, key_people, action_type, source_date FROM tasks WHERE status NOT IN ('deleted')").fetchall()
for r in rows:
    sid = r["source_id"] or ""
    title = r["title"] or ""
    status = r["status"] or ""
    action = r["action_type"] or ""
    sdate = r["source_date"] or ""
    kp = r["key_people"] or ""
    print(f"{r['id']}|{status}|{sid}|{title}|{action}|{sdate}|{kp}")
conn.close()
