"""Run WorkIQ scan via copilot subprocess and wait for results."""
import subprocess
import sys
import os
import time
import tempfile
from pathlib import Path

PROJECT_ROOT = Path('C:/Users/phtopnes/claude/projects/ClaudeTodo')

QUERY_2A = """What Teams messages and meeting action items need my attention or action? Include: (1) Teams messages from the last 1 days directed at me by name or @mentioning me that I haven't responded to, (2) action items from meetings in the last 1 days assigned to me or that I committed to. For each item, return it as a structured task suggestion with ALL of these fields: 1. **Task title**: A clean imperative action describing WHAT I NEED TO DO. 2. **Description**: 2-3 sentences of context. 3. **Source type**: teams or meeting. 4. **Key people**: Full resolved name and email address for each person involved. 5. **Priority**: P1 (urgent), P2 (time-sensitive), P3 (normal), P4 (low). 6. **Original subject or topic**. 7. **Date**: When item occurred. 8. **Action type**: respond-email, follow-up, schedule-meeting, prepare, or general. Format each as a numbered task with clear field labels."""

QUERY_2B = """What messages or emails have I SENT in the last 1 days that contain a question, request, or ask where the recipient hasn't responded yet? Only include items where I am clearly waiting for a response. For each item: 1. **Task title**: imperative action (e.g. "Follow up with Alex on budget approval"). 2. **Description**: what I asked, who I'm waiting on, when I sent it. 3. **Source type**: email, teams, or meeting. 4. **Key people**: Full resolved name and email. 5. **Priority**: P3 or P4. 6. **Original subject or topic**. 7. **Date**: When I sent it. 8. **Action type**: awaiting-response. Format as numbered tasks."""

out_2a = PROJECT_ROOT / 'data' / '_wiq_2a_out.txt'
out_2b = PROJECT_ROOT / 'data' / '_wiq_2b_out.txt'

def run_query(query, out_file, label):
    print(f"Starting {label}...")
    full_cmd = f"{query}\n\nWrite your complete structured output to file: {out_file}"
    proc = subprocess.run(
        ["copilot", "-p", full_cmd, "--allow-tool=workiq", "--allow-tool=write"],
        cwd=str(PROJECT_ROOT),
        capture_output=True, text=True, timeout=180
    )
    print(f"{label} exit code: {proc.returncode}")
    if proc.stdout:
        print(f"{label} stdout (first 500): {proc.stdout[:500]}")
    if proc.stderr:
        print(f"{label} stderr (first 200): {proc.stderr[:200]}")
    return proc

r1 = run_query(QUERY_2A, out_2a, "2a-teams-meetings")
r2 = run_query(QUERY_2B, out_2b, "2b-awaiting")
