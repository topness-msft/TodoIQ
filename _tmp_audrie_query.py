import sqlite3, json
conn = sqlite3.connect('data/claudetodo.db')
conn.row_factory = sqlite3.Row
# Check task_context columns
cols = conn.execute('PRAGMA table_info(task_context)').fetchall()
print('task_context columns:', [c['name'] for c in cols])
# Get recent context entries for task 251
ctx = conn.execute('SELECT * FROM task_context WHERE task_id = 251 ORDER BY id DESC LIMIT 3').fetchall()
print('Context entries for task 251:', len(ctx))
for r in ctx:
    d = dict(r)
    if 'raw_payload' in d and d['raw_payload']:
        d['raw_payload'] = d['raw_payload'][:400]
    print(d)
conn.close()
