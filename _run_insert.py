import sqlite3
from datetime import datetime, timezone
import json

DB = 'data/claudetodo.db'
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

title = 'Align on use case identification targets with CPMs'
description = (
    "In the CAPE Agent builder triage (twice weekly) meeting (April 3, 2026), the team discussed "
    "updated targets of 200 customers and 600 agents and the need to identify three or more use cases "
    "per customer, referencing guidance from Saurabh Pant and Srini Raghavan. Follow-up alignment with "
    "CPMs may be required to ensure customer engagements meet the updated multi-use case threshold."
)
source_type = 'meeting'
source_id = 'meeting::spant@microsoft.com::cape agent builder triage (twice weekly)'
priority = 3
action_type = 'follow-up'
key_people = json.dumps([
    {"name": "Saurabh Pant", "email": "spant@microsoft.com"},
    {"name": "Taiki Yoshida", "email": "Taiki.Yoshida@microsoft.com"},
    {"name": "Adrian Maclean", "email": "Adrian.Maclean@microsoft.com"},
    {"name": "Kanika Ramji", "email": "kanikaramji@microsoft.com"},
])
source_snippet = description
now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

conn.execute(
    """INSERT INTO tasks (title, description, status, parse_status, priority,
       source_type, source_id, source_snippet, source_url, key_people,
       action_type, coaching_text, created_at, updated_at)
       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
    (title, description, 'suggested', 'parsed', priority,
     source_type, source_id, source_snippet, None, key_people,
     action_type, None, now, now)
)
conn.commit()
task_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
print(f"Created task id={task_id}: {title}")
conn.close()
