"""Process WorkIQ scan results and insert/update tasks in TodoNess DB."""
import sqlite3
import json
from datetime import datetime, timezone

DB_PATH = 'data/claudetodo.db'
NOW = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

# ── Combined items from all three WorkIQ queries ──────────────────────────

items = [
    # === Meeting items (Phil/Aamer 1x1 - 2026-04-06) ===
    {
        'title': 'Email Aamer outlining scaling guidance support function',
        'description': 'From Phil/Aamer 1x1: Articulate what scaling guidance support looks like as a function. Draft and send to Aamer for alignment on structure and scope.',
        'source_type': 'meeting',
        'key_people': [{'name': 'Aamer Kaleem', 'email': 'aamer.kaleem@microsoft.com'}],
        'priority': 1,
        'subject': 'Phil/Aamer 1x1',
        'date': '2026-04-06',
        'action_type': 'prepare',
    },
    {
        'title': 'Send keynote panel participation note to Aamer',
        'description': 'From Phil/Aamer 1x1: Notify Aamer about Salesforce panel opportunity. Send details about the keynote panel participation ask.',
        'source_type': 'meeting',
        'key_people': [{'name': 'Aamer Kaleem', 'email': 'aamer.kaleem@microsoft.com'}],
        'priority': 2,
        'subject': 'Phil/Aamer 1x1',
        'date': '2026-04-06',
        'action_type': 'respond-email',
    },
    {
        'title': 'Request CAB workshop travel estimate from Steve Jeffery',
        'description': 'From Phil/Aamer 1x1: Follow up with Steve Jeffery to get the CAB workshop travel and expense estimate for budget planning.',
        'source_type': 'meeting',
        'key_people': [{'name': 'Steve Jeffery', 'email': 'steve.jeffery@microsoft.com'}],
        'priority': 2,
        'subject': 'Phil/Aamer 1x1',
        'date': '2026-04-06',
        'action_type': 'follow-up',
    },
    {
        'title': 'Conduct Rima Reyes reference checks with Bill and Raj',
        'description': 'From Phil/Aamer 1x1: Reach out to Bill Spencer and Raj for reference checks on Rima Reyes as part of evaluation.',
        'source_type': 'meeting',
        'key_people': [
            {'name': 'Bill Spencer', 'email': 'bill.spencer@microsoft.com'},
            {'name': 'Rima Reyes', 'email': 'rima.reyes@microsoft.com'},
        ],
        'priority': 2,
        'subject': 'Phil/Aamer 1x1',
        'date': '2026-04-06',
        'action_type': 'follow-up',
    },
    {
        'title': 'Schedule conference content prep call with Aamer and Manuela',
        'description': 'From Phil/Aamer 1x1: Block a prep call with Aamer Kaleem and Manuela Pichler to align on M365 conference content and framework.',
        'source_type': 'meeting',
        'key_people': [
            {'name': 'Aamer Kaleem', 'email': 'aamer.kaleem@microsoft.com'},
            {'name': 'Manuela Pichler', 'email': 'manuela.pichler@microsoft.com'},
        ],
        'priority': 2,
        'subject': 'Phil/Aamer 1x1',
        'date': '2026-04-06',
        'action_type': 'schedule-meeting',
    },
    {
        'title': 'Identify governance workshop candidate customers',
        'description': 'From Phil/Aamer 1x1: Research and identify candidate customers for governance workshop sessions. Prepare a shortlist.',
        'source_type': 'meeting',
        'key_people': [{'name': 'Aamer Kaleem', 'email': 'aamer.kaleem@microsoft.com'}],
        'priority': 2,
        'subject': 'Phil/Aamer 1x1',
        'date': '2026-04-06',
        'action_type': 'prepare',
    },
    {
        'title': 'Perform CPMS enabled vs disabled agent trend analysis',
        'description': 'From Phil/Aamer 1x1: Analyze CPMS data trends for enabled vs disabled agent metrics. Prepare findings for discussion.',
        'source_type': 'meeting',
        'key_people': [{'name': 'Aamer Kaleem', 'email': 'aamer.kaleem@microsoft.com'}],
        'priority': 2,
        'subject': 'Phil/Aamer 1x1',
        'date': '2026-04-06',
        'action_type': 'prepare',
    },
    {
        'title': 'Discuss technical role interest with Rima Reyes',
        'description': 'From Phil/Aamer 1x1: Schedule time to discuss technical role interest and potential swap opportunity with Rima Reyes.',
        'source_type': 'meeting',
        'key_people': [{'name': 'Rima Reyes', 'email': 'rima.reyes@microsoft.com'}],
        'priority': 3,
        'subject': 'Phil/Aamer 1x1',
        'date': '2026-04-06',
        'action_type': 'schedule-meeting',
    },
    {
        'title': "Reach out to Natasha Chopra's CSU team post-Greg call",
        'description': "From Phil/Aamer 1x1: Follow up with Natasha Chopra's CSU team after the Greg call to align on next steps.",
        'source_type': 'meeting',
        'key_people': [{'name': 'Natasha Chopra', 'email': 'natasha.chopra@microsoft.com'}],
        'priority': 3,
        'subject': 'Phil/Aamer 1x1',
        'date': '2026-04-06',
        'action_type': 'follow-up',
    },
    {
        'title': 'Set up biweekly 1:1 with Vic for FastTrack collaboration',
        'description': 'From Sync up meeting: Schedule recurring biweekly 1:1 with Vic to coordinate on FastTrack collaboration.',
        'source_type': 'meeting',
        'key_people': [{'name': 'Vic', 'email': 'vic@microsoft.com'}],
        'priority': 2,
        'subject': 'Sync up on M365 Conference & Frontier Patterns PPT',
        'date': '2026-04-07',
        'action_type': 'schedule-meeting',
    },
    {
        'title': 'Schedule follow-up on M365 Conference framework with Manuela and Saurabh',
        'description': 'From Sync up meeting: Schedule follow-up with Manuela Pichler and Saurabh to finalize M365 Conference framework details.',
        'source_type': 'meeting',
        'key_people': [
            {'name': 'Manuela Pichler', 'email': 'manuela.pichler@microsoft.com'},
            {'name': 'Saurabh', 'email': 'saurabh@microsoft.com'},
        ],
        'priority': 1,
        'subject': 'Sync up on M365 Conference & Frontier Patterns PPT',
        'date': '2026-04-07',
        'action_type': 'schedule-meeting',
    },

    # === Teams messages ===
    {
        'title': 'Respond to Akash Patel on Dataverse field conversion',
        'description': 'Akash Patel asked about Dataverse managed-to-unmanaged field conversion in Teams. This needs a direct response with guidance.',
        'source_type': 'chat',
        'key_people': [{'name': 'Akash Patel', 'email': 'akash.patel@microsoft.com'}],
        'priority': 1,
        'subject': 'Dataverse managed-to-unmanaged field conversion',
        'date': '2026-04-07',
        'action_type': 'general',
    },
    {
        'title': 'Confirm local CAT presenter for World Bank session',
        'description': 'Follow up to confirm a local CAT presenter for the World Bank session. Nikita Polyakov is involved in the coordination.',
        'source_type': 'chat',
        'key_people': [{'name': 'Nikita Polyakov', 'email': 'nikita.polyakov@microsoft.com'}],
        'priority': 2,
        'subject': 'World Bank session CAT presenter',
        'date': '2026-04-07',
        'action_type': 'follow-up',
    },
    {
        'title': "Review Steve Jeffery's recorded CAB run-through",
        'description': "Steve Jeffery shared a recorded CAB run-through in Teams. Review the recording and provide feedback.",
        'source_type': 'chat',
        'key_people': [{'name': 'Steve Jeffery', 'email': 'steve.jeffery@microsoft.com'}],
        'priority': 2,
        'subject': 'CAB run-through recording',
        'date': '2026-04-07',
        'action_type': 'general',
    },
    {
        'title': "Respond to Jack Cullinan's career discussion request",
        'description': "Jack Cullinan reached out in Teams requesting a career discussion. Schedule time or respond to coordinate.",
        'source_type': 'chat',
        'key_people': [{'name': 'Jack Cullinan', 'email': 'jack.cullinan@microsoft.com'}],
        'priority': 3,
        'subject': 'Career discussion request',
        'date': '2026-04-07',
        'action_type': 'schedule-meeting',
    },

    # === Awaiting response items ===
    {
        'title': 'Follow up with Manuela on MBR slide updates',
        'description': 'Sent Manuela Pichler a message about MBR slide updates and awaiting her response. Follow up if no reply.',
        'source_type': 'chat',
        'key_people': [{'name': 'Manuela Pichler', 'email': 'manuela.pichler@microsoft.com'}],
        'priority': 3,
        'subject': 'MBR Slide Updates',
        'date': '2026-04-07',
        'action_type': 'awaiting-response',
    },
    {
        'title': 'Follow up with Manuela on CAPE Guidance Helper role description',
        'description': 'Sent Manuela Pichler the draft CAPE Guidance Helper role description and awaiting feedback. Follow up if no reply.',
        'source_type': 'chat',
        'key_people': [{'name': 'Manuela Pichler', 'email': 'manuela.pichler@microsoft.com'}],
        'priority': 3,
        'subject': 'CAPE Guidance Helper Role Description',
        'date': '2026-04-07',
        'action_type': 'awaiting-response',
    },
    {
        'title': 'Follow up with Aamer on CAB travel expense routing',
        'description': 'Sent Aamer Kaleem a question about CAB travel expense routing and awaiting direction. Follow up if no reply.',
        'source_type': 'chat',
        'key_people': [{'name': 'Aamer Kaleem', 'email': 'aamer.kaleem@microsoft.com'}],
        'priority': 4,
        'subject': 'CAB Travel Expense Routing',
        'date': '2026-04-07',
        'action_type': 'awaiting-response',
    },
    {
        'title': 'Follow up with Joe Rafferty on governance webinar panel feasibility',
        'description': 'Sent Joe Rafferty a question about webinar panel feasibility for governance-blocked customers. Awaiting his response.',
        'source_type': 'chat',
        'key_people': [{'name': 'Joe Rafferty', 'email': 'joe.rafferty@microsoft.com'}],
        'priority': 4,
        'subject': 'Webinar Panel Feasibility for Governance-Blocked Customers',
        'date': '2026-04-07',
        'action_type': 'awaiting-response',
    },
    {
        'title': 'Follow up with Radhi on governance content cascade email',
        'description': 'Sent question to Bill and Radhi Agarwal about governance content cascade email authorship. Awaiting response.',
        'source_type': 'chat',
        'key_people': [
            {'name': 'Radhi Agarwal', 'email': 'radhi.agarwal@microsoft.com'},
        ],
        'priority': 3,
        'subject': 'Governance Content Cascade Email Author',
        'date': '2026-04-07',
        'action_type': 'awaiting-response',
    },
]

# ── Generate source_id ────────────────────────────────────────────────────
for item in items:
    first_email = item['key_people'][0]['email'].lower() if item['key_people'] else 'unknown'
    subj = item['subject'].lower().strip()
    for prefix in ['re:', 'fwd:', 're: ', 'fwd: ']:
        while subj.startswith(prefix):
            subj = subj[len(prefix):].strip()
    subj = subj[:50]
    item['source_id'] = f"{item['source_type']}::{first_email}::{subj}"

# ── In-batch dedup ────────────────────────────────────────────────────────
seen_source_ids = {}
seen_title_prefixes = {}
deduped = []
in_batch_dupes = 0

for item in items:
    sid = item['source_id']
    title_prefix = item['title'].lower()[:40]

    if sid in seen_source_ids:
        existing = seen_source_ids[sid]
        if item['priority'] < existing['priority']:
            deduped.remove(existing)
            deduped.append(item)
            seen_source_ids[sid] = item
            seen_title_prefixes[title_prefix] = item
        in_batch_dupes += 1
        continue

    if title_prefix in seen_title_prefixes:
        existing = seen_title_prefixes[title_prefix]
        if item['priority'] < existing['priority']:
            deduped.remove(existing)
            deduped.append(item)
            seen_source_ids[sid] = item
            seen_title_prefixes[title_prefix] = item
        in_batch_dupes += 1
        continue

    deduped.append(item)
    seen_source_ids[sid] = item
    seen_title_prefixes[title_prefix] = item

print(f"In-batch dedup: {len(items)} -> {len(deduped)} ({in_batch_dupes} dupes removed)")

# ── DB dedup and insert/update ────────────────────────────────────────────
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

created = 0
updated = 0
skipped = 0
email_count = 0
chat_count = 0
meeting_count = 0

for item in deduped:
    st = item['source_type']
    if st == 'email':
        email_count += 1
    elif st == 'chat':
        chat_count += 1
    elif st == 'meeting':
        meeting_count += 1

    # Pass 1: Exact match
    existing = conn.execute(
        "SELECT id, status, source_id, title, source_snippet, action_type FROM tasks "
        "WHERE source_id = ? OR LOWER(SUBSTR(title, 1, 40)) = LOWER(SUBSTR(?, 1, 40))",
        (item['source_id'], item['title'])
    ).fetchall()

    match = None

    if existing:
        match = existing[0]
    else:
        # Pass 2: Semantic match - same sender
        first_email = item['key_people'][0]['email'].lower() if item['key_people'] else ''
        sender_prefix = first_email.split('@')[0] if first_email else ''
        if sender_prefix:
            same_sender = conn.execute(
                "SELECT id, status, source_id, title, source_snippet, action_type FROM tasks "
                "WHERE source_id LIKE ?",
                ('%::' + sender_prefix + '%',)
            ).fetchall()

            item_title_lower = item['title'].lower()
            item_subj_lower = item['subject'].lower()
            for st_row in same_sender:
                st_title_lower = st_row['title'].lower() if st_row['title'] else ''
                st_snippet_lower = (st_row['source_snippet'] or '').lower()

                item_words = set(w for w in item_title_lower.split() if len(w) > 3)
                st_words = set(w for w in st_title_lower.split() if len(w) > 3)
                overlap = item_words & st_words

                if len(overlap) >= 2:
                    match = st_row
                    break

                if item_subj_lower and len(item_subj_lower) > 5:
                    if item_subj_lower in st_title_lower or item_subj_lower in st_snippet_lower:
                        match = st_row
                        break

    if match:
        if match['status'] == 'dismissed':
            skipped += 1
            print(f"  Skipped (dismissed): #{match['id']} {match['title'][:50]}")
            continue

        conn.execute(
            "UPDATE tasks SET source_snippet = ?, priority = MIN(priority, ?), "
            "updated_at = ?, updated_count = COALESCE(updated_count, 0) + 1 WHERE id = ?",
            (item['description'], item['priority'], NOW, match['id'])
        )
        updated += 1
        print(f"  Updated #{match['id']}: {match['title'][:50]}")
    else:
        key_people_json = json.dumps(item['key_people'])
        conn.execute(
            """INSERT INTO tasks (title, description, status, parse_status, priority,
               source_type, source_id, source_snippet, source_url, key_people,
               action_type, coaching_text, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (item['title'], item['description'], 'suggested', 'parsed', item['priority'],
             item['source_type'], item['source_id'], item['description'], None,
             key_people_json, item['action_type'], None, NOW, NOW)
        )
        created += 1
        print(f"  Created: {item['title'][:60]}")

conn.commit()

# ── Check for unparsed tasks ──────────────────────────────────────────────
unparsed = conn.execute(
    "SELECT COUNT(*) as cnt FROM tasks WHERE parse_status IN ('unparsed', 'queued')"
).fetchone()['cnt']
if unparsed:
    print(f"\n{unparsed} unparsed tasks found (run /todo-parse to enrich)")

# ── Log the sync ──────────────────────────────────────────────────────────
summary = json.dumps({
    'email': email_count,
    'chat': chat_count,
    'meeting': meeting_count,
    'created': created,
    'updated': updated,
    'skipped': skipped + in_batch_dupes,
})

conn.execute(
    "INSERT INTO sync_log (sync_type, result_summary, tasks_created, tasks_updated, synced_at) VALUES (?,?,?,?,?)",
    ('full_scan', summary, created, updated, NOW)
)
conn.commit()
conn.close()

# ── Summary ───────────────────────────────────────────────────────────────
total_found = len(deduped)
print(f"""
TodoNess Refresh Complete
{'=' * 54}
Source       | Found | Created | Updated | Skipped
{'=' * 54}
Email        |   {email_count:>3} |         |         |
Teams/Chat   |   {chat_count:>3} |         |         |
Meeting      |   {meeting_count:>3} |         |         |
{'=' * 54}
Total        |   {total_found:>3} |    {created:>3}  |    {updated:>3}  |    {skipped + in_batch_dupes:>3}

In-batch duplicates removed: {in_batch_dupes}
Dashboard: http://localhost:8766
""")
