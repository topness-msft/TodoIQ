import sqlite3
conn = sqlite3.connect('C:/Users/phtopnes/claude/projects/ClaudeTodo/data/claudetodo.db')
conn.row_factory = sqlite3.Row
tasks = conn.execute(
    "SELECT id, raw_input, title, description, key_people, action_type, user_notes, source_type, parse_status "
    "FROM tasks "
    "WHERE parse_status IN ('unparsed', 'queued') AND status NOT IN ('deleted', 'completed')"
).fetchall()
for t in tasks:
    print('---TASK---')
    for k in t.keys():
        print(f'{k}: {t[k]}')
print(f'Total: {len(tasks)}')
conn.close()
