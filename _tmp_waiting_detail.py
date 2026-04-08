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

for r in rows:
    kp = r['key_people'] or ''
    try:
        people = json.loads(kp) if kp else []
    except:
        people = [kp] if kp else []
    
    def person_name(p):
        if isinstance(p, dict):
            return p.get('name', str(p))
        return str(p)
    
    people_names = [person_name(p) for p in people]
    
    # Determine target person
    source_type = r['source_type'] or 'manual'
    source_id = r['source_id'] or ''
    target = None
    
    if source_type != 'manual' and '::' in source_id:
        parts = source_id.split('::')
        if len(parts) >= 2:
            originator = parts[1]
            # Find matching person in key_people
            for name in people_names:
                if originator.lower() in name.lower() or name.lower() in originator.lower():
                    target = name
                    break
            if not target:
                target = originator
    
    if not target and people_names:
        target = people_names[0]
    
    # Check for @WorkIQ questions
    notes = r['user_notes'] or ''
    workiq_questions = []
    lines = notes.split('\n')
    for i, line in enumerate(lines):
        if '@workiq' in line.lower():
            # Check if next line is an answer (starts with  →)
            has_answer = (i + 1 < len(lines) and lines[i + 1].strip().startswith('→'))
            if not has_answer:
                workiq_questions.append(line.strip())
    
    print(f"TASK #{r['id']}|{r['status']}|{r['title'][:70]}|TARGET:{target or 'NONE'}|SOURCE:{source_type}|SRC_ID:{source_id[:60]}|CREATED:{r['created_at'][:10]}|WORKIQ_Q:{json.dumps(workiq_questions)}")

conn.close()
