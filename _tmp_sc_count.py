import sqlite3, json
conn = sqlite3.connect('data/claudetodo.db')
conn.row_factory = sqlite3.Row
rows = conn.execute("SELECT id, title, key_people, source_type, source_id, created_at, waiting_activity, user_notes FROM tasks WHERE status = 'suggested' ORDER BY CASE WHEN waiting_activity IS NULL THEN 0 ELSE 1 END, created_at DESC").fetchall()
print(f'Total: {len(rows)}')
for r in rows:
    wa = 'unchecked' if not r['waiting_activity'] else 'checked'
    print(f'  #{r["id"]} [{wa}] {r["title"][:80]}')
conn.close()
