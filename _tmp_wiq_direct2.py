import subprocess

WORKIQ_CMD = r'C:\Users\phtopnes\AppData\Roaming\npm\workiq.cmd'
query = (
    "What messages or emails have I SENT in the last 7 days that contain a question, request, or ask "
    "where the recipient has not responded yet? Only include items where I am clearly waiting for a response. "
    "For each item return: 1. Task title (imperative action like 'Follow up with X on Y'), "
    "2. Description (2-3 sentences), 3. Source type (email, teams, or meeting), "
    "4. Key people (full name and email), 5. Priority (P3 or P4), "
    "6. Original subject, 7. Date sent, 8. Action type: awaiting-response. "
    "Format as numbered tasks with clear field labels."
)

try:
    proc = subprocess.run(
        [WORKIQ_CMD, 'ask', '-q', query],
        capture_output=True, text=True, encoding='utf-8', errors='replace',
        timeout=120
    )
    with open('data/_wiq_awaiting_result.txt', 'w', encoding='utf-8') as f:
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
