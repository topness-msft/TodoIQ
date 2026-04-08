import sqlite3, json

conn = sqlite3.connect('data/claudetodo.db')
conn.row_factory = sqlite3.Row
rows = conn.execute("""
    SELECT id, title, waiting_activity, updated_at
    FROM tasks
    WHERE status = 'suggested' AND waiting_activity IS NOT NULL
    ORDER BY updated_at DESC
    LIMIT 20
""").fetchall()

print(f"Already-checked tasks: {len(rows)}")
for r in rows:
    try:
        act = json.loads(r['waiting_activity'])
        status = act.get('status', '?')
        summary = act.get('summary', '?')[:80]
        checked_at = act.get('checked_at', '?')[:10]
    except:
        status = '?'
        summary = str(r['waiting_activity'])[:60]
        checked_at = '?'
    print(f"  #{r['id']} [{status}] {r['title'][:55]} | {checked_at}")
    print(f"     {summary}")
conn.close()
