import subprocess, os, json

query_params = json.dumps({"question": "What Teams messages and meeting action items need my attention or action from the last 7 days? For each item return: 1. Task title (imperative action), 2. Description (2-3 sentences), 3. Source type (teams or meeting), 4. Key people (full name and email), 5. Priority (P1-P4), 6. Original subject, 7. Date, 8. Action type (respond-email, follow-up, schedule-meeting, prepare, general). Format as numbered tasks with clear field labels."})

env = os.environ.copy()
env['CLAUDECODE'] = ''

proc = subprocess.run(
    ["claude", "mcp", "call", "workiq", "ask_work_iq", query_params],
    capture_output=True, text=True, timeout=120, env=env,
    cwd=os.getcwd(),
    creationflags=subprocess.CREATE_NO_WINDOW
)
print("=== STDOUT (last 3000) ===")
print(proc.stdout[-3000:] if len(proc.stdout) > 3000 else proc.stdout)
if proc.stderr:
    print("=== STDERR (last 1000) ===")
    print(proc.stderr[-1000:])
print(f"=== EXIT CODE: {proc.returncode} ===")
