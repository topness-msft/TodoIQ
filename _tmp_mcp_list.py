import subprocess, os
env = os.environ.copy()
env['CLAUDECODE'] = ''
proc = subprocess.run(
    ["claude", "mcp", "list"],
    capture_output=True, text=True, timeout=15, env=env,
    creationflags=subprocess.CREATE_NO_WINDOW
)
print(proc.stdout)
if proc.stderr:
    print("STDERR:", proc.stderr)
