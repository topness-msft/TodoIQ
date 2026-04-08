import sqlite3
import json
from datetime import datetime, timezone

conn = sqlite3.connect('data/claudetodo.db')
conn.row_factory = sqlite3.Row

# Search for potential matches for our 5 items
search_terms = [
    ('rodrigo', 'shark tank', 'offsite'),
    ('bill.spencer', 'spark tank', 'billsp'),
    ('foo', 'cape east', 'scale collab', 'foosh'),
    ('anne', 'krupke', 'agents at work'),
    ('greg', 'hurlman', 'kickstarter', 'mbr', 'fy26'),
]

for i, terms in enumerate(search_terms):
    conditions = " OR ".join([f"LOWER(source_id) LIKE '%{t}%' OR LOWER(title) LIKE '%{t}%'" for t in terms])
    query = f"SELECT id, status, source_id, title, action_type, priority FROM tasks WHERE {conditions}"
    rows = conn.execute(query).fetchall()
    print(f"\n=== Search {i+1}: {terms[0]} ===")
    for r in rows:
        print(f"  id={r['id']} status={r['status']} priority={r['priority']} action={r['action_type']}")
        print(f"    title: {r['title'][:80]}")
        print(f"    source_id: {r['source_id'] or 'none'}")

conn.close()
