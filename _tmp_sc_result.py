import sqlite3, json
from datetime import datetime, timezone

conn = sqlite3.connect('data/claudetodo.db')
now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

results = [
    (654, 'still_pending', 'No reply from Rodrigo De la Garza since March 31 ask about April 20 availability. Follow up needed.'),
    (678, 'still_pending', 'Waiting for Kristina Marko to send field enablement slides committed in April 6 meeting.'),
    (679, 'still_pending', 'Manuela Pichler has not responded to April 6 Teams message about CAPE guidance helper role description.'),
    (667, 'still_pending', 'No evidence you responded to Mami Uchida about Q4 localization plans for Agent Kickstarter programs.'),
    (668, 'still_pending', 'TIME-SENSITIVE: Deck review due before Wednesday April 8. Elaine shared retro learnings deck April 6.'),
    (669, 'still_pending', 'Scale team points not yet added to FY26 Sailboat retrospective per Bill Spencer group ask.'),
    (670, 'still_pending', 'April 6 1:1 commitment to send Aamer note about Salesforce keynote panel not yet fulfilled.'),
    (672, 'still_pending', 'April 6 1:1 commitment to email Aamer functional needs for scaling guidance role not yet fulfilled.'),
    (673, 'still_pending', 'Field testing coordination with Denise Moran not started after April 6 Kristina meeting commitment.'),
    (674, 'still_pending', 'Biweekly 1:1 with Vic for FastTrack/FY27 not yet scheduled. Possible duplicate of task 677.'),
    (675, 'still_pending', 'CAB mini-Kickstarter travel estimate not yet requested from Steve Jeffery.'),
    (676, 'still_pending', 'Governance workshop candidate customers not yet identified.'),
    (677, 'still_pending', 'Biweekly 1:1 with Vic for FastTrack not yet scheduled. Possible duplicate of task 674.'),
]

for task_id, status, summary in results:
    activity = json.dumps({'status': status, 'summary': summary, 'checked_at': now})
    conn.execute('UPDATE tasks SET waiting_activity = ?, updated_at = ? WHERE id = ?', (activity, now, task_id))
    print(f'Updated task #{task_id}: {status}')

conn.commit()
conn.close()
print('Done.')
