import sqlite3, json
from datetime import datetime, timezone

with open("data/_parse_tasks_data.json", encoding="utf-8") as f:
    tasks = json.load(f)

conn = sqlite3.connect("data/claudetodo.db")
now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

for t in tasks:
    conn.execute(
        """UPDATE tasks
           SET title=?, description=?, priority=?, due_date=?,
               key_people=?, related_meeting=?,
               coaching_text=?, action_type=?, skill_output=?,
               waiting_activity=?, is_quick_hit=?,
               suggestion_refreshed_at=?, parse_status="parsed", updated_at=?
           WHERE id=?""",
        (t["title"], t["description"], t["priority"], t.get("due_date"),
         t["key_people"], t.get("related_meeting"),
         t["coaching_text"], t["action_type"], t["skill_output"],
         t.get("waiting_activity"), t["is_quick_hit"],
         now, now, t["id"])
    )
    print(f"Task {t['id']} written: {t['title'][:60]}")

conn.commit()
conn.close()
print("\nAll tasks parsed and saved.")
