import subprocess

queries = [
    ("matt-sheard-ebc", "Call the ask_work_iq tool with this query: What are my most recent emails and Teams messages with Matt Sheard (msheard@microsoft.com) about EBC session April 21 2026 - participant list, session times, facilitator, expense info - since April 8 2026? List what was found and state if resolved or still pending. Only use the ask_work_iq tool, output just the results."),
    ("aamer-cab-retro", "Call the ask_work_iq tool with this query: What are my most recent emails and Teams messages with Aamer Kaleem (aamer.kaleem@microsoft.com) about Steve CAB travel funding and CPM SKI retrospective feedback since April 8 2026? List what was found and state if resolved or still pending. Only use the ask_work_iq tool, output just the results."),
]

for label, prompt in queries:
    try:
        result = subprocess.run(
            ["copilot", "-p", prompt, "--allow-tool=workiq"],
            capture_output=True, text=True, timeout=120,
            cwd=r"C:\Users\phtopnes\claude\projects\ClaudeTodo"
        )
        output = (result.stdout + result.stderr).strip()
        print(f"=== {label} ===")
        print(output[:800])
        print()
    except Exception as e:
        print(f"=== {label} ERROR: {e} ===")
