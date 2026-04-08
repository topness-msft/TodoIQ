import sqlite3, json

conn = sqlite3.connect('data/claudetodo.db')
conn.row_factory = sqlite3.Row
rows = conn.execute("""
    SELECT id, title, key_people, source_type, source_id, created_at, waiting_activity, user_notes
    FROM tasks
    WHERE status = 'suggested'
    ORDER BY CASE WHEN waiting_activity IS NULL THEN 0 ELSE 1 END, created_at DESC
""").fetchall()

unchecked = [r for r in rows if not r['waiting_activity']]
checked = [r for r in rows if r['waiting_activity']]
print(f"Total: {len(rows)}, Unchecked: {len(unchecked)}, Checked: {len(checked)}")
print("--- UNCHECKED (first 20) ---")
for r in unchecked[:20]:
    people = ''
    if r['key_people']:
        try:
            plist = json.loads(r['key_people'])
            people = plist[0]['name'] if plist else 'none'
        except:
            people = r['key_people'][:30]
    print(f"#{r['id']} | {r['title'][:65]} | {people} | {r['created_at'][:10]}")
conn.close()
