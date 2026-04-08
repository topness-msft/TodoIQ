"""WorkIQ scan: Teams + Meetings and Awaiting Response.
Calls WorkIQ CLI directly for structured task suggestions.
"""
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

WORKIQ = r'C:\Users\phtopnes\AppData\Roaming\npm\workiq.cmd'

days = 1

query_teams = (
    f"What Teams messages and meeting action items need my attention or action? "
    f"Include: (1) Teams messages from the last {days} days directed at me by name or @mentioning me that I have not responded to, "
    f"(2) action items from meetings in the last {days} days assigned to me or that I committed to. "
    "For each item return a structured task suggestion with ALL these fields: "
    "1. Task title: A clean imperative action. "
    "2. Description: 2-3 sentences of context. "
    "3. Source type: teams or meeting. "
    "4. Key people: full resolved name and email. "
    "5. Priority: P1/P2/P3/P4. "
    "6. Original subject or topic. "
    "7. Date. "
    "8. Action type: respond-email, follow-up, schedule-meeting, prepare, or general. "
    "Format each as a numbered task with clear field labels."
)

query_awaiting = (
    f"What messages or emails have I SENT in the last {days} days that contain a question, request, or ask "
    "where the recipient has not responded yet? Only include items where I am clearly waiting for a response. "
    "For each item return: "
    "1. Task title: imperative (e.g. Follow up with X on Y). "
    "2. Description: 2-3 sentences: what I asked, who I am waiting on, when I sent it. "
    "3. Source type: email, teams, or meeting. "
    "4. Key people: full resolved name and email. "
    "5. Priority: P3 or P4. "
    "6. Original subject. "
    "7. Date. "
    "8. Action type: awaiting-response. "
    "Format as numbered tasks with field labels."
)

def run_query(query, output_file, label):
    print(f"Starting {label}...")
    out_path = DATA_DIR / output_file
    try:
        proc = subprocess.run(
            [WORKIQ, 'ask', '-q', query],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            timeout=120,
            cwd=str(PROJECT_ROOT),
        )
        content = proc.stdout or ''
        with open(str(out_path), 'w', encoding='utf-8') as f:
            f.write(content)
            if proc.stderr:
                f.write('\n=== STDERR ===\n' + proc.stderr)
        print(f"{label} complete ({len(content)} chars, exit={proc.returncode})")
        return content
    except subprocess.TimeoutExpired:
        out_path.write_text("TIMEOUT after 120s", encoding="utf-8")
        print(f"{label} TIMEOUT after 120s")
        return "TIMEOUT"
    except FileNotFoundError:
        print(f"WorkIQ CLI not found at {WORKIQ}")
        return "ERROR: workiq not found"
    except Exception as e:
        print(f"{label} ERROR: {e}")
        return f"ERROR: {e}"

teams_result = run_query(query_teams, "wiq_teams_scan.txt", "Teams+Meetings scan")
awaiting_result = run_query(query_awaiting, "wiq_awaiting_scan.txt", "Awaiting Response scan")

print(f"\n--- Done ---")
print(f"Teams output: {len(teams_result)} chars")
print(f"Awaiting output: {len(awaiting_result)} chars")
