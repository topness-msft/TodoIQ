import sqlite3, json

conn = sqlite3.connect('data/claudetodo.db')
conn.row_factory = sqlite3.Row

people = ['aamer', 'billspe', 'bill.spencer', 'rajgopal', 'jwheat', 'dnastvogel', 'manuela', 'wendy', 'mudit', 'dana.bourque']
for p in people:
    rows = conn.execute(
        "SELECT id, status, title, source_id, priority FROM tasks "
        "WHERE status NOT IN ('deleted') AND (source_id LIKE ? OR key_people LIKE ? OR LOWER(title) LIKE ?)",
        (f'%{p}%', f'%{p}%', f'%{p}%')
    ).fetchall()
    if rows:
        print(f"\n=== {p} ===")
        for r in rows:
            sid = (r["source_id"] or "")[:80]
            print(f"  [{r['status']:12}] P{r['priority']} #{r['id']:3} {r['title'][:65]}")
            print(f"               src: {sid}")

conn.close()
