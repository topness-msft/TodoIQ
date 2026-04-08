"""Process WorkIQ scan results: validate, dedup, insert/update tasks."""
import sqlite3
import json
from datetime import datetime, timezone

DB = 'data/claudetodo.db'

def title_tokens(t):
    stop = {'a','an','the','to','for','of','on','in','at','and','or','with','my','re','fwd'}
    return set(w for w in t.lower().split() if w not in stop and len(w) > 1)

def fuzzy_title_match(t1, t2, threshold=0.5):
    s1, s2 = title_tokens(t1), title_tokens(t2)
    if not s1 or not s2:
        return False
    return len(s1 & s2) / len(s1 | s2) >= threshold

# All items extracted from WorkIQ responses
items = [
    # Step 2a: Teams + Meetings
    {
        "title": "Review updated video in CAT Live Studios and provide feedback",
        "description": "Simo Slaoui shared an updated video in the CAT Live Studios Teams chat and explicitly addressed you by name. The updated edit has been posted via Frame.io link and likely requires your review or approval before final export or publishing. Review the update and respond with feedback or sign-off so production can proceed.",
        "source_type": "chat",
        "key_people": [{"name": "Simo Slaoui", "email": "v-mohamedsl@microsoft.com"}, {"name": "Mehdi Slaoui Andaloussi", "email": ""}],
        "priority": 2,
        "action_type": "follow-up",
        "source_snippet": "Simo Slaoui shared an updated video in the CAT Live Studios Teams chat and explicitly addressed you by name. The updated edit has been posted via Frame.io link and likely requires your review or approval before final export or publishing.",
        "source_url": "https://teams.microsoft.com/l/message/19:7f849af8c0964935b6543de01ae5549e@thread.v2/1775595588992?context=%7B%22contextType%22:%22chat%22%7D",
        "source_id": "chat::v-mohamedsl@microsoft.com::cat live studios",
        "tier": "direct",
    },
    {
        "title": "Provide input on Agent TAM activation approach for CAPE G2G planning",
        "description": "Aamer Kaleem asked for your input on activating around HPAs and deploying agents to CAD customers, tied to the stated 30% deployment goal. This is a direct request for your perspective on activation strategy for CAPE. Respond with guidance on how CAPE can drive activation toward this outcome.",
        "source_type": "chat",
        "key_people": [{"name": "Aamer Kaleem", "email": "Aamer.Kaleem@microsoft.com"}, {"name": "Sarah Goodwin", "email": "Sarah.Goodwin@microsoft.com"}],
        "priority": 2,
        "action_type": "follow-up",
        "source_snippet": "Aamer Kaleem asked for input on Agent TAM activation approach for CAPE G2G planning, tied to 30% deployment goal across CAD customers.",
        "source_url": "https://teams.microsoft.com/l/message/19:meeting_ZGQ5ZjhiZWUtOTY0Ni00MDAxLWIyM2MtNjVmNmVlMmFmMTQ2@thread.v2/1775583310069?context=%7B%22contextType%22:%22chat%22%7D",
        "source_id": "chat::aamer.kaleem@microsoft.com::agent gobig & cape agent mau g2g planning",
        "tier": "direct",
    },
    {
        "title": "Respond with local speaker recommendations for World Bank Apps & Agents workshop",
        "description": "Nikita Polyakov tagged you asking if you know anyone locally who could present/demo Agents and Apps to ~100 attendees at the World Bank onsite April 28-29. This is tied to a FY26 renewal-stage customer with ~27K Apps seats. Respond with speaker suggestions or connect Nikita with appropriate field contacts.",
        "source_type": "chat",
        "key_people": [{"name": "Nikita Polyakov", "email": "Nikita.Polyakov@microsoft.com"}],
        "priority": 2,
        "action_type": "follow-up",
        "source_snippet": "Nikita Polyakov tagged you asking for local speaker recommendations for World Bank Apps & Agents workshop (April 28-29, ~100 attendees, FY26 renewal).",
        "source_url": "https://teams.microsoft.com/l/message/19:eefaee9f89a44035b62829dc35032383@thread.v2/1775579758533?context=%7B%22contextType%22:%22chat%22%7D",
        "source_id": "chat::nikita.polyakov@microsoft.com::world bank onsite dc april 28th/29th",
        "tier": "direct",
    },
    {
        "title": "Schedule checkpoint meeting for Thursday on customer stories & deck development",
        "description": "From the Sync on M365 Conference Deck meeting, you were assigned to schedule a checkpoint meeting for Thursday to review progress on customer stories and deck development for the Agentic Maturity Model presentation. Place this checkpoint on calendars with Aamer, Saurabh, Bill, and Manuela.",
        "source_type": "meeting",
        "key_people": [{"name": "Aamer Kaleem", "email": "Aamer.Kaleem@microsoft.com"}, {"name": "Saurabh Pant", "email": "spant@microsoft.com"}, {"name": "Bill Spencer", "email": "billspe@microsoft.com"}, {"name": "Manuela Pichler", "email": "Manuela.Pichler@microsoft.com"}],
        "priority": 1,
        "action_type": "schedule-meeting",
        "source_snippet": "Assigned in meeting: schedule Thursday checkpoint to review customer stories and deck development for Agentic Maturity Model presentation.",
        "source_url": None,
        "source_id": "meeting::aamer.kaleem@microsoft.com::sync on m365 conference deck w/ saurabh",
        "tier": "direct",
    },
    # Step 2b: Awaiting Response
    {
        "title": "Follow up with Simo on caption file update",
        "description": "You asked Simo Slaoui for an updated caption file for the CAT Live Srini video. This request was sent this morning in the CAT Live Studios Teams chat. No subsequent reply confirming the caption file has been provided.",
        "source_type": "chat",
        "key_people": [{"name": "Simo Slaoui", "email": "v-mohamedsl@microsoft.com"}],
        "priority": 4,
        "action_type": "awaiting-response",
        "source_snippet": "Requested updated caption file for CAT Live Srini video from Simo. No reply yet.",
        "source_url": "https://teams.microsoft.com/l/message/19:7f849af8c0964935b6543de01ae5549e@thread.v2/1775655928909?context=%7B%22contextType%22:%22chat%22%7D",
        "source_id": "chat::v-mohamedsl@microsoft.com::cat live studios",
        "tier": "direct",
    },
    {
        "title": "Follow up with Simo on audio sync check and packaging",
        "description": "You asked Simo to check the audio sync on the first 30 seconds of the Gary video and to package the captions and thumbnail. Simo confirmed the intro sync but hasn't confirmed packaging of captions and thumbnails.",
        "source_type": "chat",
        "key_people": [{"name": "Simo Slaoui", "email": "v-mohamedsl@microsoft.com"}],
        "priority": 3,
        "action_type": "awaiting-response",
        "source_snippet": "Asked Simo to check audio sync on Gary video and package captions/thumbnail. Sync confirmed but packaging not yet confirmed.",
        "source_url": "https://teams.microsoft.com/l/message/19:7f849af8c0964935b6543de01ae5549e@thread.v2/1775655928909?context=%7B%22contextType%22:%22chat%22%7D",
        "source_id": "chat::v-mohamedsl@microsoft.com::cat live studios",
        "tier": "direct",
    },
    {
        "title": "Follow up with Mehdi on production calendar format",
        "description": "You asked Mehdi Slaoui Andaloussi what format would work for a production calendar and whether scheduled record dates and target launch dates should be tracked. The reply addressed ideal release timing but did not answer your questions about format or key tracked dates.",
        "source_type": "chat",
        "key_people": [{"name": "Mehdi Slaoui Andaloussi", "email": ""}],
        "priority": 4,
        "action_type": "awaiting-response",
        "source_snippet": "Asked Mehdi about production calendar format and what dates to track. Reply addressed timing but not format question.",
        "source_url": "https://teams.microsoft.com/l/message/19:7f849af8c0964935b6543de01ae5549e@thread.v2/1775655928909?context=%7B%22contextType%22:%22chat%22%7D",
        "source_id": "chat::mehdi slaoui andaloussi::cat live studios",
        "tier": "direct",
    },
    # Step 2c: Flagged Emails
    {
        "title": "Provide Connect cycle feedback to Mudit Agarwal",
        "description": "Mudit Agarwal is requesting your feedback as part of his Connect cycle on his engineering ownership and platform contributions across Power Up and Power CAT Kickstarter initiatives. Review his listed impact areas and respond with your perspective on platform scalability and enterprise delivery readiness.",
        "source_type": "email",
        "key_people": [{"name": "Mudit Agarwal", "email": "Mudit.Agarwal@microsoft.com"}],
        "priority": 3,
        "action_type": "respond-email",
        "source_snippet": "Mudit Agarwal requesting Connect cycle feedback on engineering ownership and platform contributions across Power Up and Power CAT Kickstarter initiatives.",
        "source_url": "https://outlook.office365.com/owa/?ItemID=AAMkADFkODcyODkwLTE0MjItNDVmOC05Yjk4LWYzYjRkMWNjMWRjOABGAAAAAACqtvZcafJOQpm3UWtumEp1BwBUbWXlPIRSQbk%2brCcTGYSHAAAAAAEMAABUbWXlPIRSQbk%2brCcTGYSHABN7%2flzhAAA%3d&exvsurl=1&viewmodel=ReadMessageItem",
        "source_id": "email::mudit.agarwal@microsoft.com::requesting connect perspective",
        "tier": "direct",
    },
]

# === IN-BATCH DEDUP ===
# Group by source_id, keep highest priority
from collections import defaultdict
groups = defaultdict(list)
for item in items:
    groups[item['source_id']].append(item)

deduped = []
in_batch_removed = 0
for sid, group in groups.items():
    group.sort(key=lambda x: x['priority'])
    deduped.append(group[0])
    in_batch_removed += len(group) - 1

# Fuzzy title match among remaining
final_items = []
for i, item in enumerate(deduped):
    is_dup = False
    for j, kept in enumerate(final_items):
        if fuzzy_title_match(item['title'], kept['title']):
            in_batch_removed += 1
            is_dup = True
            if item['priority'] < kept['priority']:
                final_items[j] = item
            break
    if not is_dup:
        final_items.append(item)

print(f"In-batch dedup: {len(items)} raw → {len(final_items)} unique ({in_batch_removed} removed)")

# === DB DEDUP + INSERT/UPDATE ===
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

created = 0
updated = 0
skipped = 0
source_counts = {"email": 0, "chat": 0, "meeting": 0}
created_by_source = {"email": 0, "chat": 0, "meeting": 0}
updated_by_source = {"email": 0, "chat": 0, "meeting": 0}
skipped_by_source = {"email": 0, "chat": 0, "meeting": 0}

for item in final_items:
    st = item['source_type']
    source_counts[st] = source_counts.get(st, 0) + 1

    sid = item['source_id']
    title = item['title']

    # Pass 1: exact match on source_id or title prefix
    existing = conn.execute(
        "SELECT id, status, source_id, title, source_snippet FROM tasks WHERE source_id = ? OR LOWER(SUBSTR(title, 1, 40)) = LOWER(SUBSTR(?, 1, 40))",
        (sid, title)
    ).fetchall()

    # If no exact match, try fuzzy token-overlap against same-sender tasks
    if not existing:
        first_email = ""
        for p in item['key_people']:
            if p.get('email'):
                first_email = p['email'].lower()
                break
        if first_email:
            sender_prefix = first_email.split('@')[0]
            candidates = conn.execute(
                "SELECT id, status, source_id, title, source_snippet FROM tasks WHERE source_id LIKE ?",
                ('%::' + sender_prefix + '::%',)
            ).fetchall()
            existing = [c for c in candidates if fuzzy_title_match(title, c['title'])]

    # Pass 2: semantic match if still no match
    if not existing:
        first_email = ""
        for p in item['key_people']:
            if p.get('email'):
                first_email = p['email'].lower()
                break
        if first_email:
            sender_prefix = first_email.split('@')[0]
            same_sender_tasks = conn.execute(
                "SELECT id, status, source_id, title, source_snippet, action_type FROM tasks WHERE source_id LIKE ?",
                ('%::' + sender_prefix + '::%',)
            ).fetchall()
            # Semantic check: same person + same topic keywords
            for sst in same_sender_tasks:
                # Check if titles share topic keywords
                t1_tokens = title_tokens(title)
                t2_tokens = title_tokens(sst['title'])
                # More lenient: if 2+ meaningful tokens overlap, consider it a match
                overlap = t1_tokens & t2_tokens
                if len(overlap) >= 2:
                    existing = [sst]
                    break

    if existing:
        ex = existing[0]
        if ex['status'] == 'dismissed':
            skipped += 1
            skipped_by_source[st] = skipped_by_source.get(st, 0) + 1
            print(f"  SKIP (dismissed): {title}")
        else:
            # Augment existing task
            conn.execute(
                "UPDATE tasks SET source_snippet = ?, priority = MIN(priority, ?), updated_at = ? WHERE id = ?",
                (item['source_snippet'], item['priority'], now, ex['id'])
            )
            updated += 1
            updated_by_source[st] = updated_by_source.get(st, 0) + 1
            print(f"  UPDATE #{ex['id']} ({ex['status']}): {title}")
    else:
        # Create new
        key_people_json = json.dumps(item['key_people'])
        conn.execute(
            """INSERT INTO tasks (title, description, status, parse_status, priority,
               source_type, source_id, source_snippet, source_url, key_people,
               action_type, coaching_text, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (title, item['description'], 'suggested', 'parsed', item['priority'],
             st, sid, item['source_snippet'], item['source_url'], key_people_json,
             item['action_type'], None, now, now)
        )
        created += 1
        created_by_source[st] = created_by_source.get(st, 0) + 1
        print(f"  CREATE: {title}")

conn.commit()

# Add in-batch removed to skipped
skipped += in_batch_removed

# Check for unparsed tasks
unparsed = conn.execute("SELECT COUNT(*) as cnt FROM tasks WHERE parse_status IN ('unparsed', 'queued')").fetchone()
print(f"\nUnparsed tasks: {unparsed['cnt']}")

conn.close()

# Log sync
conn = sqlite3.connect(DB)
summary = json.dumps({
    "email": source_counts.get("email", 0),
    "chat": source_counts.get("chat", 0),
    "meeting": source_counts.get("meeting", 0),
    "created": created,
    "updated": updated,
    "skipped": skipped
})
conn.execute(
    "INSERT INTO sync_log (sync_type, result_summary, tasks_created, tasks_updated, synced_at) VALUES (?,?,?,?,?)",
    ('full_scan', summary, created, updated, now)
)
conn.commit()
conn.close()

# Print summary
print(f"""
==================================================
TodoNess Refresh Complete
==================================================
Source       | Found | Created | Updated | Skipped
--------------------------------------------------
Email        |   {source_counts.get('email',0)}   |    {created_by_source.get('email',0)}    |    {updated_by_source.get('email',0)}    |    {skipped_by_source.get('email',0)}
Teams/Chat   |   {source_counts.get('chat',0)}   |    {created_by_source.get('chat',0)}    |    {updated_by_source.get('chat',0)}    |    {skipped_by_source.get('chat',0)}
Meeting      |   {source_counts.get('meeting',0)}   |    {created_by_source.get('meeting',0)}    |    {updated_by_source.get('meeting',0)}    |    {skipped_by_source.get('meeting',0)}
--------------------------------------------------
Total        |   {sum(source_counts.values())}   |    {created}    |    {updated}    |    {skipped}
==================================================""")
