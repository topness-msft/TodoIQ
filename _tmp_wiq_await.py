import subprocess, os

query = "Use the workiq tool to ask_work_iq this question: What messages or emails have I SENT in the last 7 days that contain a question, request, or ask where the recipient has not responded yet? Only include items where I am clearly waiting for a response. For each item return: 1. Task title (imperative action like 'Follow up with X on Y'), 2. Description (2-3 sentences), 3. Source type (email, teams, or meeting), 4. Key people (full name and email), 5. Priority (P3 or P4), 6. Original subject, 7. Date sent, 8. Action type: awaiting-response. Format as numbered tasks."

with open('data/_wiq_awaiting.log', 'w', encoding='utf-8') as fh:
    proc = subprocess.Popen(
        ["copilot", "-p", query, "--allow-tool=workiq"],
        cwd=os.getcwd(),
        stdout=fh, stderr=fh,
        creationflags=subprocess.CREATE_NO_WINDOW
    )
    try:
        proc.wait(timeout=180)
        print(f"Exit code: {proc.returncode}")
    except subprocess.TimeoutExpired:
        proc.kill()
        print("TIMEOUT after 180s")
