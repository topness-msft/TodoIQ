import sqlite3, json
conn = sqlite3.connect('data\\claudetodo.db')
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
print('COUNT=' + str(len(rows)))
for r in rows:
    print(json.dumps({k: r[k] if r[k] is not None else '' for k in r.keys()}))
conn.close()
