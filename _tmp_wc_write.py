import sqlite3, json
from datetime import datetime, timezone
conn = sqlite3.connect("data/claudetodo.db")
now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
results = [
    (131, "activity_detected", "Saurabh very active (emails Apr 6, Teams Apr 6, offsite last week). No specific CAPE support resource discussion found. Upcoming OOO Apr 13-18. Aamer Kaleem is NOT OOO and active.", None, "waiting"),
    (171, "no_activity", "No direct communication with Irina about Project Unify since Feb 23. Only appearance: large 460-person Teams channel on Mar 13. Her spring break OOO (week of Mar 9) is now past.", None, "waiting"),
    (450, "may_be_resolved", "Rohini sent Agentic AI Adoption Maturity Model for review to you, Manuela, Nitasha, and Ashvini (~Mar 9). Shared FrontierFirmPlaybook.pdf in Teams Mar 9. Active collaboration on maturity model through Mar 11.", None, "waiting"),
    (451, "may_be_resolved", "Manuela actively collaborating on maturity model blog: alignment meeting Mar 27, shared AI Adoption Maturity Model.docx last week, ongoing Teams + email collaboration through Apr 1.", None, "waiting"),
    (488, "activity_detected", "Greg active: emails through Mar 27, Teams through Mar 27. Recommended Henry Jammes for deeper CAT engagements (Mar 24). No specific signal about Kunal periodic follow-up process.", None, "waiting"),
    (513, "activity_detected", "John active: 22 Teams messages since Mar 14. Revenue follow-up discussion last week. Included in Kunal Tanwar Upper Majors thread (Mar 23). No specific Agent Excellence customer provision signal.", None, "waiting"),
    (515, "activity_detected", "Jason not OOO. Actively collaborating with you and Roger Gilchrist on extensibility of 1P agents. No specific EMEA EBC session response found.", None, "waiting"),
    (517, "activity_detected", "Greg not OOO. Active in emails and Teams through late March. No Spark Tank rollout discussion surfaced.", None, "waiting"),
    (526, "activity_detected", "John not OOO. Active on pipeline and Kickstarter topics. Kunal Tanwar thread (Mar 23) includes FDE/CAD/Kickstarter discussion. Task added to Greg 1:1 agenda Apr 6.", None, "waiting"),
    (572, "activity_detected", "Sarah responded in 1:1 Teams chat yesterday: Vig is great place to start Phil. Also active in Cross-org Agent Acceleration channel yesterday re: CAPE/FT customer list overlap.", None, "waiting"),
    (663, "activity_detected", "Task created yesterday. Manuela active (CAPE offsite last week, emails/Teams through Apr 1). Aamer NOT OOO (back from offsite, active yesterday). No role description signal yet.", None, "waiting"),
    (664, "activity_detected", "Steve on public holiday yesterday but back today. Gave detailed CAB prep update this morning: deck progress, speaker notes, handout target end of week, concern about running solo. Travel estimate not yet provided.", None, "waiting"),
]
for task_id, classification, summary, return_date, orig_status in results:
    activity = {"status": classification, "summary": summary, "checked_at": now}
    if return_date:
        activity["return_date"] = return_date
    val = json.dumps(activity)
    conn.execute("UPDATE tasks SET waiting_activity = ?, updated_at = ? WHERE id = ?", (val, now, task_id))
conn.commit()
conn.close()
print("Updated " + str(len(results)) + " tasks")
