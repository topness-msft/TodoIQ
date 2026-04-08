"""WorkIQ refresh scan - Teams/Meetings + Awaiting Response queries."""
import subprocess
import json
import sys
import os

DAYS = 1

QUERY_TEAMS = f"""What Teams messages and meeting action items need my attention or action? Include: (1) Teams messages from the last {DAYS} days directed at me by name or @mentioning me that I haven't responded to, (2) action items from meetings in the last {DAYS} days assigned to me or that I committed to. For each item, return it as a structured task suggestion with ALL of these fields: 1. **Task title**: A clean imperative action describing WHAT I NEED TO DO (e.g. "Schedule workshop walkthrough with Alex"). Not the message topic — describe the action. 2. **Description**: 2-3 sentences of context: what was the original ask, current state, what specifically needs to happen next. 3. **Source type**: teams or meeting. 4. **Key people**: For each person involved, give their FULL resolved name and email address (e.g. "Jane Doe, jane.doe@contoso.com"). Resolve aliases and short names to full directory names. 5. **Priority**: P1 (urgent/deadline today), P2 (time-sensitive), P3 (normal), P4 (low/FYI). 6. **Original subject or topic**: The root subject (strip Re:/Fwd: prefixes). 7. **Date**: When the item was sent/occurred. 8. **Action type**: One of: respond-email, follow-up, schedule-meeting, prepare, general. Format each item as a numbered task with clear field labels."""

QUERY_AWAITING = f"""What messages or emails have I SENT in the last {DAYS} days that contain a question, request, or ask where the recipient hasn't responded yet? Only include items where I am clearly waiting for a response — not messages I sent that were purely informational. For each item, return it as a structured task suggestion with ALL of these fields: 1. **Task title**: A clean imperative action (e.g. "Follow up with Alex on budget approval"). 2. **Description**: 2-3 sentences: what I asked, who I'm waiting on, when I sent it. 3. **Source type**: email, teams, or meeting. 4. **Key people**: For each person involved, give their FULL resolved name and email address. 5. **Priority**: P3 (normal) or P4 (low) — these are lower urgency since I'm waiting, not being asked. 6. **Original subject or topic**: The root subject (strip Re:/Fwd: prefixes). 7. **Date**: When I sent the message. 8. **Action type**: awaiting-response. Format each item as a numbered task with clear field labels."""


def run_workiq(query, label):
    """Run a WorkIQ query via copilot -p."""
    print(f"\n--- {label} ---", flush=True)
    try:
        result = subprocess.run(
            ["copilot", "-p", query, "--allow-tool=workiq"],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=os.getcwd()
        )
        output = result.stdout.strip()
        if result.returncode != 0:
            err = result.stderr.strip()
            print(f"ERROR (rc={result.returncode}): {err[:500]}", flush=True)
            if output:
                print(f"STDOUT: {output[:2000]}", flush=True)
            return None
        print(output, flush=True)
        return output
    except subprocess.TimeoutExpired:
        print("TIMEOUT: WorkIQ query timed out after 120s", flush=True)
        return None
    except FileNotFoundError:
        print("ERROR: copilot CLI not found on PATH", flush=True)
        return None


if __name__ == "__main__":
    # Write results to files for later processing
    teams_result = run_workiq(QUERY_TEAMS, "Teams + Meetings Scan")
    with open("data/_workiq_teams.txt", "w", encoding="utf-8") as f:
        f.write(teams_result or "NO_RESULTS")

    awaiting_result = run_workiq(QUERY_AWAITING, "Awaiting Response Scan")
    with open("data/_workiq_awaiting.txt", "w", encoding="utf-8") as f:
        f.write(awaiting_result or "NO_RESULTS")

    print("\n--- Done ---")
    print(f"Teams result length: {len(teams_result or '')}")
    print(f"Awaiting result length: {len(awaiting_result or '')}")
