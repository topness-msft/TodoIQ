#!/usr/bin/env python3
"""Run /todo-refresh sync and wait for completion."""

import sys
import time
import json

sys.path.insert(0, 'src')

from services.claude_runner import run_copilot, is_running, get_exit_info

# Start the sync
print("Starting /todo-refresh sync...")
result = run_copilot("/todo-refresh", label="sync", timeout=600)
print(f"Start result: {json.dumps(result)}")

if result['ok']:
    print("\nWaiting for sync to complete (checking every 5 seconds)...")
    start = time.time()
    while is_running("sync"):
        elapsed = int(time.time() - start)
        print(f"  Still running... ({elapsed}s elapsed)")
        time.sleep(5)
    
    print("Sync completed!")
    
    # Get exit info
    exit_info = get_exit_info("sync")
    print(f"Exit info: {json.dumps(exit_info)}")
else:
    print(f"Failed to start: {result['message']}")
    sys.exit(1)
