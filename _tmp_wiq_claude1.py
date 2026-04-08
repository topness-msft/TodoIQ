import subprocess, os

query = 'Use the mcp__workiq__ask_work_iq tool to ask: What Teams messages and meeting action items need my attention or action from the last 7 days? For each item return: 1. Task title (imperative action), 2. Description (2-3 sentences), 3. Source type (teams or meeting), 4. Key people (full name and email), 5. Priority (P1-P4), 6. Original subject, 7. Date, 8. Action type (respond-email, follow-up, schedule-meeting, prepare, general). Format as numbered tasks with clear field labels.'

with open('data/_wiq_teams.log', 'w', encoding='utf-8') as fh:
    env = os.environ.copy()
    env['CLAUDECODE'] = ''
    proc = subprocess.Popen(
        ["claude", "-p", query,
         "--allowedTools", "mcp__workiq__ask_work_iq",
         "--no-session-persistence"],
        cwd=os.getcwd(),
        stdout=fh, stderr=fh,
        env=env,
        creationflags=subprocess.CREATE_NO_WINDOW
    )
    try:
        proc.wait(timeout=180)
        print(f"Exit code: {proc.returncode}")
    except subprocess.TimeoutExpired:
        proc.kill()
        print("TIMEOUT after 180s")
