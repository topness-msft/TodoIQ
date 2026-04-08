import sqlite3, json
from datetime import datetime, timezone

conn = sqlite3.connect("data/claudetodo.db")
now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

updates = [
    (693, "still_pending", "Daniel Gulesserian replied 'Could be Kimberly Pfahl but we don't know for sure'  no confirmed Salesforce panelist identity yet"),
    (690, "still_pending", "Maria Luisa Onorato is waiting for speaker names  explicitly said 'let me know when you have the names'"),
    (691, "still_pending", "No communication found with Julie Yack about SOW/PO proxy since Apr 7; proxy assignment unresolved"),
    (692, "still_pending", "No prep call scheduled or confirmed with Aamer Kaleem or Manuela Pichler for conference content yet"),
    (687, "still_pending", "Maria Luisa Onorato is waiting for speaker names  explicitly said 'let me know when you have the names'"),
    (688, "still_pending", "No communication found with Julie Yack about SOW/PO proxy since Apr 7; proxy assignment unresolved"),
    (689, "still_pending", "No prep call scheduled or confirmed with Aamer Kaleem or Manuela Pichler for conference content yet"),
    (682, "likely_resolved", "Steve Jeffery shared detailed T&E estimate in Teams (flights 892, hotel 420, total ~1574/$2082) a couple of hours ago"),
]

for task_id, status, summary in updates:
    activity = json.dumps({"status": status, "summary": summary, "checked_at": now})
    conn.execute("UPDATE tasks SET waiting_activity = ?, updated_at = ? WHERE id = ?", (activity, now, task_id))
    print(f"Updated task #{task_id}: {status}")

conn.commit()
conn.close()
