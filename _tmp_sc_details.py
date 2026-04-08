import sqlite3, json
conn = sqlite3.connect('data/claudetodo.db')
conn.row_factory = sqlite3.Row
rows = conn.execute("SELECT id, title, description, key_people, source_type, source_id, created_at, user_notes FROM tasks WHERE status = 'suggested' AND (waiting_activity IS NULL OR waiting_activity = '') ORDER BY created_at DESC").fetchall()
for r in rows:
    print('---TASK---')
    print(json.dumps({
        'id': r['id'],
        'title': r['title'],
        'desc': (r['description'] or '')[:200],
        'kp': r['key_people'] or '',
        'src_type': r['source_type'] or 'manual',
        'src_id': r['source_id'] or '',
        'created': r['created_at'],
        'notes': r['user_notes'] or ''
    }, indent=2))
conn.close()
