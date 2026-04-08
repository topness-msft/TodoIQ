import sqlite3

db = sqlite3.connect('data/claudetodo.db')
c = db.cursor()

print('=== POST-REFRESH DB STATE ===')
print()

# Task count
c.execute('SELECT COUNT(*) FROM tasks')
task_count = c.fetchone()[0]
print(f'Total tasks: {task_count}')

# Tasks by parse_status
c.execute('SELECT parse_status, COUNT(*) FROM tasks GROUP BY parse_status')
parse_rows = c.fetchall()
print(f'Tasks by parse_status: {dict(parse_rows)}')

# Tasks by status
c.execute('SELECT status, COUNT(*) FROM tasks GROUP BY status')
status_rows = c.fetchall()
print(f'Tasks by status: {dict(status_rows)}')

# Tasks by source_type
c.execute('SELECT source_type, COUNT(*) FROM tasks WHERE source_type IS NOT NULL GROUP BY source_type')
source_rows = c.fetchall()
print(f'Tasks by source_type: {dict(source_rows)}')

print()

# Most recent sync_log
c.execute('SELECT COUNT(*) FROM sync_log')
sync_count = c.fetchone()[0]
print(f'Total sync_log entries: {sync_count}')

c.execute('SELECT id, sync_type, result_summary, tasks_created, tasks_updated, synced_at FROM sync_log ORDER BY synced_at DESC LIMIT 5')
rows = c.fetchall()
print(f'\nMost recent sync_log entries (last 5):')
for row in rows:
    print(f'  ID={row[0]}, type={row[1]}, result={row[2]}, created={row[3]}, updated={row[4]}, ts={row[5]}')

db.close()
