import sqlite3, json
from datetime import datetime, timezone

conn = sqlite3.connect('data/claudetodo.db')
conn.row_factory = sqlite3.Row

# Item 1 dedup
source_id_1 = 'email::dustinc@pshummingbird.com::catching up + ai & copilot at hummingbird'
title_1 = 'Follow up with Dustin Caudell on meeting time confirmation'

exact1 = conn.execute(
    "SELECT id, status, source_id, title, source_snippet FROM tasks WHERE source_id = ? OR LOWER(SUBSTR(title, 1, 40)) = LOWER(SUBSTR(?, 1, 40))",
    (source_id_1, title_1)
).fetchall()
print(f"Item 1 exact matches: {len(exact1)}")
for r in exact1:
    print(f"  id={r['id']} status={r['status']} title={r['title'][:60]}")

sender1_tasks = conn.execute(
    "SELECT id, status, source_id, title, source_snippet, action_type FROM tasks WHERE source_id LIKE ?",
    ('%::dustinc%',)
).fetchall()
print(f"Item 1 sender tasks: {len(sender1_tasks)}")
for r in sender1_tasks:
    print(f"  id={r['id']} status={r['status']} title={r['title'][:60]} action={r['action_type']}")

# Item 2 dedup
source_id_2 = 'email::spant@microsoft.com::meeting summary: mbr cross-team | m365 copilot'
title_2 = 'Follow up with Saurabh Pant and Dan Stevenson on Agentic Transformation Playbook clarification'

exact2 = conn.execute(
    "SELECT id, status, source_id, title, source_snippet FROM tasks WHERE source_id = ? OR LOWER(SUBSTR(title, 1, 40)) = LOWER(SUBSTR(?, 1, 40))",
    (source_id_2, title_2)
).fetchall()
print(f"Item 2 exact matches: {len(exact2)}")
for r in exact2:
    print(f"  id={r['id']} status={r['status']} title={r['title'][:60]}")

sender2_tasks = conn.execute(
    "SELECT id, status, source_id, title, source_snippet, action_type FROM tasks WHERE source_id LIKE ?",
    ('%::spant%',)
).fetchall()
print(f"Item 2 sender tasks (spant): {len(sender2_tasks)}")
for r in sender2_tasks:
    print(f"  id={r['id']} status={r['status']} title={r['title'][:80]} action={r['action_type']}")

sender2b_tasks = conn.execute(
    "SELECT id, status, source_id, title, source_snippet, action_type FROM tasks WHERE source_id LIKE ?",
    ('%::dan.stevenson%',)
).fetchall()
print(f"Item 2 sender tasks (dan.stevenson): {len(sender2b_tasks)}")
for r in sender2b_tasks:
    print(f"  id={r['id']} status={r['status']} title={r['title'][:80]} action={r['action_type']}")

conn.close()
