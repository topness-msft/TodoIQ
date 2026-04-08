import sqlite3, json
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
for r in rows:
    print(json.dumps({'id': r['id'], 'title': r['title'], 'description': r['description'] or '', 'key_people': r['key_people'] or '', 'source_type': r['source_type'] or 'manual', 'source_id': r['source_id'] or '', 'created_at': r['created_at'], 'status': r['status'], 'waiting_activity': r['waiting_activity'] or '', 'user_notes': r['user_notes'] or ''}))
if not rows:
    print("NO_WAITING_TASKS")
conn.close()
