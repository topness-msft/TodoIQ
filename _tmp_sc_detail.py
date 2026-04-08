import sqlite3, json

conn = sqlite3.connect('data/claudetodo.db')
conn.row_factory = sqlite3.Row
rows = conn.execute("""
    SELECT id, title, description, key_people, source_type, source_id, created_at, waiting_activity, user_notes
    FROM tasks
    WHERE status = 'suggested' AND waiting_activity IS NULL
    ORDER BY created_at DESC
""").fetchall()

for r in rows:
    people = []
    if r['key_people']:
        try:
            people = json.loads(r['key_people'])
        except:
            pass
    print(json.dumps({
        'id': r['id'],
        'title': r['title'],
        'description': (r['description'] or '')[:200],
        'key_people': people,
        'source_type': r['source_type'] or 'manual',
        'source_id': r['source_id'] or '',
        'created_at': r['created_at'],
        'user_notes': r['user_notes'] or ''
    }))
conn.close()
