"""Temporary script to run WorkIQ queries for todo-refresh."""
import subprocess
import sys
import os

os.environ['PYTHONIOENCODING'] = 'utf-8'

query_type = sys.argv[1]  # 'teams' or 'awaiting'

if query_type == 'teams':
    query = (
        'What Teams messages and meeting action items need my attention or action? '
        'Include: (1) Teams messages from the last 1 days directed at me by name or '
        '@mentioning me that I have not responded to, (2) action items from meetings '
        'in the last 1 days assigned to me or that I committed to. For each item, '
        'return it as a structured task suggestion with ALL of these fields: '
        '1. **Task title**: A clean imperative action describing WHAT I NEED TO DO '
        '(e.g. "Schedule workshop walkthrough with Alex"). Not the message topic. '
        '2. **Description**: 2-3 sentences of context: what was the original ask, '
        'current state, what specifically needs to happen next. '
        '3. **Source type**: teams or meeting. '
        '4. **Key people**: For each person involved, give their FULL resolved name '
        'and email address. Resolve aliases and short names to full directory names. '
        '5. **Priority**: P1 (urgent/deadline today), P2 (time-sensitive), P3 (normal), P4 (low/FYI). '
        '6. **Original subject or topic**: The root subject (strip Re:/Fwd: prefixes). '
        '7. **Date**: When the item was sent/occurred. '
        '8. **Action type**: One of: respond-email, follow-up, schedule-meeting, prepare, general. '
        'Format each item as a numbered task with clear field labels.'
    )
elif query_type == 'awaiting':
    query = (
        'What messages or emails have I SENT in the last 1 days that contain a question, '
        'request, or ask where the recipient has not responded yet? Only include items where '
        'I am clearly waiting for a response, not messages I sent that were purely informational. '
        'For each item, return it as a structured task suggestion with ALL of these fields: '
        '1. **Task title**: A clean imperative action (e.g. "Follow up with Alex on budget approval"). '
        '2. **Description**: 2-3 sentences: what I asked, who I am waiting on, when I sent it. '
        '3. **Source type**: email, teams, or meeting. '
        '4. **Key people**: For each person involved, give their FULL resolved name and email address. '
        '5. **Priority**: P3 (normal) or P4 (low), these are lower urgency since I am waiting. '
        '6. **Original subject or topic**: The root subject (strip Re:/Fwd: prefixes). '
        '7. **Date**: When I sent the message. '
        '8. **Action type**: awaiting-response. '
        'Format each item as a numbered task with clear field labels.'
    )
else:
    print("Usage: python _tmp_wiq_refresh.py [teams|awaiting]")
    sys.exit(1)

result = subprocess.run(
    ['copilot', '-p', query, '--allow-tool=workiq'],
    capture_output=True, timeout=180,
    encoding='utf-8', errors='replace'
)
print(result.stdout)
if result.stderr:
    print("STDERR:", result.stderr[-500:], file=sys.stderr)
sys.exit(result.returncode)
