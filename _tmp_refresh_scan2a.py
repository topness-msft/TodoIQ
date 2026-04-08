import subprocess, sys, os

os.environ['PYTHONUTF8'] = '1'

prompt = (
    'What Teams messages and meeting action items need my attention or action? '
    'Include: (1) Teams messages from the last 1 days directed at me by name or '
    '@mentioning me that I have not responded to, (2) action items from meetings '
    'in the last 1 days assigned to me or that I committed to. For each item, '
    'return it as a structured task suggestion with ALL of these fields: '
    '1. Task title: A clean imperative action describing WHAT I NEED TO DO. '
    'Not the message topic - describe the action. '
    '2. Description: 2-3 sentences of context. '
    '3. Source type: teams or meeting. '
    '4. Key people: For each person involved, give their FULL resolved name and email address. '
    '5. Priority: P1 (urgent/deadline today), P2 (time-sensitive), P3 (normal), P4 (low/FYI). '
    '6. Original subject or topic: The root subject (strip Re:/Fwd: prefixes). '
    '7. Date: When the item was sent/occurred. '
    '8. Action type: One of: respond-email, follow-up, schedule-meeting, prepare, general. '
    'Format each item as a numbered task with clear field labels.'
)

result = subprocess.run(
    ['copilot', '-p', prompt, '--allow-tool=workiq'],
    capture_output=True, timeout=300, encoding='utf-8', errors='replace'
)
print(result.stdout)
if result.stderr:
    print("STDERR:", result.stderr[:300], file=sys.stderr)
print("EXIT:", result.returncode)
