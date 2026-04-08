import sqlite3
from datetime import datetime, timezone
import json

DB = 'data/claudetodo.db'
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

created = 0
updated = 0
chat_count = 0
meeting_count = 0
email_count = 0

# ─── AUGMENTS ────────────────────────────────────────────────────────────────

# Item 3 → Augment task 682 (CAB workshop logistics)
snip3 = ("In the Phil/Aamer 1:1 meeting (April 6, 2026), you were tasked to obtain and "
         "submit the necessary logistics information for the CAB mini-Kickstarter workshop. "
         "This is required for event coordination and was explicitly discussed under planning "
         "and travel logistics for CAB delivery.")
conn.execute(
    "UPDATE tasks SET source_snippet = ?, updated_at = ? WHERE id = 682 AND status != 'dismissed'",
    (snip3, now)
)
if conn.execute("SELECT changes()").fetchone()[0]:
    print("Augmented task 682 (CAB workshop logistics)")
    updated += 1

# Item 4 → Augment task 692 (conference prep call with Manuela)
snip4 = ("During the Phil/Aamer 1:1 (April 6, 2026), you and Aamer discussed scheduling a "
         "prep call with Manuela to review and finalize conference materials incorporating "
         "Saurabh's feedback. This is a prerequisite for aligning content direction ahead of "
         "the upcoming conference session.")
conn.execute(
    "UPDATE tasks SET source_snippet = ?, updated_at = ? WHERE id = 692 AND status != 'dismissed'",
    (snip4, now)
)
if conn.execute("SELECT changes()").fetchone()[0]:
    print("Augmented task 692 (conference prep call with Manuela)")
    updated += 1

# Item 5 → Augment task 711 (Steve CAB travel cost estimate)
snip5 = ("You asked Steve Jeffery to provide a ballpark travel cost estimate for CAB to "
         "complete your planning inputs (sent yesterday evening via Teams). Steve has not "
         "yet replied. Separately, Steve also messaged asking to reschedule your Thursday "
         "1:1 due to a personal appointment — see related task.")
conn.execute(
    "UPDATE tasks SET source_snippet = ?, priority = MIN(priority, 3), updated_at = ? WHERE id = 711 AND status != 'dismissed'",
    (snip5, now)
)
if conn.execute("SELECT changes()").fetchone()[0]:
    print("Augmented task 711 (Steve CAB travel estimate)")
    updated += 1

# Item 6 → Augment task 679 (Manuela CAPE guidance helper role)
snip6 = ("You asked Manuela Pichler to write up the responsibilities for the CAPE guidance "
         "helper role so Aamer can include it in an email to Joe and Dan with staffing "
         "suggestions. This was sent yesterday afternoon via Teams and the write-up has "
         "not yet been received.")
conn.execute(
    "UPDATE tasks SET source_snippet = ?, updated_at = ? WHERE id = 679 AND status != 'dismissed'",
    (snip6, now)
)
if conn.execute("SELECT changes()").fetchone()[0]:
    print("Augmented task 679 (Manuela CAPE helper role)")
    updated += 1

conn.commit()

# ─── NEW TASKS ────────────────────────────────────────────────────────────────

def insert_task(title, description, source_type, source_id, priority, action_type, key_people_list, source_url=None):
    key_people = json.dumps(key_people_list)
    conn.execute(
        """INSERT INTO tasks (title, description, status, parse_status, priority,
           source_type, source_id, source_snippet, source_url, key_people,
           action_type, coaching_text, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (title, description, 'suggested', 'parsed', priority,
         source_type, source_id, description, source_url, key_people,
         action_type, None, now, now)
    )
    conn.commit()
    task_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    print(f"Created task id={task_id}: {title}")
    return task_id

# Item 1: Respond to Steve about rescheduling Thursday 1:1
insert_task(
    title="Respond to Steve Jeffery about rescheduling Thursday 1:1",
    description=("Steve Jeffery sent a direct Teams message requesting to move your Thursday "
                 "1:1 later in the day due to a personal appointment. He proposed either 4 PM "
                 "or 5 PM as alternatives. You have not yet responded, and this impacts your "
                 "scheduled sync this week."),
    source_type='chat',
    source_id='chat::steve.jeffery@microsoft.com::1:1 on thursday',
    priority=1,
    action_type='schedule-meeting',
    key_people_list=[{"name": "Steve Jeffery", "email": "steve.jeffery@microsoft.com"}],
    source_url="https://teams.microsoft.com/l/message/19:08b7be88-37ac-4e2b-82af-f8bb67e5f2f7_9e0f5175-af75-40a0-bcb3-c015cc9695bc@unq.gbl.spaces/1775559486316"
)
chat_count += 1
created += 1

# Item 2: Reply to Mami Uchida on Frontier Agent Kickstarter localization
insert_task(
    title="Reply to Mami Uchida on Frontier Agent Kickstarter localization in Japan",
    description=("Mami Uchida (AIBP Activation GTM - Japan) reached out asking whether the "
                 "CAT Frontier Agent Program Kickstarter will be delivered in languages other "
                 "than English (e.g., Japanese) in Q4. She referenced the recent Japanese "
                 "delivery of the Copilot Studio / Agent Kickstarter and is seeking guidance "
                 "on localization direction for future initiatives."),
    source_type='chat',
    source_id='chat::mami.uchida@microsoft.com::localization of cat frontier agent program -',
    priority=2,
    action_type='respond-email',
    key_people_list=[{"name": "Mami Uchida", "email": "mami.uchida@microsoft.com"}],
    source_url="https://teams.microsoft.com/l/message/19:08b7be88-37ac-4e2b-82af-f8bb67e5f2f7_c77fd41c-16cf-4665-8e9b-7039e7f9395a@unq.gbl.spaces/1775374186523"
)
chat_count += 1
created += 1

# Item 7: Follow up with Veselina Eneva on CAB day allocation and agenda
insert_task(
    title="Follow up with Veselina Eneva on CAB day allocation and agenda",
    description=("You emailed Veselina Eneva on April 7, 2026 asking which day of CAB the "
                 "finance leadership session would fall on, how much time is allocated, and "
                 "requesting to see the draft agenda to understand your team's role. No "
                 "response received yet to your specific questions."),
    source_type='email',
    source_id='email::veneva@microsoft.com::executive finance leadership day 18th may red',
    priority=4,
    action_type='awaiting-response',
    key_people_list=[{"name": "Veselina Eneva", "email": "veneva@microsoft.com"}],
    source_url="https://outlook.office365.com/owa/?ItemID=AAMkADFkODcyODkwLTE0MjItNDVmOC05Yjk4LWYzYjRkMWNjMWRjOABGAAAAAACqtvZcafJOQpm3UWtumEp1BwBUbWXlPIRSQbk%2brCcTGYSHAAAAAAEMAABUbWXlPIRSQbk%2brCcTGYSHABNwayqSAAA%3d&exvsurl=1&viewmodel=ReadMessageItem"
)
email_count += 1
created += 1

conn.close()

# ─── SUMMARY ─────────────────────────────────────────────────────────────────
total_found = 7
skipped = 0
print()
print(f"Summary: found={total_found} created={created} updated={updated} skipped={skipped}")
print(f"  email={email_count} chat={chat_count} meeting={meeting_count}")

# Item from WorkIQ Q1 (old content removed)
title = 'placeholder'
description = (
    "In the CAPE Agent builder triage (twice weekly) meeting (April 3, 2026), the team discussed "
    "updated targets of 200 customers and 600 agents and the need to identify three or more use cases "
    "per customer, referencing guidance from Saurabh Pant and Srini Raghavan. Follow-up alignment with "
    "CPMs may be required to ensure customer engagements meet the updated multi-use case threshold."
)
source_type = 'meeting'
first_person_email = 'spant@microsoft.com'
root_subject = 'cape agent builder triage (twice weekly)'
source_id = f'{source_type}::{first_person_email.lower()}::{root_subject[:50].lower()}'
# Group action (meeting discussion, not directly assigned) -> P2 downgrade to P3
priority = 3
action_type = 'follow-up'
key_people = json.dumps([
    {"name": "Saurabh Pant", "email": "spant@microsoft.com"},
    {"name": "Taiki Yoshida", "email": "Taiki.Yoshida@microsoft.com"},
    {"name": "Adrian Maclean", "email": "Adrian.Maclean@microsoft.com"},
    {"name": "Kanika Ramji", "email": "kanikaramji@microsoft.com"},
])
source_snippet = description

print(f"source_id: {source_id}")

# Pass 1: exact
existing = conn.execute(
    'SELECT id, status, source_id, title FROM tasks WHERE source_id = ? OR LOWER(SUBSTR(title, 1, 40)) = LOWER(SUBSTR(?, 1, 40))',
    (source_id, title)
).fetchall()
print(f"Pass 1 exact matches: {len(existing)}")
for r in existing:
    print(f"  id={r['id']} status={r['status']} title={r['title'][:60]}")

# Pass 2: semantic same sender
sender_prefix = first_person_email.split('@')[0]
same_sender = conn.execute(
    'SELECT id, status, source_id, title FROM tasks WHERE source_id LIKE ?',
    ('%::' + sender_prefix + '%',)
).fetchall()
print(f"Pass 2 same-sender tasks ({sender_prefix}): {len(same_sender)}")
for r in same_sender:
    print(f"  id={r['id']} status={r['status']} title={r['title'][:60]}")

conn.close()
