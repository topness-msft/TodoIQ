import subprocess, sys

WORKIQ_CMD = r'C:\Users\phtopnes\AppData\Roaming\npm\workiq.cmd'
query = (
    "What Teams messages and meeting action items need my attention or action from the last 7 days? "
    "Include: (1) Teams messages directed at me by name or @mentioning me that I haven't responded to, "
    "(2) action items from meetings assigned to me or that I committed to. "
    "For each item, return a structured task suggestion with these fields: "
    "1. Task title (imperative action), 2. Description (2-3 sentences), "
    "3. Source type (teams or meeting), 4. Key people (full name and email), "
    "5. Priority (P1-P4), 6. Original subject, 7. Date, "
    "8. Action type (respond-email, follow-up, schedule-meeting, prepare, general). "
    "Format as numbered tasks with clear field labels."
)

try:
    proc = subprocess.run(
        [WORKIQ_CMD, 'ask', '-q', query],
        capture_output=True, text=True, encoding='utf-8', errors='replace',
        timeout=120
    )
    with open('data/_wiq_teams_result.txt', 'w', encoding='utf-8') as f:
        f.write(proc.stdout or '')
        if proc.stderr:
            f.write('\n=== STDERR ===\n')
            f.write(proc.stderr)
    print(f"Exit code: {proc.returncode}")
    print(f"Output length: {len(proc.stdout or '')} chars")
except subprocess.TimeoutExpired:
    print("TIMEOUT after 120s")
except Exception as e:
    print(f"ERROR: {e}")
