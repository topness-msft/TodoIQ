import subprocess, sys, os

tasks = [
    {"id": 626, "person": "Maria Luisa Onorato", "email": "maria.onorato@microsoft.com",
     "topic": "confirm attendee details", "since": "2026-03-25"},
    {"id": 623, "person": "Remy Ntshaykolo", "email": "remyn@microsoft.com",
     "topic": "customer scenarios for agent-to-agent orchestration", "since": "2026-03-26"},
    {"id": 610, "person": "Justin Walker", "email": "justw@microsoft.com",
     "topic": "follow-up on adoption pattern metrics", "since": "2026-03-26"},
    {"id": 594, "person": "Rodrigo De la Garza", "email": "rodela@microsoft.com",
     "topic": "offsite shark tank workshop request", "since": "2026-03-26"},
]

for t in tasks:
    prompt = (
        f"Call ask_work_iq with this exact query and output ONLY one line in the format "
        f"RESULT:{t['id']}|STATUS|SUMMARY\n\n"
        f"Query: What are my most recent emails, Teams messages, and chats with "
        f"{t['person']} ({t['email']}) about '{t['topic']}' since {t['since']}? "
        f"Was this topic resolved, addressed, or is it still pending? List all interactions found.\n\n"
        f"Classify as: likely_resolved (clear evidence resolved), still_pending (no response/unresolved), "
        f"or unclear (activity found but can't confirm). "
        f"Output exactly one line starting with RESULT:{t['id']}|"
    )
    print(f"\n=== Querying task {t['id']}: {t['person']} ===", flush=True)
    result = subprocess.run(
        ["copilot", "-p", prompt,
         "--allow-tool=workiq"],
        capture_output=True, text=True, encoding='utf-8', errors='replace',
        cwd=os.path.dirname(os.path.abspath(__file__))
    )
    print(result.stdout[-3000:] if len(result.stdout) > 3000 else result.stdout)
    if result.stderr:
        print("STDERR:", result.stderr[-500:])
    print(f"=== END {t['id']} ===", flush=True)
