"""Run both WorkIQ queries in parallel, wait for results, print output."""
import subprocess
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent

teams_prompt = (
    "What Teams messages and meeting action items need my attention or action? "
    "Include: (1) Teams messages from the last 1 days directed at me by name or "
    "@mentioning me that I have not responded to, (2) action items from meetings "
    "in the last 1 days assigned to me or that I committed to. For each item, "
    "return it as a structured task suggestion with ALL of these fields: "
    "1. **Task title**: A clean imperative action describing WHAT I NEED TO DO "
    "(e.g. 'Schedule workshop walkthrough with Alex'). Not the message topic - "
    "describe the action. "
    "2. **Description**: 2-3 sentences of context: what was the original ask, "
    "current state, what specifically needs to happen next. "
    "3. **Source type**: teams or meeting. "
    "4. **Key people**: For each person involved, give their FULL resolved name "
    "and email address (e.g. 'Jane Doe, jane.doe@contoso.com'). Resolve aliases "
    "and short names to full directory names. "
    "5. **Priority**: P1 (urgent/deadline today), P2 (time-sensitive), P3 (normal), P4 (low/FYI). "
    "6. **Original subject or topic**: The root subject (strip Re:/Fwd: prefixes). "
    "7. **Date**: When the item was sent/occurred. "
    "8. **Action type**: One of: respond-email, follow-up, schedule-meeting, prepare, general. "
    "Format each item as a numbered task with clear field labels."
)

awaiting_prompt = (
    "What messages or emails have I SENT in the last 1 days that contain a question, "
    "request, or ask where the recipient has not responded yet? Only include items where "
    "I am clearly waiting for a response - not messages I sent that were purely informational. "
    "For each item, return it as a structured task suggestion with ALL of these fields: "
    "1. **Task title**: A clean imperative action (e.g. 'Follow up with Alex on budget approval'). "
    "2. **Description**: 2-3 sentences: what I asked, who I am waiting on, when I sent it. "
    "3. **Source type**: email, teams, or meeting. "
    "4. **Key people**: For each person involved, give their FULL resolved name and email address. "
    "5. **Priority**: P3 (normal) or P4 (low) - these are lower urgency since I am waiting, not being asked. "
    "6. **Original subject or topic**: The root subject (strip Re:/Fwd: prefixes). "
    "7. **Date**: When I sent the message. "
    "8. **Action type**: awaiting-response. "
    "Format each item as a numbered task with clear field labels."
)

def launch(prompt, label):
    log_dir = PROJECT_ROOT / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"_refresh_{label}.log"
    fh = open(str(log_path), "w")
    proc = subprocess.Popen(
        [
            "copilot", "-p", prompt,
            "--allow-tool=workiq",
        ],
        cwd=str(PROJECT_ROOT),
        stdout=fh,
        stderr=fh,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    return proc, fh, log_path

print("Launching WorkIQ queries...", flush=True)
t0 = time.time()

p1, fh1, log1 = launch(teams_prompt, "teams")
p2, fh2, log2 = launch(awaiting_prompt, "awaiting")

print(f"  Teams PID={p1.pid}, Awaiting PID={p2.pid}", flush=True)

# Wait for both (max 5 min each)
p1.wait(timeout=300)
fh1.close()
p2.wait(timeout=300)
fh2.close()

elapsed = time.time() - t0
print(f"Both queries completed in {elapsed:.0f}s", flush=True)

print("\n=== TEAMS/MEETINGS RESULT ===")
print(log1.read_text(encoding="utf-8", errors="replace"))

print("\n=== AWAITING RESPONSE RESULT ===")
print(log2.read_text(encoding="utf-8", errors="replace"))
