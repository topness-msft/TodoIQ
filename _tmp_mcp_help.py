import subprocess, os
env = os.environ.copy()
env['CLAUDECODE'] = ''
proc = subprocess.run(
    ["claude", "mcp", "--help"],
    capture_output=True, text=True, timeout=15, env=env,
    creationflags=subprocess.CREATE_NO_WINDOW
)
print(proc.stdout)
print(proc.stderr)
