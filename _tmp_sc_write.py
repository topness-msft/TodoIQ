import sqlite3, json
from datetime import datetime, timezone

conn = sqlite3.connect('data/claudetodo.db')
now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

results = [
    (646, 'still_pending', 'Presenter ownership for your section explicitly left open — Sameer and Elaine waiting on confirmation of who covers your portion (Sameer vs. delegate).'),
    (647, 'unclear', 'You already responded in Teams that Wednesday dinner is WIP and pinged Bill Spencer. Monday/Tuesday confirmed; Wednesday calendar hold still pending from Bill.'),
    (648, 'likely_resolved', 'You introduced yourself to Sahana via Teams, shared the Envisioning the Frontier of Work toolkit and a nomination link. Task action completed; awaiting Sahana response.'),
    (641, 'still_pending', 'No direct contact with Saurabh to clarify presentation intent. Saurabh proposed 15-min update content with Sameer but no Phil-Saurabh 1:1 clarification found.'),
    (642, 'still_pending', 'No response from Serena Xie with GitHub handle. No repo access granted yet.'),
    (643, 'still_pending', 'No evidence of PO spreadsheet review or follow-up with Greg on FY26 POs since task was created.'),
    (644, 'unclear', 'Active coordination with Mehdi on making Forte/Work IQ available, including deployment constraints (DLP). No confirmation tool is ready for offsite.'),
    (645, 'likely_resolved', 'You attended the CSU/CAPE Sync and were fully looped in on campaign details: ~10 CAPE 100 accounts, in-product messaging mechanics, risk framing, and measurement approach. Rebecca and Adrian have explicit action items.'),
]

for task_id, status, summary in results:
    activity = json.dumps({'status': status, 'summary': summary, 'checked_at': now})
    conn.execute('UPDATE tasks SET waiting_activity = ?, updated_at = ? WHERE id = ?', (activity, now, task_id))
    print(f'Updated task #{task_id}: {status}')

conn.commit()
conn.close()
