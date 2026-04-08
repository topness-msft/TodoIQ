import sqlite3, json

conn = sqlite3.connect('data/claudetodo.db')
conn.row_factory = sqlite3.Row
rows = conn.execute("""
    SELECT id, title, key_people, source_type, source_id, created_at, status, user_notes
    FROM tasks
    WHERE status = 'waiting'
       OR (status = 'snoozed'
           AND waiting_activity LIKE '%out_of_office%'
           AND (json_extract(waiting_activity, '$.checked_at') IS NULL
                OR json_extract(waiting_activity, '$.checked_at') < datetime('now', '-20 hours')))
""").fetchall()

print(f"Total tasks: {len(rows)}")
print()

for r in rows:
    kp = r['key_people'] or ''
    # Parse key_people JSON array
    try:
        people = json.loads(kp) if kp else []
    except:
        people = [kp] if kp else []
    
    # Check for @WorkIQ questions in user_notes
    notes = r['user_notes'] or ''
    has_workiq = '@workiq' in notes.lower()
    workiq_marker = ' [HAS @WorkIQ]' if has_workiq else ''
    
    # Handle people as dicts or strings
    def person_name(p):
        if isinstance(p, dict):
            return p.get('name', str(p))
        return str(p)
    people_str = ', '.join(person_name(p) for p in people) if people else 'NO KEY PEOPLE'
    print(f"#{r['id']} [{r['status']}] {r['title'][:60]}")
    print(f"   People: {people_str} | Source: {r['source_type']} | Created: {r['created_at'][:10]}{workiq_marker}")
    print()

conn.close()
