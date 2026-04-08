import sqlite3, json

conn = sqlite3.connect('data/claudetodo.db')
conn.row_factory = sqlite3.Row

queries = [
    ('v-mohamedsl', 'cat live studios'),
    ('v-mohamedsl', 'video'),
    ('vasavi.bhaviri', 'webinar'),
    ('vasavi.bhaviri', 'reorg'),
    ('sarah.goodwin', 'steerco'),
    ('sarah.goodwin', 'product gap'),
    ('greg.hurlman', 'cab'),
    ('mudit.agarwal', 'connect'),
]

for sender, topic in queries:
    rows = conn.execute(
        'SELECT id, status, source_id, title, priority, action_type FROM tasks WHERE source_id LIKE ?',
        ('%' + sender + '%',)
    ).fetchall()
    print(f'\n=== Sender: {sender} (topic: {topic}) ===')
    for r in rows:
        title_lower = r['title'].lower()
        sid_lower = (r['source_id'] or '').lower()
        status = r['status']
        pri = r['priority']
        tid = r['id']
        sid = r['source_id']
        ttl = r['title'][:80]
        if topic.lower() in title_lower or topic.lower() in sid_lower:
            print(f'  MATCH [{status:10s}] P{pri} id={tid} | {sid} | {ttl}')
        else:
            print(f'  other [{status:10s}] P{pri} id={tid} | {sid} | {ttl}')

conn.close()
