import sqlite3
conn = sqlite3.connect('data/claudetodo.db')
rows = conn.execute("SELECT id, status, source_id, title FROM tasks WHERE status NOT IN ('deleted') ORDER BY id DESC").fetchall()
for r in rows:
    sid = r[2] or ""
    title = r[3] or ""
    line = f"{r[0]}|{r[1]}|{sid}|{title}"
    print(line[:200])
conn.close()
