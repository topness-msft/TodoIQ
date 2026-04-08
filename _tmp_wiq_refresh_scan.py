"""Run WorkIQ scans for todo-refresh."""
import subprocess
import os
import sys

workiq = os.path.join(os.environ['APPDATA'], 'npm', 'workiq.cmd')
env = {**os.environ, 'NODE_OPTIONS': '--no-deprecation'}

q1 = (
    "What Teams messages and meeting action items need my attention or action? "
    "Include: (1) Teams messages from the last 1 days directed at me by name "
    "or @mentioning me that I have not responded to, "
    "(2) action items from meetings in the last 1 days assigned to me or that "
    "I committed to. For each item return a structured task suggestion with ALL "
    "these fields: 1. Task title: A clean imperative action describing WHAT I NEED "
    "TO DO. 2. Description: 2-3 sentences of context. 3. Source type: teams or meeting. "
    "4. Key people: full resolved name and email address. 5. Priority: P1/P2/P3/P4. "
    "6. Original subject or topic. 7. Date. 8. Action type: respond-email, follow-up, "
    "schedule-meeting, prepare, or general. Format each as a numbered task with clear "
    "field labels."
)

q2 = (
    "What messages or emails have I SENT in the last 1 days that contain a question, "
    "request, or ask where the recipient has not responded yet? Only include items "
    "where I am clearly waiting for a response - not messages I sent that were purely "
    "informational. For each item return: 1. Task title: imperative action "
    "(e.g. Follow up with X on Y). 2. Description: 2-3 sentences: what I asked, "
    "who I am waiting on, when I sent it. 3. Source type: email, teams, or meeting. "
    "4. Key people: full resolved name and email. 5. Priority: P3 or P4. "
    "6. Original subject or topic. 7. Date. 8. Action type: awaiting-response. "
    "Format as numbered tasks with field labels."
)

queries = [(q1, "data/_wiq_scan1.txt"), (q2, "data/_wiq_scan2.txt")]

for i, (q, outfile) in enumerate(queries, 1):
    print(f"Running Q{i}...", flush=True)
    try:
        proc = subprocess.run(
            [workiq, 'ask', '-q', q],
            capture_output=True, text=True, encoding='utf-8',
            errors='replace', timeout=120, env=env
        )
        with open(outfile, 'w', encoding='utf-8') as f:
            f.write(proc.stdout or '')
        out_len = len(proc.stdout or '')
        print(f"Q{i} done. Exit={proc.returncode}, stdout_len={out_len}")
        if proc.stderr:
            print(f"Q{i} stderr (first 300): {proc.stderr[:300]}")
    except Exception as e:
        print(f"Q{i} error: {e}")
        with open(outfile, 'w') as f:
            f.write('')
