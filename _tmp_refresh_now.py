"""Run WorkIQ scan via copilot subprocess and capture output."""
import subprocess
import sys
import os

DAYS = 1

QUERY_TEAMS = (
    "What Teams messages and meeting action items need my attention or action? "
    f"Include: (1) Teams messages from the last {DAYS} days directed at me by name or @mentioning me "
    "that I haven't responded to, (2) action items from meetings in the last {DAYS} days assigned to me "
    "or that I committed to. "
    "For each item, return it as a structured task suggestion with ALL of these fields: "
    "1. **Task title**: A clean imperative action describing WHAT I NEED TO DO. "
    "2. **Description**: 2-3 sentences of context. "
    "3. **Source type**: teams or meeting. "
    "4. **Key people**: Full resolved name and email address for each person. "
    "5. **Priority**: P1 (urgent/deadline today), P2 (time-sensitive), P3 (normal), P4 (low/FYI). "
    "6. **Original subject or topic**: The root subject. "
    "7. **Date**: When the item was sent/occurred. "
    "8. **Action type**: One of: respond-email, follow-up, schedule-meeting, prepare, general. "
    "Format each item as a numbered task with clear field labels."
)

print("=== Running WorkIQ Teams+Meetings scan ===")
print(f"Query length: {len(QUERY_TEAMS)} chars")

try:
    result = subprocess.run(
        ["copilot", "-p", QUERY_TEAMS, "--allow-tool=workiq"],
        capture_output=True, text=True, timeout=300,
        cwd=os.path.dirname(os.path.abspath(__file__)),
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    print("=== STDOUT ===")
    print(result.stdout)
    if result.stderr:
        print("=== STDERR ===")
        print(result.stderr[:500])
    print(f"=== Exit code: {result.returncode} ===")
except subprocess.TimeoutExpired:
    print("ERROR: WorkIQ query timed out after 300s")
    sys.exit(1)
except FileNotFoundError:
    print("ERROR: copilot CLI not found on PATH")
    sys.exit(1)

# Step 0: Clear sync request marker
p = Path('data') / '.sync_requested'
if p.exists():
    p.unlink()
    print('Cleared .sync_requested marker')

# Step 1: Determine sync window
conn = sqlite3.connect('data/claudetodo.db')
conn.row_factory = sqlite3.Row
row = conn.execute(
    "SELECT synced_at FROM sync_log WHERE sync_type IN ('full_scan','flagged_emails') ORDER BY synced_at DESC LIMIT 1"
).fetchone()
conn.close()

now = datetime.now(timezone.utc)
if row and row['synced_at']:
    last_dt = datetime.fromisoformat(row['synced_at'].replace('Z', '+00:00'))
    days = max(1, (now - last_dt).days)
    if days > 7:
        days = 7
    print(f"Last sync: {row['synced_at']}")
else:
    days = 7
    print("No previous sync found")

print(f"DAYS_SINCE={days}")
