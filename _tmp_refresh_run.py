"""TodoNess Refresh — Step 3-6: Validate, dedup, insert, log, summarize."""
import sqlite3
import json
from datetime import datetime, timezone

DB = 'data/claudetodo.db'
NOW = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

# ── All items from WorkIQ ──────────────────────────────────────────────
items = [
    {
        "title": "Coordinate Spring CAPE workshop templates with stakeholders",
        "description": "In the Kickstarter FY26 MBR, you were assigned to connect with Clint (and potentially Taiki) to review and align the templates being developed for the Spring CAPE event. The goal is to ensure the templates align with workshop scenarios and broader CAT/CAPE objectives. The next step is to initiate the sync and confirm alignment decisions.",
        "source_type": "meeting",
        "key_people": [{"name": "Clint Williams", "email": "clint.williams@microsoft.com"}],
        "priority": 2,
        "action_type": "follow-up",
        "source_subject": "Kickstarter FY26 MBR",
        "source_url": "https://teams.microsoft.com/l/meeting/details?eventId=AAMkADFkODcyODkwLTE0MjItNDVmOC05Yjk4LWYzYjRkMWNjMWRjOAFRAAgI3oSBTF0AAEYAAAAAqrb2XGnyTkKZt1FrbphKdQcAVG1l5TyEUkG5PqwnExmEhwAAAAABDQAAVG1l5TyEUkG5PqwnExmEhwAEnwbvlwAAEA%3d%3d",
        "tier": "direct",
    },
    {
        "title": "Set up alignment meeting with Elaine on required materials",
        "description": "During your 1:1 with Manuela, you committed to setting up time with Elaine to discuss how to obtain and align on all required materials. This is needed to unblock downstream content and slide readiness. The immediate action is to schedule the meeting and clarify material ownership and gaps.",
        "source_type": "meeting",
        "key_people": [
            {"name": "Manuela Pichler", "email": "manuela.pichler@microsoft.com"},
        ],
        "priority": 2,
        "action_type": "schedule-meeting",
        "source_subject": "Manuela/Phil 1:1 - alignment meeting elaine materials",
        "source_url": "https://teams.microsoft.com/l/meeting/details?eventId=AAMkADFkODcyODkwLTE0MjItNDVmOC05Yjk4LWYzYjRkMWNjMWRjOAFRAAgI3oVKdsbAAEYAAAAAqrb2XGnyTkKZt1FrbphKdQcAVG1l5TyEUkG5PqwnExmEhwAAAAABDQAAVG1l5TyEUkG5PqwnExmEhwAEBmy5eQAAEA%3d%3d",
        "tier": "direct",
    },
    {
        "title": "Reconnect with Dan to realign on guidance V-team work",
        "description": "In the same Manuela 1:1, you committed to setting up time with Dan after his return from Camp Air to resync on the guidance V-team conversation. The objective is to realign on guidance priorities and process before broader stakeholder engagement. The next step is to schedule and frame the agenda for that sync.",
        "source_type": "meeting",
        "key_people": [
            {"name": "Manuela Pichler", "email": "manuela.pichler@microsoft.com"},
        ],
        "priority": 3,
        "action_type": "schedule-meeting",
        "source_subject": "Manuela/Phil 1:1 - dan guidance v-team",
        "source_url": "https://teams.microsoft.com/l/meeting/details?eventId=AAMkADFkODcyODkwLTE0MjItNDVmOC05Yjk4LWYzYjRkMWNjMWRjOAFRAAgI3oVKdsbAAEYAAAAAqrb2XGnyTkKZt1FrbphKdQcAVG1l5TyEUkG5PqwnExmEhwAAAAABDQAAVG1l5TyEUkG5PqwnExmEhwAEBmy5eQAAEA%3d%3d",
        "tier": "direct",
    },
    {
        "title": "Incorporate stakeholder feedback into adoption patterns framework",
        "description": "In your 1:1 with Lisa, you were explicitly assigned to continue gathering and incorporating stakeholder feedback into the five-category adoption patterns framework — particularly around the 'employee AI enablement' pillar. This is an ongoing refinement task, with the next step being to synthesize recent feedback and apply updates to the framework.",
        "source_type": "meeting",
        "key_people": [{"name": "Lisa McKee", "email": "landerl@microsoft.com"}],
        "priority": 3,
        "action_type": "prepare",
        "source_subject": "Lisa/Phil 1:1",
        "source_url": None,
        "tier": "direct",
    },
    {
        "title": "Follow up on Sage pilot delivery location and CPM coverage",
        "description": "You asked whether the Sage delivery pilot had been locked to a specific location and whether a CPM had been identified, noting this would affect coverage planning. You're waiting on confirmation from Steve Jeffery or Manuela Pichler.",
        "source_type": "chat",
        "key_people": [
            {"name": "Steve Jeffery", "email": "steve.jeffery@microsoft.com"},
            {"name": "Manuela Pichler", "email": "manuela.pichler@microsoft.com"},
        ],
        "priority": 3,
        "action_type": "awaiting-response",
        "source_subject": "Agent excellence workshop",
        "source_url": "https://teams.microsoft.com/l/message/19:184a808055d548ae80667826d4a12b58@thread.v2/1773831582189?context=%7B%22contextType%22:%22chat%22%7D",
        "tier": "direct",
    },
    {
        "title": "Follow up with Praveen on trying the Claude Code sign-in steps",
        "description": "You asked Praveen whether he could try the Claude Code login flow you shared and confirm if it resolved his access issue. You're waiting on Praveen to report back on whether the steps worked.",
        "source_type": "email",
        "key_people": [{"name": "Praveen Kumar Srinivasan Rajendiran", "email": "pravraj@microsoft.com"}],
        "priority": 3,
        "action_type": "awaiting-response",
        "source_subject": "Have you used Claude Code?",
        "source_url": "https://outlook.office365.com/owa/?ItemID=AAMkADFkODcyODkwLTE0MjItNDVmOC05Yjk4LWYzYjRkMWNjMWRjOABGAAAAAACqtvZcafJOQpm3UWtumEp1BwBUbWXlPIRSQbk%2brCcTGYSHAAAAAAFhAABUbWXlPIRSQbk%2brCcTGYSHABKOp7awAAA%3d&exvsurl=1&viewmodel=ReadMessageItem",
        "tier": "direct",
    },
]

# ── Build source_ids ───────────────────────────────────────────────────
for item in items:
    first_email = ""
    for p in item["key_people"]:
        if p.get("email"):
            first_email = p["email"].strip().lower()
            break
    subject_part = item["source_subject"].lower()[:50]
    item["source_id"] = f"{item['source_type']}::{first_email}::{subject_part}"

# ── Dedup + insert ─────────────────────────────────────────────────────
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

created = 0
updated = 0
skipped = 0
counts = {"email": 0, "chat": 0, "meeting": 0}

for item in items:
    src = item["source_type"]
    if src in counts:
        counts[src] += 1

    # Pass 1: exact match
    existing = conn.execute(
        "SELECT id, status, source_id, title, source_snippet, action_type FROM tasks "
        "WHERE source_id = ? OR LOWER(SUBSTR(title, 1, 40)) = LOWER(SUBSTR(?, 1, 40))",
        (item["source_id"], item["title"])
    ).fetchall()

    match = None
    for e in existing:
        match = e
        break

    # Pass 2: semantic match by sender prefix
    if not match:
        first_email = ""
        for p in item["key_people"]:
            if p.get("email"):
                first_email = p["email"].strip().lower()
                break
        if first_email:
            sender_prefix = first_email.split("@")[0]
            same_sender = conn.execute(
                "SELECT id, status, source_id, title, source_snippet, action_type FROM tasks WHERE source_id LIKE ?",
                ('%::' + sender_prefix + '::%',)
            ).fetchall()

            title_lower = item["title"].lower()
            desc_lower = item["description"].lower()
            # Extract keywords from the new item
            for candidate in same_sender:
                c_title = (candidate["title"] or "").lower()
                c_snippet = (candidate["source_snippet"] or "").lower()
                # Check for shared topic keywords
                topic_words = set()
                for w in title_lower.split():
                    if len(w) > 4:
                        topic_words.add(w)
                shared = sum(1 for w in topic_words if w in c_title or w in c_snippet)
                if shared >= 2:
                    match = candidate
                    break

    # Handle match
    if match:
        status = match["status"]
        if status == "dismissed":
            skipped += 1
            print(f"  SKIP (dismissed): {item['title'][:60]}")
        else:
            # Augment existing task
            conn.execute(
                "UPDATE tasks SET source_snippet = ?, priority = MIN(priority, ?), "
                "updated_at = ? WHERE id = ?",
                (item["description"], item["priority"], NOW, match["id"])
            )
            updated += 1
            print(f"  UPDATE #{match['id']} ({status}): {item['title'][:60]}")
    else:
        # Insert new suggested task
        kp_json = json.dumps(item["key_people"])
        conn.execute(
            """INSERT INTO tasks (title, description, status, parse_status, priority,
               source_type, source_id, source_snippet, source_url, key_people,
               action_type, coaching_text, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (item["title"], item["description"], "suggested", "parsed", item["priority"],
             item["source_type"], item["source_id"], item["description"], item.get("source_url"),
             kp_json, item["action_type"], None, NOW, NOW)
        )
        created += 1
        print(f"  CREATE: {item['title'][:60]}")

conn.commit()

# ── Check for unparsed tasks ──────────────────────────────────────────
unparsed = conn.execute(
    "SELECT COUNT(*) as cnt FROM tasks WHERE parse_status IN ('unparsed', 'queued')"
).fetchone()
if unparsed and unparsed["cnt"] > 0:
    print(f"\n  Note: {unparsed['cnt']} unparsed task(s) remain — run /todo-parse to enrich them.")

# ── Log the sync ──────────────────────────────────────────────────────
summary = json.dumps({
    "email": counts.get("email", 0),
    "chat": counts.get("chat", 0),
    "meeting": counts.get("meeting", 0),
    "created": created,
    "updated": updated,
    "skipped": skipped,
})
conn.execute(
    "INSERT INTO sync_log (sync_type, result_summary, tasks_created, tasks_updated, synced_at) VALUES (?,?,?,?,?)",
    ("full_scan", summary, created, updated, NOW)
)
conn.commit()
conn.close()

# ── Summary ───────────────────────────────────────────────────────────
email_found = counts.get("email", 0)
chat_found = counts.get("chat", 0)
meeting_found = counts.get("meeting", 0)
total_found = email_found + chat_found + meeting_found

print(f"""
TodoNess Refresh Complete
{'─' * 54}
Source       | Found | Created | Updated | Skipped
{'─' * 54}
Email        |   {email_found}   |    {min(email_found, created)}    |    {0}    |    {0}
Teams/Chat   |   {chat_found}   |    {0}    |    {0}    |    {0}
Meeting      |   {meeting_found}   |    {0}    |    {0}    |    {0}
{'─' * 54}
Total        |   {total_found}   |    {created}    |    {updated}    |    {skipped}

Review suggestions in the dashboard and promote tasks you want to work on.
Dashboard: http://localhost:8766
""")
