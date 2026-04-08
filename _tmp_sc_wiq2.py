import subprocess, sys, json

def query_workiq(prompt, label):
    """Run copilot -p with workiq and wait for result."""
    try:
        result = subprocess.run(
            ["copilot", "-p", prompt, "--allow-tool=workiq"],
            capture_output=True, text=True, timeout=90,
            cwd=r"C:\Users\phtopnes\claude\projects\ClaudeTodo"
        )
        return result.stdout.strip() or result.stderr.strip() or "(no output)"
    except subprocess.TimeoutExpired:
        return "(timeout)"
    except Exception as e:
        return f"(error: {e})"

# Query Matt Sheard tasks
r = query_workiq(
    "Use WorkIQ to find: recent emails/Teams messages with Matt Sheard (msheard@microsoft.com) about EBC session April 21 since April 8 2026. Was any of this resolved? Brief summary.",
    "matt-sheard"
)
print("=== MATT SHEARD ===")
print(r[:500])
