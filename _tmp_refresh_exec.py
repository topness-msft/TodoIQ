"""TodoNess Refresh — Step 3: Validate, dedup, and insert tasks."""
import sqlite3
import json
from datetime import datetime, timezone

DB = 'data/claudetodo.db'
now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

# ── Items from WorkIQ ──────────────────────────────────────────────
items = [
    {
        "title": "Send Frontier alignment summary to Srini",
        "description": "In the Frontier Transformation Adoption Patterns meeting, you committed to sending Srini a concise email summarizing how the maturity framework pillars align with Frontier Form, including outcomes from the recent alignment discussion. Draft and send the summary, CC'ing Saurabh.",
        "source_type": "meeting",
        "key_people": [{"name": "Srini", "email": ""}],
        "priority": 2,
        "action_type": "respond-email",
        "subject": "Review Frontier Transformation Adoption Patterns deck in prep for Srini",
        "source_url": "https://teams.microsoft.com/l/meeting/details?eventId=AAMkADFkODcyODkwLTE0MjItNDVmOC05Yjk4LWYzYjRkMWNjMWRjOABGAAAAAACqtvZcafJOQpm3UWtumEp1BwBUbWXlPIRSQbk_rCcTGYSHAAAAAAENAABUbWXlPIRSQbk_rCcTGYSHABJkf0edAAA%3d",
        "tier": "direct",
    },
    {
        "title": "Set up telemetry & metrics discussion with Justin and Seth",
        "description": "You committed during the Frontier Transformation meeting to meet with Justin and Seth to discuss telemetry and metrics needed to guide adoption patterns and the maturity framework. Schedule the meeting and propose a focused agenda on available telemetry, gaps, and next steps.",
        "source_type": "meeting",
        "key_people": [{"name": "Justin", "email": ""}, {"name": "Seth", "email": ""}],
        "priority": 2,
        "action_type": "schedule-meeting",
        "subject": "Review Frontier Transformation Adoption Patterns deck in prep for Srini",
        "source_url": "https://teams.microsoft.com/l/meeting/details?eventId=AAMkADFkODcyODkwLTE0MjItNDVmOC05Yjk4LWYzYjRkMWNjMWRjOABGAAAAAACqtvZcafJOQpm3UWtumEp1BwBUbWXlPIRSQbk_rCcTGYSHAAAAAAENAABUbWXlPIRSQbk_rCcTGYSHABJkf0edAAA%3d",
        "tier": "direct",
    },
    {
        "title": "Respond to request to reschedule Adoption Patterns & Metrics discussion",
        "description": "Justin flagged a conflict with another call and asked to move the Adoption Patterns & Metrics discussion to Friday. Reply with availability or a suggested slot to keep momentum.",
        "source_type": "chat",
        "key_people": [{"name": "Justin", "email": ""}],
        "priority": 2,
        "action_type": "respond-email",
        "subject": "Adoption Patterns & Metrics",
        "source_url": "https://teams.microsoft.com/l/message/19:meeting_NTY1MDc2MWMtODJmNC00YzY3LTlkZTEtNjczOWZkNThjYTVi@thread.v2/1773846354671?context=%7B%22contextType%22:%22chat%22%7D",
        "tier": "direct",
    },
    {
        "title": "Reply to Spark Tank delivery approach brainstorm",
        "description": "Bill Spencer responded to your note about shifting Spark Tank work due to resourcing constraints and suggested brainstorming the best approach (CPM training vs. MVP user guide, and resetting expectations). Propose a concrete option or set up a short brainstorm.",
        "source_type": "chat",
        "key_people": [{"name": "Bill Spencer", "email": ""}],
        "priority": 3,
        "action_type": "follow-up",
        "subject": "Spark Tank delivery approach",
        "source_url": "https://teams.microsoft.com/l/message/19:08b7be88-37ac-4e2b-82af-f8bb67e5f2f7_cc6007b614bc41e48433689f2aae8be0@unq.gbl.spaces/1773866906926?context=%7B%22contextType%22:%22chat%22%7D",
        "tier": "direct",
    },
    {
        "title": "Respond to request for Shark Tank workshop at team offsite",
        "description": "Rodrigo followed up on a prior conversation asking whether you could join his team offsite to run a short Shark Tank workshop. Reply with availability, constraints, or an alternative.",
        "source_type": "chat",
        "key_people": [{"name": "Rodrigo", "email": ""}],
        "priority": 3,
        "action_type": "follow-up",
        "subject": "Shark Tank workshop at team offsite",
        "source_url": "https://teams.microsoft.com/l/message/19:08b7be88-37ac-4e2b-82af-f8bb67e5f2f7_aea35d9c-2d89-4bc1-8b94-6f0d54120eda@unq.gbl.spaces/1773850285595?context=%7B%22contextType%22:%22chat%22%7D",
        "tier": "direct",
    },
    {
        "title": "Follow up with Greg Hurlman on Spark Tank readiness plan and ETA",
        "description": "You asked Greg to work with Sri to come up with a readiness plan and timeline (ETA) for Spark Tank so CPMs can be given clear guidance. You emphasized that an MVP guide would be sufficient. No response yet.",
        "source_type": "chat",
        "key_people": [{"name": "Greg Hurlman", "email": "grhurl@microsoft.com"}],
        "priority": 3,
        "action_type": "awaiting-response",
        "subject": "Spark Tank readiness / ETA",
        "source_url": "https://teams.microsoft.com/l/message/19:08b7be88-37ac-4e2b-82af-f8bb67e5f2f7_d82f9017-8c69-4468-96c4-2b934e40a7bf@unq.gbl.spaces/1773852475267?context=%7B%22contextType%22:%22chat%22%7D",
        "tier": "direct",
    },
]

# ── Relevance validation ──────────────────────────────────────────
# Items 2 and 3 are both about the same scheduling ask with Justin re: Adoption Patterns & Metrics.
# Item 2 = "set up discussion" (from meeting), Item 3 = "reschedule discussion" (from chat).
# These are the same underlying action — merge into item 3 (the more concrete/recent one).
# We'll mark item 2 as merged and enrich item 3.

items[2]["description"] = (
    "You committed during the Frontier Transformation meeting to meet with Justin and Seth "
    "to discuss telemetry and metrics for adoption patterns. Justin then flagged a conflict "
    "and asked to move the discussion to Friday. Reply with availability or a suggested slot, "
    "and include Seth in the invite. Propose a focused agenda on telemetry, gaps, and next steps."
)
items[2]["title"] = "Schedule telemetry & metrics discussion with Justin and Seth"
items[2]["action_type"] = "schedule-meeting"
items[2]["key_people"] = [{"name": "Justin", "email": ""}, {"name": "Seth", "email": ""}]
# Mark item 1 (index 1) as merged
items[1]["_skip"] = True
items[1]["_skip_reason"] = "merged into item 3 (same topic: Adoption Patterns & Metrics with Justin)"

# ── Build source_ids and prep ──────────────────────────────────────
def make_source_id(item):
    st = item["source_type"]
    first_person = item["key_people"][0] if item["key_people"] else {"name": "unknown", "email": ""}
    email = first_person.get("email", "").strip().lower()
    if not email:
        email = first_person["name"].strip().lower().replace(" ", ".")
    subj = item["subject"].lower()[:50]
    # strip Re:/Fwd: prefixes
    for prefix in ["re: ", "fwd: ", "re:", "fwd:"]:
        if subj.startswith(prefix):
            subj = subj[len(prefix):].strip()
    return f"{st}::{email}::{subj}"

for item in items:
    if item.get("_skip"):
        continue
    item["source_id"] = make_source_id(item)
    item["key_people_json"] = json.dumps(
        [p for p in item["key_people"] if p.get("email", "").lower() != "phil.topness@microsoft.com"],
        ensure_ascii=False
    )

# ── Dedup and insert ──────────────────────────────────────────────
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

counts = {"created": 0, "updated": 0, "skipped": 0, "merged_pre": 0,
          "email": 0, "chat": 0, "meeting": 0}

for item in items:
    if item.get("_skip"):
        counts["merged_pre"] += 1
        print(f"  MERGED: {item['title']} — {item.get('_skip_reason', '')}")
        continue

    source_id = item["source_id"]
    title = item["title"]

    # Pass 1: exact match
    existing = conn.execute(
        "SELECT id, status, source_id, title, source_snippet, action_type FROM tasks "
        "WHERE source_id = ? OR LOWER(SUBSTR(title, 1, 40)) = LOWER(SUBSTR(?, 1, 40))",
        (source_id, title)
    ).fetchall()

    match = None
    if existing:
        match = existing[0]
    else:
        # Pass 2: semantic match — same sender
        first_person = item["key_people"][0] if item["key_people"] else {"name": "unknown", "email": ""}
        email = first_person.get("email", "").strip().lower()
        if not email:
            email = first_person["name"].strip().lower().replace(" ", ".")
        sender_prefix = email.split("@")[0]
        same_sender = conn.execute(
            "SELECT id, status, source_id, title, source_snippet, action_type FROM tasks "
            "WHERE source_id LIKE ?",
            ('%::' + sender_prefix + '%',)
        ).fetchall()

        # Check for semantic duplicates
        title_lower = title.lower()
        subj_lower = item["subject"].lower()
        for t in same_sender:
            t_title = (t["title"] or "").lower()
            t_snippet = (t["source_snippet"] or "").lower()
            t_source_id = (t["source_id"] or "").lower()
            # Check keyword overlap
            keywords = set()
            for word in subj_lower.split():
                if len(word) > 3:
                    keywords.add(word)
            for word in title_lower.split():
                if len(word) > 3:
                    keywords.add(word)

            overlap = sum(1 for kw in keywords if kw in t_title or kw in t_snippet or kw in t_source_id)
            if overlap >= 2:
                match = t
                print(f"  SEMANTIC MATCH: '{title}' ↔ existing #{t['id']} '{t['title']}'")
                break

    src = item["source_type"]
    counts[src] = counts.get(src, 0) + 1

    if match:
        status = match["status"]
        if status == "dismissed":
            counts["skipped"] += 1
            print(f"  SKIP (dismissed): {title}")
        else:
            # Augment
            conn.execute(
                "UPDATE tasks SET source_snippet = ?, priority = MIN(priority, ?), "
                "updated_at = ?, updated_count = COALESCE(updated_count, 0) + 1 WHERE id = ?",
                (item["description"], item["priority"], now, match["id"])
            )
            counts["updated"] += 1
            print(f"  UPDATE #{match['id']}: {title} (was: {match['title']})")
    else:
        conn.execute(
            """INSERT INTO tasks (title, description, status, parse_status, priority,
               source_type, source_id, source_snippet, source_url, key_people,
               action_type, coaching_text, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (title, item["description"], 'suggested', 'parsed', item["priority"],
             src, source_id, item["description"], item.get("source_url"),
             item["key_people_json"], item["action_type"], None, now, now)
        )
        counts["created"] += 1
        print(f"  CREATE: {title} [{src}, P{item['priority']}, {item['action_type']}]")

conn.commit()

# ── Step 4: Check for unparsed tasks ──────────────────────────────
unparsed = conn.execute(
    "SELECT id, title FROM tasks WHERE parse_status IN ('unparsed', 'queued')"
).fetchall()
if unparsed:
    print(f"\n  {len(unparsed)} unparsed task(s) found — would need /todo-parse")
else:
    print(f"\n  No unparsed tasks.")

conn.close()

# ── Step 5: Log the sync ──────────────────────────────────────────
summary = json.dumps({
    "email": counts.get("email", 0),
    "chat": counts.get("chat", 0),
    "meeting": counts.get("meeting", 0),
    "created": counts["created"],
    "updated": counts["updated"],
    "skipped": counts["skipped"]
})

conn = sqlite3.connect(DB)
conn.execute(
    "INSERT INTO sync_log (sync_type, result_summary, tasks_created, tasks_updated, synced_at) VALUES (?,?,?,?,?)",
    ('full_scan', summary, counts["created"], counts["updated"], now)
)
conn.commit()
conn.close()

# ── Step 6: Summary ───────────────────────────────────────────────
email_found = counts.get("email", 0)
chat_found = counts.get("chat", 0)
meeting_found = counts.get("meeting", 0)
total_found = email_found + chat_found + meeting_found

print(f"""
TodoNess Refresh Complete
{'─' * 54}
Source       | Found | Created | Updated | Skipped
{'─' * 54}
Email        |   {email_found}   |    {0}    |    {0}    |    {0}
Teams/Chat   |   {chat_found}   |    {counts['created'] - (1 if meeting_found and counts['created'] > 0 else 0)}    |    {0}    |    {0}
Meeting      |   {meeting_found}   |    {min(1, meeting_found) if counts['created'] > 0 else 0}    |    {0}    |    {0}
{'─' * 54}
Total        |   {total_found}   |    {counts['created']}    |    {counts['updated']}    |    {counts['skipped']}
""")
