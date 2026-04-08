import sqlite3
import json
from datetime import datetime, timezone

conn = sqlite3.connect('data/claudetodo.db')
conn.row_factory = sqlite3.Row

# Search for tasks matching key people/topics from our new items
searches = [
    ('steve.jeffery', 'Steve Jeffery'),
    ('manuela.pichler', 'Manuela Pichler'),
    ('grhurl', 'Greg Hurlman'),
    ('darian', 'Darian'),
    ('cab', 'CAB'),
    ('mbr', 'MBR'),
    ('agent excellence', 'AgentExcellence'),
    ('bat scaling', 'BAT'),
    ('bat deliver', 'BAT-deliver'),
    ('workshop', 'Workshop'),
    ('kickstarter', 'Kickstarter'),
    ('program simplif', 'ProgramSimplify'),
]

seen_ids = set()
for term, label in searches:
    rows = conn.execute(
        '''SELECT id, status, source_id, title, action_type, priority
           FROM tasks
           WHERE (LOWER(source_id) LIKE ? OR LOWER(title) LIKE ? OR LOWER(source_snippet) LIKE ?)
           AND status NOT IN ('deleted')
           ORDER BY id DESC LIMIT 10''',
        (f'%{term}%', f'%{term}%', f'%{term}%')
    ).fetchall()
    for r in rows:
        tid = r['id']
        if tid not in seen_ids:
            seen_ids.add(tid)
            src = r['source_id'][:70] if r['source_id'] else 'None'
            title = r['title'][:70]
            print(f"{label}: ID={tid} | {r['status']} | p={r['priority']} | {r['action_type']} | {title} | src={src}")

conn.close()
