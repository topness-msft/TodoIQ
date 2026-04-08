import sqlite3, json
conn = sqlite3.connect('data/claudetodo.db')
conn.row_factory = sqlite3.Row
rows = conn.execute("""
    SELECT id, title, description, key_people, source_type, source_id, created_at, waiting_activity, user_notes
    FROM tasks
    WHERE status = 'suggested'
    ORDER BY CASE WHEN waiting_activity IS NULL THEN 0 ELSE 1 END, created_at DESC
""").fetchall()
for r in rows:
    print(json.dumps({'id': r['id'], 'title': r['title'], 'description': r['description'] or '', 'key_people': r['key_people'] or '', 'source_type': r['source_type'] or 'manual', 'source_id': r['source_id'] or '', 'created_at': r['created_at'], 'waiting_activity': r['waiting_activity'] or '', 'user_notes': r['user_notes'] or ''}))
conn.close()
