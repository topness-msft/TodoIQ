import sqlite3, json
from datetime import datetime, timezone

conn = sqlite3.connect('data/claudetodo.db')
conn.row_factory = sqlite3.Row
now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

#  Validation Results 
# Item 1: "Acknowledge Lisa's schedule update"  SKIP (stale  1:1 already occurred today, acknowledging lateness is moot)
# Items 2+3+4: All from same "Agents Usage working sync" meeting about Seismic page  MERGE into 1 task
#   Tier: Group (all participants asked)  downgrade P2 to P3

#  Dedup check for merged item 
# source_id pattern: meeting::anfern@microsoft.com::agents usage working sync
source_id = "meeting::anfern@microsoft.com::agents usage working sync"
existing = conn.execute(
    "SELECT id, status, source_id, title FROM tasks WHERE source_id = ? OR LOWER(SUBSTR(title, 1, 40)) = LOWER(SUBSTR(?, 1, 40))",
    (source_id, "Contribute CAT/KS links and feedback to Agent Adoption Seismic page")
).fetchall()

created = 0
updated = 0
skipped = 1  # item 1 skipped as stale

if existing:
    ex = existing[0]
    if ex['status'] == 'dismissed':
        skipped += 1
        print(f"Skipped (dismissed): [{ex['id']}] {ex['title']}")
    else:
        # Augment
        conn.execute(
            "UPDATE tasks SET source_snippet = ?, priority = MIN(priority, ?), updated_at = ?, updated_count = COALESCE(updated_count, 0) + 1 WHERE id = ?",
            (
                "From Agents Usage working sync: Anne Fernando asked all participants to review the Agent Adoption Seismic page and add missing links by March 20. Assess whether CAT KS workshops, BATs, or agentic enablement materials should be linked. Share any referenced assets to Anne.",
                3, now, ex['id']
            )
        )
        updated += 1
        print(f"Updated existing task [{ex['id']}]")
else:
    # Create new merged task
    title = "Contribute CAT/KS links and feedback to Agent Adoption Seismic page"
    description = "Anne Fernando asked all participants in the Agents Usage working sync to review the Agent Adoption Seismic page and add missing or important links by March 20. Assess whether CAT KS workshops, BATs, or agentic enablement materials should be linked or summarized there, and share any referenced assets to Anne."
    key_people = json.dumps([
        {"name": "Anne Fernando", "email": "anfern@microsoft.com"},
        {"name": "Chantrelle Nielsen", "email": "Chantrelle.Nielsen@microsoft.com"}
    ])
    source_snippet = "From Agents Usage working sync: Anne Fernando asked all participants to review the Agent Adoption Seismic page and add missing links by March 20. Assess whether CAT KS workshops, BATs, or agentic enablement materials should be linked. Share any referenced assets to Anne."
    source_url = "https://teams.microsoft.com/l/meeting/details?eventId=AAMkADFkODcyODkwLTE0MjItNDVmOC05Yjk4LWYzYjRkMWNjMWRjOAFRAAgI3oO4IfNAAEYAAAAAqrb2XGnyTkKZt1FrbphKdQcAVG1l5TyEUkG5PqwnExmEhwAAAAABDQAAVG1l5TyEUkG5PqwnExmEhwAEnwcqzgAAEA%3d%3d"

    conn.execute(
        """INSERT INTO tasks (title, description, status, parse_status, priority,
           source_type, source_id, source_snippet, source_url, key_people,
           action_type, coaching_text, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (title, description, 'suggested', 'parsed', 3,
         'meeting', source_id, source_snippet, source_url, key_people,
         'prepare', None, now, now)
    )
    created += 1
    new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    print(f"Created new task [{new_id}]: {title}")

conn.commit()

#  Check for unparsed tasks (Step 4) 
unparsed = conn.execute(
    "SELECT COUNT(*) as cnt FROM tasks WHERE parse_status IN ('unparsed', 'queued')"
).fetchone()
print(f"\nUnparsed tasks: {unparsed['cnt']}")

conn.close()

# Summary counts
email_count = 0
chat_count = 0
meeting_found = 4  # 4 items from meetings (3 merged + 1 item from teams skipped)
meeting_created = created
meeting_updated = updated  
meeting_skipped = skipped

print(f"\n=== COUNTS ===")
print(f"email_count={email_count}")
print(f"chat_count={chat_count}")  
print(f"meeting_count={meeting_found}")
print(f"created={created}")
print(f"updated={updated}")
print(f"skipped={skipped}")
