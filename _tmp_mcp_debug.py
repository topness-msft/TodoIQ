import subprocess, json, sys, threading, time

proc = subprocess.Popen(
    [r'C:\Program Files\nodejs\npx.cmd', '-y', '@microsoft/workiq', 'mcp'],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
)

print(f'PID {proc.pid}')

stdout_data = bytearray()
stderr_data = bytearray()

def read_stdout():
    while True:
        chunk = proc.stdout.read(1)
        if not chunk:
            break
        stdout_data.extend(chunk)

def read_stderr():
    while True:
        chunk = proc.stderr.read(1)
        if not chunk:
            break
        stderr_data.extend(chunk)

t1 = threading.Thread(target=read_stdout, daemon=True)
t2 = threading.Thread(target=read_stderr, daemon=True)
t1.start()
t2.start()

time.sleep(10)

out_str = stdout_data.decode('utf-8', errors='replace')
err_str = stderr_data.decode('utf-8', errors='replace')
print(f'stdout ({len(stdout_data)} bytes): {repr(out_str[:500])}')
print(f'stderr ({len(stderr_data)} bytes): {repr(err_str[:500])}')
print(f'Process alive: {proc.poll() is None}')

# Send initialize
body = json.dumps({
    'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
    'params': {
        'protocolVersion': '2024-11-05',
        'capabilities': {},
        'clientInfo': {'name': 'todoness', 'version': '1.0'}
    }
})
frame = f'Content-Length: {len(body)}\r\n\r\n{body}'
proc.stdin.write(frame.encode('utf-8'))
proc.stdin.flush()
print('Sent initialize')

time.sleep(15)
out_str = stdout_data.decode('utf-8', errors='replace')
err_str = stderr_data.decode('utf-8', errors='replace')
print(f'stdout after init ({len(stdout_data)} bytes): {repr(out_str[:1000])}')
print(f'stderr after init ({len(stderr_data)} bytes): {repr(err_str[:1000])}')

proc.terminate()
