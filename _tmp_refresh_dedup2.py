import sqlite3, json

conn = sqlite3.connect('data/claudetodo.db')
conn.row_factory = sqlite3.Row

patterns = [
    '%vasavi.bhaviri%', '%spant%',
    '%sarah.goodwin%', '%gobig%', '%cape%', '%hpa%', '%kickstarter%',
    '%msheard%', '%matt.sheard%', '%fasttrack%', '%ebc%design%',
    '%adrian.maclean%', '%connects%workback%', '%fy26%connects%',
    '%mudit.agarwal%', '%connect%perspective%', '%connect%feedback%',
    '%darian%', '%webinar%streamlin%',
]

seen = set()
for p in patterns:
    rows = conn.execute(
        "SELECT id, status, source_id, title, action_type FROM tasks WHERE source_id LIKE ? OR LOWER(title) LIKE ?",
        (p, p)
    ).fetchall()
    for r in rows:
        tid = r['id']
        if tid not in seen:
            seen.add(tid)
            src = r['source_id'] or 'None'
            title = r['title'][:90] if r['title'] else 'None'
            act = r['action_type'] or 'None'
            st = r['status'] or 'None'
            print(f"ID={tid} | STATUS={st} | ACTION={act} | SRC={src} | TITLE={title}")

conn.close()
