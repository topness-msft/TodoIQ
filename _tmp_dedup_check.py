import sqlite3

conn = sqlite3.connect('data/claudetodo.db')
conn.row_factory = sqlite3.Row

keywords = ['frontier firm', 'cape playbook', 'judson', 'seismic', 'fasttrack',
            'ft & cat', 'john wheat', 'workshop data', 'maturity model',
            'frontier transformation']
for kw in keywords:
    rows = conn.execute(
        'SELECT id, status, source_id, title, action_type FROM tasks '
        'WHERE LOWER(title) LIKE ? OR LOWER(source_snippet) LIKE ?',
        (f'%{kw}%', f'%{kw}%')
    ).fetchall()
    if rows:
        for r in rows:
            st = r['status']
            tid = r['id']
            act = str(r['action_type'])
            ttl = r['title'][:70]
            print(f'  KW={kw:25s} [{st:10s}] id={tid:4} action={act:15s} title={ttl}')

for em in ['mfirestone', 'alexander.hurtado', 'rituvashisth', 'jwheat']:
    rows = conn.execute(
        'SELECT id, status, source_id, title, action_type FROM tasks '
        'WHERE LOWER(source_id) LIKE ?',
        (f'%{em}%',)
    ).fetchall()
    if rows:
        print(f'\n--- {em} tasks ---')
        for r in rows:
            st = r['status']
            tid = r['id']
            src = str(r['source_id'])[:60]
            ttl = r['title'][:60]
            print(f'  [{st:10s}] id={tid:4} src={src:60s} title={ttl}')

conn.close()
