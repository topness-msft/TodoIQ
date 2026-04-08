import subprocess, os

query = """Use the workiq tool to ask_work_iq this question: What Teams messages and meeting action items need my attention or action? Include: (1) Teams messages from the last 7 days directed at me by name or @mentioning me that I haven't responded to, (2) action items from meetings in the last 7 days assigned to me or that I committed to. For each item, return it as a structured task suggestion with ALL of these fields: 1. Task title: A clean imperative action describing WHAT I NEED TO DO. 2. Description: 2-3 sentences of context. 3. Source type: teams or meeting. 4. Key people: For each person involved, give their FULL resolved name and email address. 5. Priority: P1 (urgent/deadline today), P2 (time-sensitive), P3 (normal), P4 (low/FYI). 6. Original subject or topic. 7. Date: When the item was sent/occurred. 8. Action type: One of: respond-email, follow-up, schedule-meeting, prepare, general. Format each item as a numbered task with clear field labels. Return ONLY the structured task list, no preamble."""

proc = subprocess.run(
    ["copilot", "-p", query, "--allow-tool=workiq"],
    capture_output=True, text=True, timeout=120,
    cwd=os.getcwd(),
    creationflags=subprocess.CREATE_NO_WINDOW
)
print("=== STDOUT ===")
print(proc.stdout[-5000:] if len(proc.stdout) > 5000 else proc.stdout)
print("=== STDERR ===")
print(proc.stderr[-2000:] if len(proc.stderr) > 2000 else proc.stderr)
print(f"=== EXIT CODE: {proc.returncode} ===")
